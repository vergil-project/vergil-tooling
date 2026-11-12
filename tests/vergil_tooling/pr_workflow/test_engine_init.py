"""Tests for run-and-done init_state (#1872)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError


def test_init_state_is_run_and_done() -> None:
    state = engine.init_state(
        issues=["42"],
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


def test_init_state_single_issue_records_no_issue_list() -> None:
    state = engine.init_state(
        issues=["42"], branch="b", base="develop", head_sha="h", base_sha="b0", now="t"
    )
    assert state.issues is None
    assert state.all_issues == ["42"]


def test_init_state_batch_records_the_ordered_list() -> None:
    state = engine.init_state(
        issues=["11", "10"], branch="b", base="develop", head_sha="h", base_sha="b0", now="t"
    )
    assert state.issue == "11"
    assert state.issues == ["11", "10"]


def test_init_state_refuses_no_issue() -> None:
    with pytest.raises(WorkflowError, match="at least one issue"):
        engine.init_state(
            issues=[], branch="b", base="develop", head_sha="h", base_sha="b0", now="t"
        )
