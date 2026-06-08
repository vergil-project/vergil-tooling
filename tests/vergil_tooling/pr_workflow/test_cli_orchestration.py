"""Deterministic tests for the paired CLI handshake glue (no real blocking)."""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from vergil_tooling.bin import vrg_pr_workflow as cli
from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

if TYPE_CHECKING:
    import pytest

_NOW = "2026-06-08T00:00:00Z"


class FakeTransport(Transport):
    """In-memory transport whose waits resolve immediately by flipping owner."""

    def __init__(self) -> None:
        self.state: WorkflowState | None = None
        self.writes: list[WorkflowState] = []
        self.base = "origin/develop"
        self.worktree_root = None  # settings.max_rounds is not exercised here

    def read(self) -> WorkflowState | None:
        return self.state

    def write(self, state: WorkflowState) -> None:
        # Independent copies: the writes log is a historical record that later
        # in-place mutations of self.state must not retroactively alter.
        self.state = WorkflowState.from_json(state.to_json())
        self.writes.append(WorkflowState.from_json(state.to_json()))

    def wait_until_present(self, *, timeout: float) -> WorkflowState:
        assert self.state is not None
        return self.state

    def wait_until_owner(self, role: str, *, timeout: float) -> WorkflowState:
        assert self.state is not None
        self.state.owner = role  # simulate the counterpart handing the turn over
        return self.state

    def head_sha(self) -> str:
        return "h0"

    def merge_base(self) -> str:
        return "b0"


def _args(**kw: object) -> argparse.Namespace:
    ns = argparse.Namespace(issue=None, no_audit=False)
    ns.__dict__.update(kw)
    return ns


def test_next_user_init_paired_writes_audit_then_waits(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli.git, "current_branch", lambda: "feature/1534-x")
    transport = FakeTransport()
    rc = cli._next_user(_args(as_role="user", issue="1534", no_audit=False), transport)
    assert rc == 0
    assert transport.writes[0].owner == "audit"  # init handed to audit for the handshake
    directive = json.loads(capsys.readouterr().out)
    assert directive["then"]["verb"] == "report-ready"  # wait flipped back to user


def test_next_audit_first_call_acks_and_returns_review_directive(capsys) -> None:
    transport = FakeTransport()
    transport.state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    rc = cli._next_audit(_args(as_role="audit", issue="1534"), transport)
    assert rc == 0
    assert any(w.participants.get("audit") for w in transport.writes)  # ack recorded
    directive = json.loads(capsys.readouterr().out)
    assert directive["then"]["verb"] == "submit-review"


def test_next_audit_solo_exits_clean(capsys) -> None:
    transport = FakeTransport()
    transport.state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="solo",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    rc = cli._next_audit(_args(as_role="audit", issue="1534"), transport)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "solo"
