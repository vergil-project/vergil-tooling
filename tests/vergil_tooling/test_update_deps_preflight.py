from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vergil_tooling.lib.branch_sweep import PullRequest
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.preflight import preflight

_MOD = "vergil_tooling.lib.update_deps.preflight"


def _sync_ok(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(
        _MOD + ".git.read_output",
        lambda *a: "deadbeef" if "rev-parse" in a else "",
    )
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    monkeypatch.setattr(_MOD + ".version.show", lambda root: "2.1.243")
    monkeypatch.setattr(_MOD + ".git.ref_exists", lambda ref: False)
    monkeypatch.setattr(_MOD + ".git.remote_branches", lambda: {"develop", "main"})


def test_preflight_creates_worktree_and_chdirs(monkeypatch) -> None:
    _sync_ok(monkeypatch)
    chdirs: list[Path] = []
    made: dict[str, object] = {}
    wt_path = Path("/tmp/repo/.worktrees/deps-update-2.1.243")  # noqa: S108

    def _fake_create(root, *, branch, base):  # noqa: ARG001
        made.update(branch=branch, base=base)
        return wt_path

    monkeypatch.setattr(_MOD + ".create_worktree", _fake_create)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(Path(p)))
    ctx = preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.repo == "owner/repo"
    assert ctx.branch == "deps/update-2.1.243"
    assert ctx.version == "2.1.243"
    assert made == {"branch": "deps/update-2.1.243", "base": "develop"}
    assert ctx.worktree_path == wt_path
    assert chdirs == [wt_path]


def test_preflight_wraps_worktree_error(monkeypatch) -> None:
    """A ManagedWorktreeError from the shared helper is translated into the
    pipeline's own UpdateDepsError at the preflight boundary."""
    from vergil_tooling.lib.managed_worktree import ManagedWorktreeError

    _sync_ok(monkeypatch)

    def _raise(root, *, branch, base):  # noqa: ARG001
        raise ManagedWorktreeError("path already exists")

    monkeypatch.setattr(_MOD + ".create_worktree", _raise)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: None)
    with pytest.raises(UpdateDepsError, match="path already exists"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_existing_deps_branch_fails_loud(monkeypatch) -> None:
    """A second off-cycle run on the same develop version must not reuse or
    clobber the in-flight deps/update-<version> branch (Review Focus 2)."""
    from vergil_tooling.lib.managed_worktree import ManagedWorktreeError

    _sync_ok(monkeypatch)

    def collide(root, *, branch, base):  # noqa: ARG001
        msg = f"branch {branch} already exists"
        raise ManagedWorktreeError(msg)

    monkeypatch.setattr(_MOD + ".create_worktree", collide)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: None)
    with pytest.raises(UpdateDepsError, match=r"deps/update-2\.1\.243 already exists") as info:
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert "dependency-update PR" in str(info.value)


