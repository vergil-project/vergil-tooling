"""Tests for the post-report-ready freeze predicate + message builder (#2346)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.pr_workflow import engine, freeze
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _ready() -> WorkflowState:
    state = engine.init_state(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        head_sha="aaaaaaaa1111",
        base_sha="0000",
        now="2026-06-25T00:00:00Z",
    )
    return engine.apply_report_ready(
        state,
        title="t",
        summary="s",
        notes="n",
        linkage="Ref",
        head_sha="aaaaaaaa1111",
        now="2026-06-25T01:00:00Z",
    )


# -- is_frozen ---------------------------------------------------------------


def test_is_frozen_none() -> None:
    assert freeze.is_frozen(None) is False


def test_is_frozen_implementing() -> None:
    state = engine.init_state(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        head_sha="aaaa",
        base_sha="0000",
        now="2026-06-25T00:00:00Z",
    )
    assert freeze.is_frozen(state) is False


def test_is_frozen_ready() -> None:
    assert freeze.is_frozen(_ready()) is True


def test_is_frozen_false_after_submit() -> None:
    state = _ready()
    engine.apply_submitted(
        state, pr_url="https://github.com/o/r/pull/7", pr_number=7, now="2026-06-25T02:00:00Z"
    )
    assert freeze.is_frozen(state) is False


def test_is_frozen_false_after_unfreeze() -> None:
    state = _ready()
    engine.apply_unfreeze(state, now="2026-06-25T02:00:00Z")
    assert freeze.is_frozen(state) is False


# -- has_drifted / build_refusal ---------------------------------------------


def test_has_drifted_true_when_head_moved() -> None:
    assert freeze.has_drifted(_ready(), "bbbbbbbb2222") is True


def test_has_drifted_false_when_head_matches() -> None:
    assert freeze.has_drifted(_ready(), "aaaaaaaa1111") is False


def test_has_drifted_false_when_head_unknown() -> None:
    assert freeze.has_drifted(_ready(), None) is False


def test_build_refusal_points_at_followup_and_unfreeze() -> None:
    msg = freeze.build_refusal(_ready(), action="add a commit", current_head="aaaaaaaa1111")
    assert "FROZEN" in msg
    assert "#42" in msg
    assert "vrg-issue-create" in msg
    assert "vrg-pr-workflow unfreeze" in msg
    assert "report-ready" in msg
    # No drift when HEAD still matches the reported commit.
    assert "DRIFT" not in msg


def test_build_refusal_flags_drift_loudly() -> None:
    msg = freeze.build_refusal(
        _ready(), action="advance this branch with a push", current_head="bbbbbbbb2222"
    )
    assert "DRIFT DETECTED" in msg
    assert "bbbbbbbb" in msg
    assert "aaaaaaaa" in msg
    assert "1719" in msg


# -- check_worktree (I/O) ----------------------------------------------------


def _write_state(tmp_path: Path, state: WorkflowState) -> None:
    LocalFileTransport(tmp_path).write(state)


def test_check_worktree_no_file(tmp_path: Path) -> None:
    check = freeze.check_worktree(tmp_path, action="add a commit")
    assert check.frozen is False
    assert check.read_error is None


def test_check_worktree_frozen(tmp_path: Path) -> None:
    _write_state(tmp_path, _ready())
    check = freeze.check_worktree(tmp_path, action="add a commit")
    assert check.frozen is True
    assert check.message is not None
    assert "FROZEN" in check.message


def test_check_worktree_not_frozen_when_implementing(tmp_path: Path) -> None:
    state = _ready()
    engine.apply_unfreeze(state, now="2026-06-25T02:00:00Z")
    _write_state(tmp_path, state)
    check = freeze.check_worktree(tmp_path, action="add a commit")
    assert check.frozen is False


def test_check_worktree_captures_read_error(tmp_path: Path) -> None:
    (tmp_path / ".vergil").mkdir()
    (tmp_path / ".vergil" / "pr-workflow.json").write_text("{ not json")
    check = freeze.check_worktree(tmp_path, action="add a commit")
    assert check.frozen is False
    assert check.read_error is not None


# -- at_reported_commit / publishable_branch (#3071) --------------------------


def test_at_reported_commit_true_when_head_matches() -> None:
    assert freeze.at_reported_commit(_ready(), "aaaaaaaa1111") is True


def test_at_reported_commit_false_when_drifted() -> None:
    assert freeze.at_reported_commit(_ready(), "bbbbbbbb2222") is False


def test_at_reported_commit_false_when_head_unknown() -> None:
    assert freeze.at_reported_commit(_ready(), None) is False


def test_at_reported_commit_false_when_head_sha_missing() -> None:
    # A missing recorded head_sha must never read as "at the reported commit".
    state = _ready()
    state.git.pop("head_sha", None)
    assert freeze.at_reported_commit(state, "aaaaaaaa1111") is False


def test_check_worktree_publishable_at_reported_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(tmp_path, _ready())
    monkeypatch.setattr(freeze, "_current_head", lambda _root: "aaaaaaaa1111")
    monkeypatch.setattr(freeze, "_current_branch", lambda _root: "feature/42-x")
    check = freeze.check_worktree(tmp_path, action="advance this branch with a push")
    assert check.frozen is True
    assert check.drifted is False
    assert check.publishable_branch == "feature/42-x"


def test_check_worktree_not_publishable_when_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_state(tmp_path, _ready())
    monkeypatch.setattr(freeze, "_current_head", lambda _root: "bbbbbbbb2222")
    monkeypatch.setattr(freeze, "_current_branch", lambda _root: "feature/42-x")
    check = freeze.check_worktree(tmp_path, action="advance this branch with a push")
    assert check.frozen is True
    assert check.drifted is True
    assert check.publishable_branch is None


def test_check_worktree_not_publishable_when_head_sha_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _ready()
    state.git.pop("head_sha", None)
    _write_state(tmp_path, state)
    monkeypatch.setattr(freeze, "_current_head", lambda _root: "aaaaaaaa1111")
    monkeypatch.setattr(freeze, "_current_branch", lambda _root: "feature/42-x")
    check = freeze.check_worktree(tmp_path, action="advance this branch with a push")
    assert check.frozen is True
    assert check.publishable_branch is None


def test_check_worktree_not_publishable_on_other_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same commit, but a different (or detached) checkout: not the frozen branch.
    _write_state(tmp_path, _ready())
    monkeypatch.setattr(freeze, "_current_head", lambda _root: "aaaaaaaa1111")
    monkeypatch.setattr(freeze, "_current_branch", lambda _root: "HEAD")
    check = freeze.check_worktree(tmp_path, action="advance this branch with a push")
    assert check.frozen is True
    assert check.publishable_branch is None


def test_check_worktree_not_publishable_when_branch_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # tmp_path is not a git repo, so the real _current_branch resolves to None.
    _write_state(tmp_path, _ready())
    monkeypatch.setattr(freeze, "_current_head", lambda _root: "aaaaaaaa1111")
    check = freeze.check_worktree(tmp_path, action="advance this branch with a push")
    assert check.frozen is True
    assert check.publishable_branch is None
