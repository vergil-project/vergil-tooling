from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError


def test_context_defaults() -> None:
    ctx = UpdateDepsContext(repo="owner/repo", repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.branch is None
    assert ctx.worktree_path is None
    assert ctx.pr_url is None
    assert ctx.any_changes is False
    assert ctx.results == []


def test_update_deps_error_carries_fields() -> None:
    err = UpdateDepsError(phase="preflight", command="git status", message="dirty", detail="x")
    assert err.phase == "preflight"
    assert err.command == "git status"
    assert err.detail == "x"
    assert str(err) == "dirty"
