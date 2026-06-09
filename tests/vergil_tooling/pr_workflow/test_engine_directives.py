"""Tests for engine.directive_for (per-check audit loop)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.registry import check_ids

_NOW = "2026-06-08T00:00:00Z"


def _user_turn_fresh() -> engine.WorkflowState:
    state = engine.init_state(
        issue="1534",
        branch="b",
        base="origin/develop",
        mode="paired",
        head_sha="h0",
        base_sha="b0",
        user_token="u-1",
        now=_NOW,
    )
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)
    return state


def _ready(state: engine.WorkflowState) -> None:
    engine.apply_report_ready(
        state,
        title="t",
        summary="s",
        notes="n",
        linkage="Ref",
        head_sha="h1",
        now=_NOW,
    )


def test_user_init_directive_names_report_ready() -> None:
    directive = engine.directive_for(_user_turn_fresh(), "user")
    assert directive["then"]["verb"] == "report-ready"
    assert "Implement issue #1534" in directive["do"]


def test_audit_directive_returns_first_pending_check_and_range() -> None:
    state = _user_turn_fresh()
    _ready(state)
    directive = engine.directive_for(state, "audit")
    assert directive["then"]["verb"] == "submit-check"
    assert directive["check"] == check_ids()[0]
    assert directive["range"] == "origin/develop..h1"


def test_audit_directive_advances_to_next_pending_check() -> None:
    state = _user_turn_fresh()
    _ready(state)
    engine.apply_check(
        state,
        check_id=check_ids()[0],
        status="pass",
        findings=None,
        reason=None,
        head_sha="h1",
        now=_NOW,
    )
    directive = engine.directive_for(state, "audit")
    assert directive["check"] == check_ids()[1]


def test_user_changes_directive_carries_findings() -> None:
    state = _user_turn_fresh()
    _ready(state)
    finding = {"file": "x.py", "line": 9, "severity": "warning", "note": "doc it"}
    for cid in check_ids():
        if cid == check_ids()[0]:
            engine.apply_check(
                state,
                check_id=cid,
                status="fail",
                findings=[finding],
                reason=None,
                head_sha="h1",
                now=_NOW,
            )
        else:
            engine.apply_check(
                state,
                check_id=cid,
                status="pass",
                findings=None,
                reason=None,
                head_sha="h1",
                now=_NOW,
            )
    directive = engine.directive_for(state, "user")
    assert directive["then"]["verb"] == "report-fixes"
    assert directive["findings"][0]["check"] == check_ids()[0]
    assert directive["findings"][0]["note"] == "doc it"


def test_user_approved_directive_is_done() -> None:
    state = _user_turn_fresh()
    _ready(state)
    for cid in check_ids():
        engine.apply_check(
            state,
            check_id=cid,
            status="pass",
            findings=None,
            reason=None,
            head_sha="h1",
            now=_NOW,
        )
    directive = engine.directive_for(state, "user")
    assert directive["done"] is True
    assert directive["reason"] == "approved"


def test_directive_rejects_unknown_role() -> None:
    with pytest.raises(WorkflowError, match="unknown role"):
        engine.directive_for(_user_turn_fresh(), "robot")


def test_user_directive_raises_for_unexpected_status() -> None:
    state = _user_turn_fresh()
    _ready(state)  # pr_metadata set, status "reviewing", owner audit
    with pytest.raises(WorkflowError, match="no user directive"):
        engine.directive_for(state, "user")
