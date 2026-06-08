"""Tests for engine report/review/rollup/escalate/resolve transitions."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.registry import check_ids

_NOW = "2026-06-08T00:00:00Z"


def _paired_owned_by_user() -> engine.WorkflowState:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)  # owner -> user
    return state


def _all_checks(status: str) -> list[dict]:
    return [{"id": cid, "status": status} for cid in check_ids()]


def test_report_ready_paired_hands_to_audit() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(
        state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
    )
    assert state.owner == "audit"
    assert state.status == "reviewing"
    assert state.pr_metadata == {"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"}
    assert state.git["head_sha"] == "h1"


def test_report_ready_solo_goes_straight_to_approved() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="solo",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    engine.apply_report_ready(
        state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
    )
    assert state.status == "approved"
    assert state.owner == "user"


def test_report_ready_rejects_out_of_turn() -> None:
    state = _paired_owned_by_user()
    state.owner = "audit"
    with pytest.raises(WorkflowError, match="out-of-turn"):
        engine.apply_report_ready(
            state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
        )


def test_review_all_pass_approves_and_hands_to_user() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    engine.apply_review(state, checks=_all_checks("pass"), head_sha="h1", now=_NOW)
    assert state.status == "approved"
    assert state.owner == "user"
    assert state.git["last_reviewed_sha"] == "h1"


def test_review_with_a_fail_requests_changes() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    assert state.status == "changes-requested"
    assert state.owner == "user"


def test_review_with_an_escalate_goes_to_human() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[1] = {"id": checks[1]["id"], "status": "escalate", "reason": "needs a human"}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    assert state.status == "escalated"
    assert state.owner == "human"
    assert state.escalation["check"] == checks[1]["id"]


def test_review_rejects_unknown_check_id() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass") + [{"id": "made-up", "status": "pass"}]
    with pytest.raises(WorkflowError, match="unknown check"):
        engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)


def test_review_rejects_missing_check() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")[:-1]  # drop one
    with pytest.raises(WorkflowError, match="missing checks"):
        engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)


def test_report_fixes_requires_new_commits() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)  # owner -> user, last_reviewed = h1
    with pytest.raises(WorkflowError, match="no new commits"):
        engine.apply_report_fixes(state, head_sha="h1", note=None, now=_NOW)


def test_report_fixes_bumps_round_and_hands_to_audit() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    engine.apply_report_fixes(state, head_sha="h2", note="addressed", now=_NOW)
    assert state.round == 1
    assert state.owner == "audit"
    assert state.git["head_sha"] == "h2"


def test_report_fixes_escalates_when_round_cap_exceeded() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    state.round = 1  # already used the one permitted fix round (max_rounds=1)
    engine.apply_report_fixes(state, head_sha="h2", note=None, now=_NOW, max_rounds=1)
    assert state.round == 2
    assert state.owner == "human"
    assert state.status == "escalated"
    assert "runaway-round cap" in state.escalation["reason"]


def test_apply_error_records_terminal_error() -> None:
    state = _paired_owned_by_user()
    engine.apply_error(state, by="audit", reason="cannot proceed", now=_NOW)
    assert state.status == "error"
    assert state.error == {"by": "audit", "at": _NOW, "reason": "cannot proceed"}
    assert state.history[-1]["action"] == "abort"


def test_escalate_hands_to_human() -> None:
    state = _paired_owned_by_user()
    engine.apply_escalate(state, by="user", reason="stuck", now=_NOW)
    assert state.owner == "human"
    assert state.status == "escalated"
    assert state.escalation["reason"] == "stuck"


def test_resolve_requires_human_owner_and_hands_back() -> None:
    state = _paired_owned_by_user()
    engine.apply_escalate(state, by="user", reason="stuck", now=_NOW)
    engine.apply_resolve(state, to_role="user", note="ok go", now=_NOW)
    assert state.owner == "user"
    assert state.escalation is None


def test_resolve_rejected_when_not_escalated() -> None:
    state = _paired_owned_by_user()
    with pytest.raises(WorkflowError, match="not awaiting the human"):
        engine.apply_resolve(state, to_role="user", note=None, now=_NOW)
