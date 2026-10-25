"""Tests for vergil_tooling.lib.upstream_refresh (epic vergil-project/.github#472, T6).

Git runs for real against a temp bare ``origin``; only the ``gh`` calls are faked.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.upstream_refresh import (
    COMMIT_SUBJECT,
    REFRESH_BRANCH,
    REPORT_ENV,
    RefreshPr,
    UpstreamRefreshError,
    changed_paths,
    ensure_clean_on_base,
    open_refresh_prs,
    render_body,
    run_hook,
    sync,
)

if TYPE_CHECKING:
    from pathlib import Path

_REPO = "o/r"
_BASE = "develop"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class FakeGitHub:
    """Records every gh call; ``prs`` is what ``gh pr list`` returns."""

    prs: object = field(default_factory=list)
    runs: list[tuple[str, ...]] = field(default_factory=list)
    creates: list[tuple[str, ...]] = field(default_factory=list)
    bodies: list[tuple[str, str]] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)

    @property
    def writes(self) -> list[object]:
        return [*self.runs, *self.creates, *self.bodies, *self.deletes]

    def comments(self) -> list[tuple[str, ...]]:
        return [call for call in self.runs if call[:2] == ("pr", "comment")]

    def closes(self) -> list[tuple[str, ...]]:
        return [call for call in self.runs if call[:2] == ("pr", "close")]


@dataclass
class Env:
    origin: Path
    seed: Path
    work: Path
    gh: FakeGitHub

    def branch_on_origin(self, branch: str, content: str) -> str:
        """Create/overwrite *branch* on origin as base + one commit setting data.txt."""
        _git(self.seed, "fetch", "-q", "origin")
        _git(self.seed, "checkout", "-q", "-B", branch, f"origin/{_BASE}")
        (self.seed / "data.txt").write_text(content)
        _git(self.seed, "commit", "-qam", f"seed {branch}")
        _git(self.seed, "push", "-q", "-f", "origin", f"{branch}:{branch}")
        _git(self.seed, "checkout", "-q", _BASE)
        return _git(self.seed, "rev-parse", branch)

    def origin_sha(self, branch: str) -> str:
        return _git(self.origin, "rev-parse", f"refs/heads/{branch}")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(github, "repo_installation_token", lambda _repo: None)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", _BASE, str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "checkout", "-q", "-b", _BASE)
    (seed / "data.txt").write_text("old\n")
    (seed / ".gitignore").write_text("ignored/\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", _BASE)
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))

    gh = FakeGitHub()

    def read_json(*args: str) -> object:
        assert args[:2] == ("pr", "list")
        return gh.prs

    def run(*args: str) -> None:
        gh.runs.append(args)

    def read_output(*args: str) -> str:
        gh.creates.append(args)
        return "https://github.com/o/r/pull/42"

    def edit_pr_body(pr: str, *, body: str) -> None:
        gh.bodies.append((pr, body))

    def delete_if_exists(endpoint: str) -> bool:
        gh.deletes.append(endpoint)
        return True

    monkeypatch.setattr(github, "read_json", read_json)
    monkeypatch.setattr(github, "run", run)
    monkeypatch.setattr(github, "read_output", read_output)
    monkeypatch.setattr(github, "edit_pr_body", edit_pr_body)
    monkeypatch.setattr(github, "delete_if_exists", delete_if_exists)
    return Env(origin=origin, seed=seed, work=work, gh=gh)


def _pr(number: int, head: str = REFRESH_BRANCH) -> dict[str, object]:
    return {"number": number, "url": f"https://github.com/o/r/pull/{number}", "headRefName": head}


def _refresh(env: Env, work: Path | None = None, hook: str = "echo new > data.txt") -> str:
    root = work or env.work
    ensure_clean_on_base(root, _BASE)
    notes = run_hook(root, hook, root.parent / "report.txt")
    return sync(_REPO, _BASE, root, notes, changed_paths(root))


# -- the outcomes ----------------------------------------------------------------


def test_no_change_no_pr_exits_quietly(env: Env) -> None:
    assert _refresh(env, hook="true") == "nothing"
    assert env.gh.writes == []


def test_no_change_closes_stale_pr(env: Env) -> None:
    env.branch_on_origin(REFRESH_BRANCH, "stale\n")
    env.gh.prs = [_pr(7)]
    assert _refresh(env, hook="true") == "closed"
    assert env.gh.runs == [
        (
            "pr",
            "close",
            "7",
            "--repo",
            _REPO,
            "--comment",
            "Nothing to refresh on develop; closing.",
        )
    ]
    assert env.gh.deletes == [f"repos/{_REPO}/git/refs/heads/{REFRESH_BRANCH}"]


def test_change_opens_pr(env: Env) -> None:
    assert _refresh(env) == "opened"
    tip = env.origin_sha(REFRESH_BRANCH)
    assert _git(env.origin, "log", "-1", "--format=%s", tip) == COMMIT_SUBJECT
    assert _git(env.origin, "rev-list", "--count", f"{_BASE}..{REFRESH_BRANCH}") == "1"
    assert _git(env.origin, "show", f"{tip}:data.txt") == "new"
    (create,) = env.gh.creates
    assert create[:2] == ("pr", "create")
    assert create[create.index("--title") + 1] == COMMIT_SUBJECT
    assert create[create.index("--head") + 1] == REFRESH_BRANCH
    assert create[create.index("--base") + 1] == _BASE
    assert create[create.index("--repo") + 1] == _REPO
    # HEAD and the local base branch never move; the index is left clean.
    assert _git(env.work, "rev-parse", "HEAD") == env.origin_sha(_BASE)
    assert _git(env.work, "diff", "--cached", "--name-only") == ""


def test_change_opens_pr_over_leftover_branch(env: Env) -> None:
    """A refresh branch with no open PR is overwritten under its lease."""
    env.branch_on_origin(REFRESH_BRANCH, "leftover\n")
    assert _refresh(env) == "opened"
    assert _git(env.origin, "show", f"{REFRESH_BRANCH}:data.txt") == "new"


def test_change_refreshes_pr(env: Env) -> None:
    old = env.branch_on_origin(REFRESH_BRANCH, "older\n")
    env.gh.prs = [_pr(5)]
    assert _refresh(env) == "refreshed"
    assert env.origin_sha(REFRESH_BRANCH) != old
    assert _git(env.origin, "show", f"{REFRESH_BRANCH}:data.txt") == "new"
    assert [url for url, _body in env.gh.bodies] == ["https://github.com/o/r/pull/5"]
    assert len(env.gh.comments()) == 1
    assert env.gh.closes() == []
    assert env.gh.creates == []


def test_refresh_when_pr_branch_missing_on_origin(env: Env) -> None:
    env.gh.prs = [_pr(5)]
    assert _refresh(env) == "refreshed"
    assert _git(env.origin, "show", f"{REFRESH_BRANCH}:data.txt") == "new"


def test_open_pr_same_content_no_push(env: Env) -> None:
    before = env.branch_on_origin(REFRESH_BRANCH, "new\n")
    env.gh.prs = [_pr(5)]
    assert _refresh(env) == "unchanged"
    assert env.origin_sha(REFRESH_BRANCH) == before
    assert env.gh.writes == []


def test_superseded_refresh_prs_closed(env: Env) -> None:
    env.branch_on_origin(REFRESH_BRANCH, "older\n")
    env.branch_on_origin(f"{REFRESH_BRANCH}-manual", "manual\n")
    env.gh.prs = [_pr(9, f"{REFRESH_BRANCH}-manual"), _pr(5)]
    assert _refresh(env) == "refreshed"
    assert _git(env.origin, "show", f"{REFRESH_BRANCH}:data.txt") == "new"
    assert [url for url, _body in env.gh.bodies] == ["https://github.com/o/r/pull/5"]
    (close,) = env.gh.closes()
    assert close[2] == "9"
    assert "#5" in close[close.index("--comment") + 1]
    assert env.gh.deletes == [f"repos/{_REPO}/git/refs/heads/{REFRESH_BRANCH}-manual"]


def test_superseded_closed_even_when_unchanged(env: Env) -> None:
    env.branch_on_origin(REFRESH_BRANCH, "new\n")
    env.gh.prs = [_pr(5), _pr(9, f"{REFRESH_BRANCH}-2")]
    assert _refresh(env) == "unchanged"
    assert [c[2] for c in env.gh.closes()] == ["9"]
    assert env.gh.comments() == []


def test_superseded_pr_sharing_survivor_branch_keeps_it(env: Env) -> None:
    """A superseded PR on the survivor's own branch is closed without deleting that branch."""
    env.branch_on_origin(REFRESH_BRANCH, "older\n")
    env.gh.prs = [_pr(5), _pr(9)]
    assert _refresh(env) == "refreshed"
    assert [c[2] for c in env.gh.closes()] == ["9"]
    assert env.gh.deletes == []


