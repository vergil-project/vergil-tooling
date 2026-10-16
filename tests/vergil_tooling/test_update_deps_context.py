from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import (
    DEPS_LABEL,
    DepsFailure,
    FailureKind,
    UpdateDepsContext,
    UpdateDepsError,
)


def test_context_defaults() -> None:
    ctx = UpdateDepsContext(repo="owner/repo", repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.branch is None
    assert ctx.worktree_path is None
    assert ctx.pr_url is None
    assert ctx.any_changes is False
    assert ctx.results == []
    assert ctx.vergil_bump is None


def test_update_deps_error_carries_fields() -> None:
    err = UpdateDepsError(phase="preflight", command="git status", message="dirty", detail="x")
    assert err.phase == "preflight"
    assert err.command == "git status"
    assert err.detail == "x"
    assert str(err) == "dirty"


def test_context_new_fields_default_to_none() -> None:
    ctx = UpdateDepsContext(repo="owner/repo", repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.version is None
    assert ctx.failure is None
    assert ctx.fix_issue_url is None
    assert DEPS_LABEL == "dependency-update"


@pytest.mark.parametrize(
    ("any_changes", "kind", "expected"),
    [
        (False, None, False),
        (True, None, True),
        (True, "validation", True),
        (True, "merge", True),
        (True, "update", False),
    ],
)
def test_has_work(any_changes: bool, kind: FailureKind | None, expected: bool) -> None:  # noqa: FBT001
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"), any_changes=any_changes)  # noqa: S108
    if kind is not None:
        ctx.failure = DepsFailure(kind=kind, message="m", output_tail="", reproduce="r")
    assert ctx.has_work is expected
