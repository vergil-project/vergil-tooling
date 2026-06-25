"""Tests for apply_report_ready / apply_submitted run-and-done semantics (#1872)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _fresh() -> WorkflowState:
    return engine.init_state(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        head_sha="bbb",
        base_sha="aaa",
        now="2026-06-25T00:00:00Z",
    )


def test_report_ready_records_metadata_and_marks_ready() -> None:
    state = _fresh()
    engine.apply_report_ready(
        state,
        title="t",
        summary="s",
        notes="n",
        linkage="Ref",
        head_sha="ccc",
        now="2026-06-25T01:00:00Z",
    )
    assert state.status == "ready"
    assert state.pr_metadata == {"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"}
    assert state.git["head_sha"] == "ccc"
    assert state.updated_at == "2026-06-25T01:00:00Z"


def test_report_ready_is_idempotent_and_overwrites() -> None:
    state = _fresh()
    engine.apply_report_ready(
        state,
        title="t1",
        summary="s1",
        notes="n1",
        linkage="Ref",
        head_sha="ccc",
        now="2026-06-25T01:00:00Z",
    )
    engine.apply_report_ready(
        state,
        title="t2",
        summary="s2",
        notes="n2",
        linkage="Ref",
        head_sha="ddd",
        now="2026-06-25T02:00:00Z",
    )
    assert state.pr_metadata == {"title": "t2", "summary": "s2", "notes": "n2", "linkage": "Ref"}
    assert state.git["head_sha"] == "ddd"


def test_report_ready_rejects_autoclose_keyword() -> None:
    state = _fresh()
    with pytest.raises(WorkflowError, match="auto-close keyword"):
        engine.apply_report_ready(
            state,
            title="t",
            summary="Closes #42",
            notes="n",
            linkage="Ref",
            head_sha="ccc",
            now="2026-06-25T01:00:00Z",
        )


def test_apply_submitted_records_marker() -> None:
    state = _fresh()
    engine.apply_submitted(
        state,
        pr_url="https://github.com/o/r/pull/7",
        pr_number=7,
        now="2026-06-25T03:00:00Z",
    )
    assert state.submitted == {
        "pr_url": "https://github.com/o/r/pull/7",
        "pr_number": 7,
        "at": "2026-06-25T03:00:00Z",
    }


def test_reject_autoclose_skips_none_values() -> None:
    """_reject_autoclose must not crash when a field value is None."""
    # Access via the module to exercise the None-skip branch (line coverage).
    from vergil_tooling.lib.pr_workflow.engine import _reject_autoclose  # noqa: PLC2701

    _reject_autoclose("test-verb", title=None, summary=None)  # no raise
