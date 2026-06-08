"""Tests for engine init, handshake, and the ownership guard."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError

_NOW = "2026-06-08T15:00:00Z"


def test_init_paired_assigns_owner_audit_and_records_user_presence() -> None:
    state = engine.init_state(
        issue="1534", branch="feature/1534-x", base="origin/develop",
        mode="paired", head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    assert state.mode == "paired"
    assert state.owner == "audit"
    assert state.status == "implementing"
    assert state.round == 0
    assert state.participants["user"]["token"] == "u-1"
    assert state.participants["audit"] is None
    assert state.git == {"base_sha": "b0", "head_sha": "h0", "last_reviewed_sha": None}
    assert state.history[0]["action"] == "init"
    assert state.history[0]["mode"] == "paired"


def test_init_solo_assigns_owner_user() -> None:
    state = engine.init_state(
        issue="1534", branch="feature/1534-x", base="origin/develop",
        mode="solo", head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    assert state.mode == "solo"
    assert state.owner == "user"


def test_init_rejects_unknown_mode() -> None:
    with pytest.raises(WorkflowError, match="mode"):
        engine.init_state(
            issue="1", branch="b", base="origin/develop", mode="bogus",
            head_sha="h", base_sha="b", user_token="u", now=_NOW,
        )


def test_audit_ack_records_presence_and_flips_owner_to_user() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    engine.audit_ack(state, issue="1534", audit_token="a-1", now="2026-06-08T15:00:05Z")
    assert state.owner == "user"
    assert state.participants["audit"]["token"] == "a-1"
    assert state.history[-1]["action"] == "ack"


def test_audit_ack_rejects_issue_mismatch() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    with pytest.raises(WorkflowError, match="issue mismatch"):
        engine.audit_ack(state, issue="999", audit_token="a-1", now=_NOW)


def test_audit_ack_rejects_solo_workflow() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="solo",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    with pytest.raises(WorkflowError, match="solo"):
        engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)
