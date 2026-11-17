"""back-merge-bump re-runs after a failure part-way through (#4415).

Real repositories stand in for the release worktree and origin; only GitHub
and the in-container lockfile refresh are faked.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import git
from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_MOD = "vergil_tooling.lib.release.bump"
_BRANCH = "release/post-0.1.0"
_PR = "https://github.com/owner/repo/pull/12"
_SUBJECT = "chore(release): bump version to 0.1.1"
_REAL_GIT_RUN = git.run


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_rust_repo(root: Path) -> None:
    (root / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        'primary-language = "rust"\n\n[dependencies]\nvergil = "v2.0"\n'
        '\n[ci]\nversions = ["1.92"]\n'
    )
    (root / "VERSION").write_text("0.1.0\n")
    (root / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.0"\n')
    (root / "Cargo.lock").write_text('[[package]]\nname = "fixture"\nversion = "0.1.0"\n')
    (root / "README.md").write_text("fixture\n")


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A release worktree on release/0.1.0, with main released at 0.1.0 on origin."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "main")
    _write_rust_repo(work)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "release 0.1.0")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "release/0.1.0")
    monkeypatch.chdir(work)
    return work


def _lock(root: Path) -> None:
    """Stand-in for the in-container ``cargo update --workspace``."""
    new = (root / "VERSION").read_text().strip()
    (root / "Cargo.lock").write_text(f'[[package]]\nname = "fixture"\nversion = "{new}"\n')


def _lock_fails(root: Path) -> None:
    raise subprocess.CalledProcessError(127, ["vrg-container-run", "--", "cargo"], stderr="boom")


def _leave_partial_bump(root: Path) -> None:
    """A run killed outright mid-bump, before the bump could restore its files.

    A bump that fails restores its files itself (#4587), so a partial bump is
    left only by a kill no handler sees: the first run fails in its lockfile
    step, then VERSION and Cargo.toml are bumped by hand, as the killed run
    would have left them.
    """
    with pytest.raises(subprocess.CalledProcessError):
        _run(root, lock=_lock_fails)
    (root / "VERSION").write_text("0.1.1\n")
    (root / "Cargo.toml").write_text('[package]\nname = "fixture"\nversion = "0.1.1"\n')


@pytest.fixture
def gh() -> Iterator[dict[str, MagicMock]]:
    """GitHub with no back-merge PR yet; creating and merging one succeeds."""
    with (
        patch("vergil_tooling.lib.github.get_installation_token", return_value=None),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=None),
        patch(_MOD + ".github.create_pr", return_value=_PR) as create_pr,
        patch(_MOD + ".github.merge_commit_sha", return_value="m3rge"),
        patch(_MOD + ".wait_and_merge") as wait_and_merge,
    ):
        yield {"create_pr": create_pr, "wait_and_merge": wait_and_merge}


def _ctx(root: Path) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="0.1.0",
        repo_root=root,
        version_override=None,
        worktree_path=root,
    )
    ctx.issue_number = 9
    return ctx


def _run(root: Path, lock: Callable[[Path], None] = _lock) -> ReleaseContext:
    ctx = _ctx(root)
    with patch("vergil_tooling.lib.version._run_lockfile_maintenance", lambda r, _lang: lock(r)):
        back_merge_and_bump(ctx)
    return ctx


def _fail_on(subcommand: str) -> Callable[..., None]:
    """A ``git.run`` that fails every *subcommand* call and runs the rest."""

    def run(*args: str) -> None:
        if args[0] == subcommand:
            raise subprocess.CalledProcessError(1, ["git", *args])
        _REAL_GIT_RUN(*args)

    return run


def _post_commits(root: Path, ref: str = _BRANCH) -> list[str]:
    return _git(root, "log", "--format=%s", f"origin/main..{ref}").splitlines()


def _origin_tip(root: Path) -> str:
    return _git(root, "ls-remote", "--heads", "origin", _BRANCH).partition("\t")[0]


def _assert_bumped_and_pushed(root: Path, ctx: ReleaseContext) -> None:
    assert ctx.next_version == "0.1.1"
    assert ctx.bump_pr_url == _PR
    assert _post_commits(root) == [_SUBJECT]
    assert _git(root, "show", f"{_BRANCH}:VERSION") == "0.1.1"
    assert 'version = "0.1.1"' in _git(root, "show", f"{_BRANCH}:Cargo.toml")
    assert _origin_tip(root) == _git(root, "rev-parse", _BRANCH)
    assert _git(root, "status", "--porcelain") == ""


# --- re-runs that complete ---------------------------------------------------


def test_first_run_creates_bumps_and_pushes(repo: Path, gh: dict[str, MagicMock]) -> None:
    ctx = _run(repo)
    _assert_bumped_and_pushed(repo, ctx)
    gh["create_pr"].assert_called_once()


def test_resume_after_bump_failed_before_commit(repo: Path, gh: dict[str, MagicMock]) -> None:
    """The lockfile step failed: the branch is at origin/main and the worktree is clean.

    The failed bump restored VERSION and Cargo.toml itself (#4587), so the
    re-run bumps once, to 0.1.1.
    """
    with pytest.raises(subprocess.CalledProcessError):
        _run(repo, lock=_lock_fails)
    assert git.current_branch() == _BRANCH
    assert _post_commits(repo) == []
    assert _git(repo, "status", "--porcelain") == ""
    assert _origin_tip(repo) == ""

    ctx = _run(repo)

    _assert_bumped_and_pushed(repo, ctx)
    assert 'version = "0.1.1"' in _git(repo, "show", f"{_BRANCH}:Cargo.lock")
    gh["create_pr"].assert_called_once()


def test_resume_after_bump_killed_before_commit(repo: Path, gh: dict[str, MagicMock]) -> None:
    """The branch at origin/main, a partial bump uncommitted by a killed run.

    The re-run discards that partial bump and redoes it, so the version moves
    to 0.1.1 once, never to 0.1.2.
    """
    _leave_partial_bump(repo)
    assert git.current_branch() == _BRANCH
    assert sorted(_git(repo, "diff", "--name-only").splitlines()) == ["Cargo.toml", "VERSION"]

    ctx = _run(repo)

    _assert_bumped_and_pushed(repo, ctx)
    assert 'version = "0.1.1"' in _git(repo, "show", f"{_BRANCH}:Cargo.lock")
    gh["create_pr"].assert_called_once()


def test_resume_with_branch_at_main_and_clean_worktree(
    repo: Path, gh: dict[str, MagicMock]
) -> None:
    _git(repo, "checkout", "-q", "-b", _BRANCH, "origin/main")
    ctx = _run(repo)
    _assert_bumped_and_pushed(repo, ctx)


def test_resume_with_branch_not_checked_out(repo: Path, gh: dict[str, MagicMock]) -> None:
    """A leftover branch the worktree is not on is switched to, then reused."""
    _git(repo, "branch", _BRANCH, "origin/main")
    ctx = _run(repo)
    _assert_bumped_and_pushed(repo, ctx)


def test_resume_after_commit_failed_before_push(repo: Path, gh: dict[str, MagicMock]) -> None:
    """The bump commit exists but was never pushed: it is reused, not bumped again."""
    with patch.object(git, "run", _fail_on("push")), pytest.raises(subprocess.CalledProcessError):
        _run(repo)
    assert _post_commits(repo) == [_SUBJECT]
    assert _origin_tip(repo) == ""
    gh["create_pr"].assert_not_called()

    with patch(_MOD + ".version.bump") as bump:
        ctx = _run(repo)

    bump.assert_not_called()
    _assert_bumped_and_pushed(repo, ctx)
    gh["create_pr"].assert_called_once()


def test_resume_after_push_failed_before_pr(repo: Path, gh: dict[str, MagicMock]) -> None:
    """Pushed, then the PR create failed: the push is a no-op and the PR is created."""
    gh["create_pr"].side_effect = [subprocess.CalledProcessError(1, ["gh"]), _PR]
    with pytest.raises(subprocess.CalledProcessError):
        _run(repo)
    pushed = _origin_tip(repo)
    assert pushed == _git(repo, "rev-parse", _BRANCH)

    with patch(_MOD + ".version.bump") as bump:
        ctx = _run(repo)

    bump.assert_not_called()
    _assert_bumped_and_pushed(repo, ctx)
    assert _origin_tip(repo) == pushed
    assert gh["create_pr"].call_count == 2


def test_resume_after_push_with_local_branch_removed(repo: Path, gh: dict[str, MagicMock]) -> None:
    """The pushed branch is checked out from origin when the local one is gone."""
    gh["create_pr"].side_effect = [subprocess.CalledProcessError(1, ["gh"]), _PR]
    with pytest.raises(subprocess.CalledProcessError):
        _run(repo)
    pushed = _origin_tip(repo)
    _git(repo, "checkout", "-q", "release/0.1.0")
    _git(repo, "branch", "-q", "-D", _BRANCH)

    ctx = _run(repo)

    _assert_bumped_and_pushed(repo, ctx)
    assert git.current_branch() == _BRANCH
    assert _origin_tip(repo) == pushed


# --- states that are refused, with nothing changed -------------------------


def _assert_refused(repo: Path, gh: dict[str, MagicMock], fragment: str) -> None:
    before = (_git(repo, "rev-parse", _BRANCH), _git(repo, "status", "--porcelain"))
    with pytest.raises(ReleaseError, match=fragment) as exc:
        _run(repo)
    assert exc.value.phase == "back-merge-bump"
    assert _BRANCH in str(exc.value)
    assert (_git(repo, "rev-parse", _BRANCH), _git(repo, "status", "--porcelain")) == before
    gh["create_pr"].assert_not_called()


def test_refuses_branch_with_foreign_commit(repo: Path, gh: dict[str, MagicMock]) -> None:
    _git(repo, "checkout", "-q", "-b", _BRANCH, "origin/main")
    (repo / "README.md").write_text("someone's work\n")
    _git(repo, "commit", "-qam", "docs: someone's work")
    _assert_refused(repo, gh, "not back-merge-bump's own")
    assert _origin_tip(repo) == ""


def test_refuses_bump_commit_followed_by_more_work(repo: Path, gh: dict[str, MagicMock]) -> None:
    with patch.object(git, "run", _fail_on("push")), pytest.raises(subprocess.CalledProcessError):
        _run(repo)
    (repo / "README.md").write_text("more\n")
    _git(repo, "commit", "-qam", "docs: more")
    _assert_refused(repo, gh, "not back-merge-bump's own")


def test_refuses_bump_commit_to_another_version(repo: Path, gh: dict[str, MagicMock]) -> None:
    _git(repo, "checkout", "-q", "-b", _BRANCH, "origin/main")
    (repo / "VERSION").write_text("0.2.0\n")
    _git(repo, "commit", "-qam", "chore(release): bump version to 0.2.0")
    _assert_refused(repo, gh, "not back-merge-bump's own")


def test_refuses_bump_subject_with_other_version_file(repo: Path, gh: dict[str, MagicMock]) -> None:
    """The stage's own subject is not enough: VERSION must hold that version too."""
    _git(repo, "checkout", "-q", "-b", _BRANCH, "origin/main")
    (repo / "VERSION").write_text("0.2.0\n")
    _git(repo, "commit", "-qam", _SUBJECT)
    _assert_refused(repo, gh, "VERSION reads 0.2.0")


def test_refuses_branch_without_origin_main(repo: Path, gh: dict[str, MagicMock]) -> None:
    _git(repo, "checkout", "-q", "--orphan", _BRANCH)
    _git(repo, "commit", "-qm", "unrelated")
    _assert_refused(repo, gh, "does not contain origin/main")


def test_refuses_diverged_local_and_remote(repo: Path, gh: dict[str, MagicMock]) -> None:
    _git(repo, "checkout", "-q", "-b", _BRANCH, "origin/main")
    (repo / "README.md").write_text("remote\n")
    _git(repo, "commit", "-qam", "remote work")
    _git(repo, "push", "-q", "origin", _BRANCH)
    _git(repo, "reset", "-q", "--hard", "origin/main")
    _assert_refused(repo, gh, "exists locally at")


def test_refuses_partial_bump_mixed_with_other_changes(
    repo: Path, gh: dict[str, MagicMock]
) -> None:
    _leave_partial_bump(repo)
    (repo / "README.md").write_text("local edit\n")
    _assert_refused(repo, gh, "not a partial bump")
    assert (repo / "VERSION").read_text() == "0.1.1\n"


def test_refuses_untracked_file(repo: Path, gh: dict[str, MagicMock]) -> None:
    _leave_partial_bump(repo)
    (repo / "notes.txt").write_text("keep me\n")
    _assert_refused(repo, gh, "not a partial bump")
    assert (repo / "notes.txt").is_file()


def test_refuses_dirty_version_at_other_value(repo: Path, gh: dict[str, MagicMock]) -> None:
    _git(repo, "checkout", "-q", "-b", _BRANCH, "origin/main")
    (repo / "VERSION").write_text("0.3.0\n")
    _assert_refused(repo, gh, "not a partial bump")


def test_refuses_changes_on_top_of_bump_commit(repo: Path, gh: dict[str, MagicMock]) -> None:
    with patch.object(git, "run", _fail_on("push")), pytest.raises(subprocess.CalledProcessError):
        _run(repo)
    (repo / "Cargo.lock").write_text("edited\n")
    _assert_refused(repo, gh, "uncommitted")


def test_refuses_switch_with_dirty_worktree(repo: Path, gh: dict[str, MagicMock]) -> None:
    """A leftover branch is not switched to over uncommitted changes."""
    _git(repo, "branch", _BRANCH, "origin/main")
    (repo / "README.md").write_text("local edit\n")
    _assert_refused(repo, gh, "uncommitted")
    assert git.current_branch() == "release/0.1.0"