def test_preflight_existing_local_deps_branch_fails_loud(monkeypatch) -> None:
    _sync_ok(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(_MOD + ".git.ref_exists", lambda ref: seen.append(ref) or True)
    monkeypatch.setattr(_MOD + ".branch_sweep.list_prs", lambda repo, branch: [])
    monkeypatch.setattr(_MOD + ".create_worktree", lambda *a, **kw: pytest.fail("created"))
    with pytest.raises(UpdateDepsError, match=r"deps/update-2\.1\.243 already exists"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert seen == ["refs/heads/deps/update-2.1.243"]


def test_preflight_deps_branch_on_origin_fails_loud(monkeypatch) -> None:
    """A failed run deletes its local branch; the PR head survives on origin."""
    _sync_ok(monkeypatch)
    monkeypatch.setattr(_MOD + ".git.remote_branches", lambda: {"develop", "deps/update-2.1.243"})
    monkeypatch.setattr(_MOD + ".branch_sweep.list_prs", lambda repo, branch: [_pr("OPEN")])
    monkeypatch.setattr(_MOD + ".create_worktree", lambda *a, **kw: pytest.fail("created"))
    with pytest.raises(UpdateDepsError, match=r"deps/update-2\.1\.243 already exists"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


# --- an existing deps/update-<v> branch whose PR finished (#3330) -------------

_BRANCH = "deps/update-2.1.243"
_HEAD = "a" * 40


def _pr(state: str, number: int = 7, head: str = _HEAD) -> PullRequest:
    return PullRequest(number=number, state=state, head_oid=head)


def _existing(
    monkeypatch,
    prs: list[PullRequest],
    *,
    local: bool = False,
    remote: bool = True,
    local_tip: str = _HEAD,
    remote_tip: str = _HEAD,
) -> dict[str, list]:
    """Patch preflight for a deps branch present locally and/or on origin."""
    _sync_ok(monkeypatch)
    calls: dict[str, list] = {"gh": [], "git": [], "created": []}
    monkeypatch.setattr(_MOD + ".git.ref_exists", lambda ref: local)
    monkeypatch.setattr(
        _MOD + ".git.remote_branches",
        lambda: {"develop", _BRANCH} if remote else {"develop"},
    )

    def _read(*a: str) -> str:
        if a == ("rev-parse", f"refs/heads/{_BRANCH}"):
            return local_tip
        return "deadbeef" if "rev-parse" in a else ""

    monkeypatch.setattr(_MOD + ".git.read_output", _read)
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: calls["git"].append(a))
    monkeypatch.setattr(_MOD + ".branch_sweep.list_prs", lambda repo, branch: prs)
    monkeypatch.setattr(_MOD + ".branch_sweep.current_tip", lambda repo, branch: remote_tip)
    monkeypatch.setattr(_MOD + ".github.run", lambda *a: calls["gh"].append(a))

    def _create(root, *, branch, base):  # noqa: ARG001
        calls["created"].append(branch)
        return Path("/tmp/repo/.worktrees/deps-update-2.1.243")  # noqa: S108

    monkeypatch.setattr(_MOD + ".create_worktree", _create)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: None)
    return calls


def test_preflight_merged_pr_reports_done(monkeypatch, capsys) -> None:
    """A branch left on origin after its PR merged: the update is done (exit 0)."""
    calls = _existing(monkeypatch, [_pr("CLOSED", 3, "b" * 40), _pr("MERGED", 7)])
    ctx = preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.landed_pr_url == "https://github.com/owner/repo/pull/7"
    assert ctx.version == "2.1.243"
    assert ctx.branch == _BRANCH
    assert ctx.worktree_path is None
    assert calls["created"] == []
    assert calls["gh"] == []
    out = capsys.readouterr().out
    assert "already merged in https://github.com/owner/repo/pull/7" in out


def test_preflight_merged_pr_with_local_branch_reports_done(monkeypatch) -> None:
    calls = _existing(monkeypatch, [_pr("MERGED")], local=True, remote=False)
    ctx = preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.landed_pr_url == "https://github.com/owner/repo/pull/7"
    assert calls["git"] == [("fetch", "origin", "develop")]


def test_preflight_closed_pr_deletes_stale_branch_and_continues(monkeypatch, capsys) -> None:
    calls = _existing(monkeypatch, [_pr("CLOSED")], local=True, remote=True)
    ctx = preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.landed_pr_url is None
    assert calls["gh"] == [("api", "-X", "DELETE", f"repos/owner/repo/git/refs/heads/{_BRANCH}")]
    assert ("branch", "-D", _BRANCH) in calls["git"]
    assert calls["created"] == [_BRANCH]
    out = capsys.readouterr().out
    assert f"Deleted stale branch {_BRANCH} on origin" in out
    assert f"Deleted stale local branch {_BRANCH}" in out


def test_preflight_closed_pr_remote_only_skips_local_delete(monkeypatch) -> None:
    calls = _existing(monkeypatch, [_pr("CLOSED")])
    preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert len(calls["gh"]) == 1
    assert ("branch", "-D", _BRANCH) not in calls["git"]
    assert calls["created"] == [_BRANCH]


def test_preflight_closed_pr_local_only_skips_remote_delete(monkeypatch) -> None:
    calls = _existing(monkeypatch, [_pr("CLOSED")], local=True, remote=False)
    preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert calls["gh"] == []
    assert ("branch", "-D", _BRANCH) in calls["git"]


def test_preflight_closed_pr_remote_delete_failure_refuses(monkeypatch) -> None:
    calls = _existing(monkeypatch, [_pr("CLOSED")])

    def _fail(*a: str) -> None:
        raise subprocess.CalledProcessError(1, ["gh", *a], stderr="HTTP 403")

    monkeypatch.setattr(_MOD + ".github.run", _fail)
    with pytest.raises(UpdateDepsError, match="deleting it on origin failed") as info:
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert info.value.command == f"git push origin --delete {_BRANCH}"
    assert calls["created"] == []


def test_preflight_closed_pr_local_delete_failure_refuses(monkeypatch) -> None:
    calls = _existing(monkeypatch, [_pr("CLOSED")], local=True, remote=False)

    def _git(*a: str) -> None:
        if a[0] == "branch":
            raise subprocess.CalledProcessError(1, ["git", *a])

    monkeypatch.setattr(_MOD + ".git.run", _git)
    with pytest.raises(UpdateDepsError, match="deleting the local branch failed"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert calls["created"] == []


def test_preflight_open_pr_refuses_in_flight(monkeypatch) -> None:
    calls = _existing(monkeypatch, [_pr("MERGED", 3), _pr("OPEN", 9)])
    with pytest.raises(UpdateDepsError, match=r"open PR #9\)\. An earlier dependency update"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert calls["gh"] == []
    assert calls["created"] == []


def test_preflight_no_pr_refuses_in_flight(monkeypatch) -> None:
    calls = _existing(monkeypatch, [])
    with pytest.raises(UpdateDepsError, match="it has no PR"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert calls["gh"] == []
    assert calls["created"] == []


def test_preflight_unexpected_pr_state_refuses(monkeypatch) -> None:
    _existing(monkeypatch, [_pr("DRAFTY")])
    with pytest.raises(UpdateDepsError, match="unexpected state 'DRAFTY'"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


@pytest.mark.parametrize(
    ("local", "remote", "local_tip", "remote_tip"),
    [
        (False, True, _HEAD, "c" * 40),
        (True, False, "c" * 40, _HEAD),
    ],
)
def test_preflight_reused_branch_tip_refuses(
    monkeypatch, local, remote, local_tip, remote_tip
) -> None:
    """A tip that is not the last PR's head holds commits no PR covers: never delete it."""
    calls = _existing(
        monkeypatch,
        [_pr("CLOSED")],
        local=local,
        remote=remote,
        local_tip=local_tip,
        remote_tip=remote_tip,
    )
    with pytest.raises(UpdateDepsError, match="is not the head of its last PR #7"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert calls["gh"] == []
    assert ("branch", "-D", _BRANCH) not in calls["git"]


@pytest.mark.parametrize(
    "error",
    [subprocess.CalledProcessError(1, ["gh"]), ValueError("bad response")],
)
def test_preflight_pr_lookup_failure_refuses(monkeypatch, error) -> None:
    _existing(monkeypatch, [])

    def _raise(repo: str, branch: str) -> list[PullRequest]:
        raise error

    monkeypatch.setattr(_MOD + ".branch_sweep.list_prs", _raise)
    with pytest.raises(UpdateDepsError, match="PRs could not be listed"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_non_develop(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "feature/x")
    with pytest.raises(UpdateDepsError, match="Must be on develop"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_dirty_tree(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: " M file.py")
    with pytest.raises(UpdateDepsError, match="not clean"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_out_of_sync_develop(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    outs = iter(["", "local-sha", "remote-sha"])  # status, HEAD, origin/develop
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: next(outs))
    with pytest.raises(UpdateDepsError, match="sync"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
