"""Deterministic integration of the paired loop over engine + LocalFileTransport.

Drives both roles' transitions directly (no blocking waits), proving the full
handshake -> per-check review -> changes -> fixes -> approve cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.registry import check_ids

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.pr_workflow.state import WorkflowState

_NOW = "2026-06-08T00:00:00Z"


def _reload(transport: LocalFileTransport) -> WorkflowState:
    state = transport.read()
    assert state is not None
    return state


def _review_round(transport: LocalFileTransport, *, fail: str | None, head_sha: str) -> None:
    """AUDIT runs all checks one at a time, persisting after each (per-check loop)."""
    for cid in check_ids():
        state = _reload(transport)
        if cid == fail:
            engine.apply_check(
                state,
                check_id=cid,
                status="fail",
                findings=[{"file": "feature.py", "line": 1, "severity": "warning", "note": "x"}],
                reason=None,
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
        transport.write(state)


def test_paired_full_cycle(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)

    # USER init (paired) -> owner audit.
    state = engine.init_state(
        issue="1534",
        branch="feature/1534-x",
        base="develop",
        mode="paired",
        head_sha="h0",
        base_sha="b0",
        user_token="u-1",
        now=_NOW,
    )
    transport.write(state)

    # AUDIT acks -> owner user.
    state = _reload(transport)
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)
    transport.write(state)
    assert _reload(transport).owner == "user"

    # USER reports ready -> owner audit.
    state = _reload(transport)
    engine.apply_report_ready(
        state,
        title="t",
        summary="s",
        notes="n",
        linkage="Ref",
        head_sha="h1",
        now=_NOW,
    )
    transport.write(state)

    # AUDIT per-check review with one failure -> changes-requested, owner user.
    _review_round(transport, fail=check_ids()[3], head_sha="h1")
    assert _reload(transport).status == "changes-requested"

    # USER fixes -> round 1, owner audit.
    state = _reload(transport)
    engine.apply_report_fixes(state, head_sha="h2", note="reworded commit", now=_NOW)
    transport.write(state)
    assert _reload(transport).round == 1

    # AUDIT re-review, all pass -> approved, owner user.
    _review_round(transport, fail=None, head_sha="h2")

    final = _reload(transport)
    assert final.status == "approved"
    assert final.owner == "user"
    assert engine.directive_for(final, "user")["done"] is True

    # Milestone history (ignoring the per-check submit-check entries).
    milestones = [h["action"] for h in final.history if h["action"] != "submit-check"]
    assert milestones == [
        "init",
        "ack",
        "report-ready",
        "review-complete",
        "report-fixes",
        "review-complete",
    ]