# -- shallow (depth-1) checkout, as the ops workflow provides ----------------------


def _shallow_clone(env: Env) -> Path:
    work = env.origin.parent / "shallow"
    _git(env.origin.parent, "clone", "-q", "--depth", "1", f"file://{env.origin}", str(work))
    assert _git(work, "rev-parse", "--is-shallow-repository") == "true"
    return work


def test_shallow_checkout_same_content_unchanged(env: Env) -> None:
    before = env.branch_on_origin(REFRESH_BRANCH, "new\n")
    work = _shallow_clone(env)
    assert REFRESH_BRANCH not in _git(work, "branch", "-r")
    env.gh.prs = [_pr(5)]
    assert _refresh(env, work=work) == "unchanged"
    assert env.origin_sha(REFRESH_BRANCH) == before
    assert env.gh.writes == []
    assert _git(work, "rev-parse", "--is-shallow-repository") == "true"


def test_shallow_checkout_refreshes(env: Env) -> None:
    env.branch_on_origin(REFRESH_BRANCH, "older\n")
    # Advance develop so the existing refresh branch is behind the new base.
    (env.seed / "other.txt").write_text("x\n")
    _git(env.seed, "add", "other.txt")
    _git(env.seed, "commit", "-qm", "advance")
    _git(env.seed, "push", "-q", "origin", _BASE)
    work = _shallow_clone(env)
    env.gh.prs = [_pr(5)]
    assert _refresh(env, work=work) == "refreshed"
    tip = env.origin_sha(REFRESH_BRANCH)
    assert _git(env.origin, "rev-parse", f"{tip}^") == env.origin_sha(_BASE)
    assert _git(work, "rev-parse", "--is-shallow-repository") == "true"


