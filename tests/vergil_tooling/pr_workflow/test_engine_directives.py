"""Tests for engine.directive_for."""

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


def _all_checks(status: str) -> list[dict]:
    return [{"id": cid, "status": status} for cid in check_ids()]


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


def test_audit_directive_lists_all_checks_and_the_range() -> None:
    state = _user_turn_fresh()
    _ready(state)
    directive = engine.directive_for(state, "audit")
    assert directive["then"]["verb"] == "submit-review"
    assert directive["checks"] == list(check_ids())
    assert directive["range"] == "origin/develop..h1"


def test_user_changes_directive_carries_findings() -> None:
    state = _user_turn_fresh()
    _ready(state)
    checks = _all_checks("pass")
    checks[0] = {
        "id": checks[0]["id"],
        "status": "fail",
        "findings": [{"file": "x.py", "line": 9, "severity": "warning", "note": "doc it"}],
    }
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    directive = engine.directive_for(state, "user")
    assert directive["then"]["verb"] == "report-fixes"
    assert directive["findings"][0]["check"] == checks[0]["id"]
    assert directive["findings"][0]["note"] == "doc it"


def test_user_approved_directive_is_done() -> None:
    state = _user_turn_fresh()
    _ready(state)
    engine.apply_review(state, checks=_all_checks("pass"), head_sha="h1", now=_NOW)
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
