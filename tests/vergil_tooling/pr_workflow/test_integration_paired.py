"""Deterministic integration of the paired loop over engine + LocalFileTransport.

Drives both roles' state transitions directly (no blocking waits), proving the
full handshake -> changes -> fixes -> approve cycle and the recorded history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.registry import check_ids

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-06-08T00:00:00Z"


def _all(status: str) -> list[dict]:
    return [{"id": cid, "status": status} for cid in check_ids()]


def test_paired_full_cycle(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)

    # USER init (paired) -> owner audit.
    state = engine.init_state(
        issue="1534", branch="feature/1534-x", base="develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    transport.write(state)

    # AUDIT acks -> owner user.
    state = transport.read()
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)
    transport.write(state)
    assert transport.read().owner == "user"

    # USER reports ready -> owner audit.
    state = transport.read()
    engine.apply_report_ready(
        state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
    )
    transport.write(state)

    # AUDIT review with one failure -> changes-requested, owner user.
    state = transport.read()
    checks = _all("pass")
    checks[3] = {"id": checks[3]["id"], "status": "fail",
                 "findings": [{"file": "feature.py", "line": 1, "severity": "warning",
                               "note": "commit message overstates the change"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    transport.write(state)
    assert transport.read().status == "changes-requested"

    # USER fixes -> round 1, owner audit.
    state = transport.read()
    engine.apply_report_fixes(state, head_sha="h2", note="reworded commit", now=_NOW)
    transport.write(state)
    assert transport.read().round == 1

    # AUDIT re-review, all pass -> approved, owner user.
    state = transport.read()
    engine.apply_review(state, checks=_all("pass"), head_sha="h2", now=_NOW)
    transport.write(state)

    final = transport.read()
    assert final.status == "approved"
    assert final.owner == "user"
    assert engine.directive_for(final, "user")["done"] is True

    actions = [h["action"] for h in final.history]
    assert actions == ["init", "ack", "report-ready", "submit-review", "report-fixes", "submit-review"]
