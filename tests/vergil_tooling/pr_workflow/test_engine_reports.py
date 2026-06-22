"""Tests for engine report/review/rollup/escalate/resolve transitions."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.registry import check_ids

_NOW = "2026-06-08T00:00:00Z"
_FINDING = {"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}


def _paired_owned_by_user() -> engine.WorkflowState:
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
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)  # owner -> user
    return state


def _ready(state: engine.WorkflowState, head_sha: str = "h1") -> None:
    engine.apply_report_ready(
        state,
        title="t",
        summary="s",
        notes="n",
        linkage="Ref",
        head_sha=head_sha,
        now=_NOW,
    )


def _run_review(
    state: engine.WorkflowState,
    *,
    fail: str | None = None,
    escalate: str | None = None,
    head_sha: str = "h1",
) -> None:
    """Drive a full per-check review round; all pass unless one is fail/escalate."""
    for cid in check_ids():
        if cid == fail:
            engine.apply_check(
                state,
                check_id=cid,
                status="fail",
                findings=[dict(_FINDING)],
                reason=None,
                head_sha=head_sha,
                now=_NOW,
            )
        elif cid == escalate:
            engine.apply_check(
                state,
                check_id=cid,
                status="escalate",
                findings=None,
                reason="needs a human",
                head_sha=head_sha,
                now=_NOW,
            )
        else:
            engine.apply_check(
                state,
                check_id=cid,
                status="pass",
                findings=None,
                reason=None,
                head_sha=head_sha,
                now=_NOW,
            )


def test_apply_submitted_records_the_pr_marker() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    engine.apply_submitted(
        state,
        pr_url="https://github.com/o/r/pull/312",
        pr_number=312,
        now="2026-06-08T01:00:00Z",
    )
    assert state.submitted == {
        "pr_url": "https://github.com/o/r/pull/312",
        "pr_number": 312,
        "at": "2026-06-08T01:00:00Z",
    }
    assert state.updated_at == "2026-06-08T01:00:00Z"
    assert state.history[-1]["action"] == "submitted"


def test_report_ready_paired_hands_to_audit() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    assert state.owner == "audit"
    assert state.status == "reviewing"
    assert state.pr_metadata == {"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"}
    assert state.git["head_sha"] == "h1"


def test_report_ready_solo_goes_straight_to_approved() -> None:
    state = engine.init_state(
        issue="1534",
        branch="b",
        base="origin/develop",
        mode="solo",
        head_sha="h0",
        base_sha="b0",
        user_token="u-1",
        now=_NOW,
    )
    _ready(state)
    assert state.status == "approved"
    assert state.owner == "user"


def test_report_ready_rejects_out_of_turn() -> None:
    state = _paired_owned_by_user()
    state.owner = "audit"
    with pytest.raises(WorkflowError, match="out-of-turn"):
        _ready(state)


@pytest.mark.parametrize("bad", ["Closes #1", "Fixes #2", "Resolves #3"])
def test_report_ready_rejects_autoclose_in_notes(bad: str) -> None:
    state = _paired_owned_by_user()
    with pytest.raises(WorkflowError, match=r"--notes contains an auto-close keyword"):
        engine.apply_report_ready(
            state, title="t", summary="s", notes=bad, linkage="Ref", head_sha="h1", now=_NOW
        )
    # Rejected at entry: no state was written.
    assert state.pr_metadata is None
    assert state.owner == "user"


@pytest.mark.parametrize("field", ["title", "summary", "notes"])
def test_report_ready_rejects_autoclose_in_any_field(field: str) -> None:
    state = _paired_owned_by_user()
    kwargs = {"title": "t", "summary": "s", "notes": "n"}
    kwargs[field] = "Closes #299"
    with pytest.raises(WorkflowError, match=rf"--{field} contains an auto-close keyword"):
        engine.apply_report_ready(state, linkage="Ref", head_sha="h1", now=_NOW, **kwargs)


@pytest.mark.parametrize(
    "ok",
    ["Ref #1", "fix(rdqm): right-size matrices (#300)", "see #287", "the fix touches #287 files"],
)
def test_report_ready_accepts_safe_fields(ok: str) -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(
        state, title=ok, summary=ok, notes=ok, linkage="Ref", head_sha="h1", now=_NOW
    )
    assert state.pr_metadata == {"title": ok, "summary": ok, "notes": ok, "linkage": "Ref"}


def test_report_fixes_rejects_autoclose_revision() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, fail=check_ids()[0])  # owner user, last_reviewed h1
    with pytest.raises(WorkflowError, match=r"report-fixes: --notes contains an auto-close"):
        engine.apply_report_fixes(state, head_sha="h2", note=None, now=_NOW, notes="Fixes #2")
    # Rejected at entry: the round was not bumped and the metadata is untouched.
    assert state.round == 0
    assert state.pr_metadata == {"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"}


def test_review_all_pass_approves_and_hands_to_user() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state)
    assert state.status == "approved"
    assert state.owner == "user"
    assert state.git["last_reviewed_sha"] == "h1"


def test_partial_round_stays_with_audit() -> None:
    state = _paired_owned_by_user()
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
    assert state.owner == "audit"  # round not complete
    assert state.status == "reviewing"
    assert engine.next_pending_check(state) == check_ids()[1]


def test_review_with_a_fail_requests_changes() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, fail=check_ids()[0])
    assert state.status == "changes-requested"
    assert state.owner == "user"


def test_review_with_an_escalate_goes_to_human() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, escalate=check_ids()[1])
    assert state.status == "escalated"
    assert state.owner == "human"
    esc = state.escalation
    assert esc is not None
    assert esc["check"] == check_ids()[1]


def test_apply_check_rejects_unknown_check_id() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    with pytest.raises(WorkflowError, match="unknown check"):
        engine.apply_check(
            state,
            check_id="made-up",
            status="pass",
            findings=None,
            reason=None,
            head_sha="h1",
            now=_NOW,
        )


def test_apply_check_rejects_invalid_status() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    with pytest.raises(WorkflowError, match="invalid status"):
        engine.apply_check(
            state,
            check_id=check_ids()[0],
            status="bogus",
            findings=None,
            reason=None,
            head_sha="h1",
            now=_NOW,
        )


def test_apply_check_rejects_out_of_turn() -> None:
    state = _paired_owned_by_user()  # owner user, not audit
    with pytest.raises(WorkflowError, match="out-of-turn"):
        engine.apply_check(
            state,
            check_id=check_ids()[0],
            status="pass",
            findings=None,
            reason=None,
            head_sha="h1",
            now=_NOW,
        )


def test_next_pending_check_is_none_once_round_complete() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state)
    assert engine.next_pending_check(state) is None


def test_report_fixes_requires_new_commits() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, fail=check_ids()[0])  # owner user, last_reviewed h1
    with pytest.raises(WorkflowError, match="no new commits"):
        engine.apply_report_fixes(state, head_sha="h1", note=None, now=_NOW)


def test_report_fixes_bumps_round_and_reopens_all_checks() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, fail=check_ids()[0])
    engine.apply_report_fixes(state, head_sha="h2", note="addressed", now=_NOW)
    assert state.round == 1
    assert state.owner == "audit"
    assert state.git["head_sha"] == "h2"
    assert engine.next_pending_check(state) == check_ids()[0]  # all reopen for the new round


def test_report_fixes_accepts_metadata_only_round() -> None:
    state = _paired_owned_by_user()
    _ready(state)  # pr_metadata {title t, summary s, notes n, linkage Ref}
    _run_review(state, fail=check_ids()[0])  # owner user, last_reviewed h1
    # No new commit (head still h1), but the summary is revised: a valid round.
    engine.apply_report_fixes(
        state, head_sha="h1", note="reworded summary", now=_NOW, summary="sharper summary"
    )
    assert state.round == 1
    assert state.owner == "audit"
    assert state.status == "reviewing"
    assert state.pr_metadata == {
        "title": "t",
        "summary": "sharper summary",
        "notes": "n",
        "linkage": "Ref",
    }
    assert state.history[-1]["revised"] == ["summary"]


def test_report_fixes_revises_multiple_metadata_fields() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, fail=check_ids()[0])
    engine.apply_report_fixes(
        state, head_sha="h1", note=None, now=_NOW, title="new title", notes="new notes"
    )
    assert state.pr_metadata == {
        "title": "new title",
        "summary": "s",
        "notes": "new notes",
        "linkage": "Ref",
    }
    assert state.history[-1]["revised"] == ["notes", "title"]


def test_report_fixes_rejects_empty_round() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, fail=check_ids()[0])  # owner user, last_reviewed h1
    # No new commit and no metadata revision: nothing to re-review.
    with pytest.raises(WorkflowError, match="no new commits and no metadata revision"):
        engine.apply_report_fixes(state, head_sha="h1", note=None, now=_NOW)


def test_report_fixes_escalates_when_round_cap_exceeded() -> None:
    state = _paired_owned_by_user()
    _ready(state)
    _run_review(state, fail=check_ids()[0])
    state.round = 1  # already used the one permitted fix round (max_rounds=1)
    engine.apply_report_fixes(state, head_sha="h2", note=None, now=_NOW, max_rounds=1)
    assert state.round == 2
    assert state.owner == "human"
    assert state.status == "escalated"
    esc = state.escalation
    assert esc is not None
    assert "runaway-round cap" in esc["reason"]


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
    esc = state.escalation
    assert esc is not None
    assert esc["reason"] == "stuck"


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


def test_resolve_rejects_invalid_to_role() -> None:
    state = _paired_owned_by_user()
    engine.apply_escalate(state, by="user", reason="x", now=_NOW)
    with pytest.raises(WorkflowError, match="invalid --to"):
        engine.apply_resolve(state, to_role="sideways", note=None, now=_NOW)


def test_resolve_to_audit_without_note() -> None:
    state = _paired_owned_by_user()
    engine.apply_escalate(state, by="user", reason="x", now=_NOW)
    engine.apply_resolve(state, to_role="audit", note=None, now=_NOW)
    assert state.owner == "audit"
    assert state.status == "reviewing"
    assert "note" not in state.history[-1]
