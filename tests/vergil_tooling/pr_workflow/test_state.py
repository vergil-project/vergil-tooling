"""Tests for the slimmed run-and-done WorkflowState (#1872)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import SCHEMA_VERSION, WorkflowState


def _state(**overrides: Any) -> WorkflowState:
    base: dict[str, Any] = {
        "issue": "42",
        "branch": "feature/42-x",
        "base": "origin/develop",
        "status": "ready",
        "created_at": "2026-06-25T00:00:00Z",
        "updated_at": "2026-06-25T00:00:00Z",
        "git": {"base_sha": "aaa", "head_sha": "bbb"},
    }
    base.update(overrides)
    return WorkflowState(**base)  # type: ignore[arg-type]


def test_round_trips_through_json() -> None:
    state = _state(pr_metadata={"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"})
    restored = WorkflowState.from_json(state.to_json())
    assert restored == state


def test_schema_version_is_two() -> None:
    assert SCHEMA_VERSION == 2
    assert json.loads(_state().to_json())["schema_version"] == 2


def test_unsupported_schema_version_rejected() -> None:
    payload = json.loads(_state().to_json())
    payload["schema_version"] = 1
    with pytest.raises(WorkflowError, match="unsupported schema_version"):
        WorkflowState.from_json(json.dumps(payload))


def test_missing_required_field_rejected() -> None:
    payload = json.loads(_state().to_json())
    del payload["branch"]
    with pytest.raises(WorkflowError, match="missing required field 'branch'"):
        WorkflowState.from_json(json.dumps(payload))


def test_invalid_status_rejected() -> None:
    with pytest.raises(WorkflowError, match="invalid status"):
        WorkflowState.from_json(_state(status="reviewing").to_json())


# --- The ordered issue list of a batch (epic vergil-project/.github#1019) ---


def test_state_round_trips_issue_list() -> None:
    state = _state(issue="10", issues=["10", "11", "12"])
    restored = WorkflowState.from_json(state.to_json())
    assert restored == state
    assert restored.issue == "10"
    assert restored.issues == ["10", "11", "12"]
    assert restored.all_issues == ["10", "11", "12"]
    assert json.loads(state.to_json())["issues"] == ["10", "11", "12"]


def test_state_issue_list_follows_issue_in_key_order() -> None:
    keys = list(json.loads(_state(issue="10", issues=["10", "11"]).to_json()))
    assert keys[keys.index("issue") + 1] == "issues"


def test_state_single_issue_json_unchanged() -> None:
    state = _state(pr_metadata={"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"})
    payload = json.loads(state.to_json())
    assert "issues" not in payload
    assert list(payload) == [
        "schema_version",
        "issue",
        "branch",
        "base",
        "status",
        "created_at",
        "updated_at",
        "git",
        "pr_metadata",
        "submitted",
    ]


def test_state_without_issues_reads_as_single() -> None:
    restored = WorkflowState.from_json(_state().to_json())
    assert restored.issues is None
    assert restored.all_issues == ["42"]


def test_state_null_issue_list_reads_as_single() -> None:
    payload = json.loads(_state().to_json())
    payload["issues"] = None
    assert WorkflowState.from_json(json.dumps(payload)).all_issues == ["42"]


@pytest.mark.parametrize(
    ("issues", "message"),
    [
        (["10"], "at least 2"),
        ([], "at least 2"),
        (["11", "10"], "first entry"),
        (["10", ""], "non-empty string"),
        (["10", 11], "non-empty string"),
        ("10,11", "must be a list"),
    ],
)
def test_state_rejects_a_malformed_issue_list(issues: object, message: str) -> None:
    payload = json.loads(_state(issue="10").to_json())
    payload["issues"] = issues
    with pytest.raises(WorkflowError, match=message):
        WorkflowState.from_json(json.dumps(payload))