def test_shallow_checkout_opens(env: Env) -> None:
    work = _shallow_clone(env)
    assert _refresh(env, work=work) == "opened"
    assert _git(env.origin, "show", f"{REFRESH_BRANCH}:data.txt") == "new"


# -- hook and changed paths ---------------------------------------------------------


def test_untracked_files_refused(env: Env) -> None:
    run_hook(
        env.work,
        "echo junk > junk.txt && mkdir -p ignored && touch ignored/x",
        env.work.parent / "r",
    )
    with pytest.raises(UpstreamRefreshError, match=r"hook left untracked files: junk\.txt$"):
        changed_paths(env.work)


def test_hook_failure_raises(env: Env) -> None:
    with pytest.raises(UpstreamRefreshError, match=r"failed \(exit 3\): exit 3"):
        run_hook(env.work, "exit 3", env.work.parent / "r")


def test_hook_gets_report_env_and_runs_from_root(env: Env) -> None:
    report = env.work.parent / "report.txt"
    notes = run_hook(env.work, f'pwd > "${REPORT_ENV}"', report)
    assert notes == [str(env.work)]


def test_hook_without_report_has_no_notes(env: Env) -> None:
    assert run_hook(env.work, "true", env.work.parent / "absent.txt") == []


def test_notes_in_body(env: Env) -> None:
    report = env.work.parent / "report.txt"
    notes = run_hook(
        env.work, f'printf "first note\\n\\n  second note  \\n" > "${REPORT_ENV}"', report
    )
    assert notes == ["first note", "second note"]
    body = render_body(notes, ["data.txt"])
    assert "## Notes\n\n- first note\n- second note\n" in body
    assert "## Changed files\n\n- `data.txt`\n" in body
    assert body.startswith("Automated upstream refresh. A human reviews and merges")


def test_body_omits_notes_when_none() -> None:
    assert "## Notes" not in render_body([], ["a.txt"])


def test_changed_paths_rename_and_delete(env: Env) -> None:
    (env.work / "gone.txt").write_text("x\n")
    _git(env.work, "add", "gone.txt")
    _git(env.work, "commit", "-qm", "add gone")
    run_hook(env.work, "git mv data.txt moved.txt && rm gone.txt", env.work.parent / "r")
    assert changed_paths(env.work) == ["data.txt", "gone.txt", "moved.txt"]


def test_changed_paths_none(env: Env) -> None:
    assert changed_paths(env.work) == []


# -- preconditions --------------------------------------------------------------------


def test_requires_clean_tree(env: Env) -> None:
    (env.work / "data.txt").write_text("dirty\n")
    with pytest.raises(UpstreamRefreshError, match="not clean"):
        ensure_clean_on_base(env.work, _BASE)


def test_requires_head_at_base(env: Env) -> None:
    _git(env.work, "commit", "-q", "--allow-empty", "-m", "local")
    with pytest.raises(UpstreamRefreshError, match="is not origin/develop"):
        ensure_clean_on_base(env.work, _BASE)


def test_clean_on_base_returns_sha(env: Env) -> None:
    assert ensure_clean_on_base(env.work, _BASE) == env.origin_sha(_BASE)


# -- PR listing -----------------------------------------------------------------------


def test_open_refresh_prs_filters_and_sorts(env: Env) -> None:
    env.gh.prs = [
        _pr(9, f"{REFRESH_BRANCH}-x"),
        _pr(3, "feature/x"),
        _pr(4, "upstream-refresher"),
        _pr(5),
    ]
    assert open_refresh_prs(_REPO, _BASE) == [
        RefreshPr(5, "https://github.com/o/r/pull/5", REFRESH_BRANCH),
        RefreshPr(9, "https://github.com/o/r/pull/9", f"{REFRESH_BRANCH}-x"),
    ]


@pytest.mark.parametrize("payload", [{"not": "a list"}, ["not a dict"]])
def test_open_refresh_prs_unexpected_json(env: Env, payload: object) -> None:
    env.gh.prs = payload
    with pytest.raises(UpstreamRefreshError, match="unexpected JSON"):
        open_refresh_prs(_REPO, _BASE)
