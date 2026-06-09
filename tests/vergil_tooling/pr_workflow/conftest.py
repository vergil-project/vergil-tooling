"""Shared fixtures for the pr_workflow CLI/transport tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fast_workflow_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI-driven waits from sleeping on real time.

    ``vrg-pr-workflow`` polls once a second and waits patiently (24h) in
    production; under test that turns any wait that does not resolve on the
    first read into seconds — or, with the patient timeout, an effective hang —
    which dominated the suite's wall-clock. Force a 0 poll interval (no real
    sleeps) and a short timeout (fail fast instead of blocking) for every test
    here (issue #1572).
    """
    monkeypatch.setenv("VRG_PR_WORKFLOW_POLL_INTERVAL", "0")
    monkeypatch.setenv("VRG_PR_WORKFLOW_TIMEOUT", "2")
