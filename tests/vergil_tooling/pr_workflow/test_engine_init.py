"""Tests for run-and-done init_state (#1872)."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow import engine


def test_init_state_is_run_and_done() -> None:
    state = engine.init_state(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        head_sha="bbb",
        base_sha="aaa",
        now="2026-06-25T00:00:00Z",
    )
    assert state.issue == "42"
    assert state.branch == "feature/42-x"
    assert state.base == "origin/develop"
    assert state.status == "implementing"
    assert state.pr_metadata is None
    assert state.submitted is None
    assert state.git == {"base_sha": "aaa", "head_sha": "bbb"}
