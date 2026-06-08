"""Tests for vergil_tooling.lib.pr_workflow.state."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _minimal() -> WorkflowState:
    return WorkflowState(
        issue="1534",
        branch="feature/1534-x",
        base="origin/develop",
        mode="paired",
        owner="audit",
        status="implementing",
        round=0,
        created_at="2026-06-08T15:00:00Z",
        updated_at="2026-06-08T15:00:00Z",
        participants={"user": {"token": "u-1", "present_at": "2026-06-08T15:00:00Z"}, "audit": None},
        git={"base_sha": "b0", "head_sha": "h0", "last_reviewed_sha": None},
    )


def test_roundtrip_through_json_preserves_fields() -> None:
    state = _minimal()
    restored = WorkflowState.from_json(state.to_json())
    assert restored.to_dict() == state.to_dict()


def test_to_dict_has_stable_top_level_keys() -> None:
    keys = set(_minimal().to_dict())
    assert keys == {
        "schema_version", "issue", "branch", "base", "phase", "mode", "owner",
        "status", "round", "created_at", "updated_at", "participants",
        "pr_metadata", "git", "checks", "escalation", "error", "history",
    }


def test_from_json_rejects_non_json() -> None:
    with pytest.raises(WorkflowError, match="not valid JSON"):
        WorkflowState.from_json("{not json")


def test_from_dict_rejects_missing_required_field() -> None:
    data = _minimal().to_dict()
    del data["owner"]
    with pytest.raises(WorkflowError, match="owner"):
        WorkflowState.from_dict(data)


def test_validate_rejects_bad_owner() -> None:
    state = _minimal()
    state.owner = "nobody"
    with pytest.raises(WorkflowError, match="invalid owner"):
        state.validate()


def test_from_dict_rejects_unknown_schema_version() -> None:
    data = _minimal().to_dict()
    data["schema_version"] = 99
    with pytest.raises(WorkflowError, match="schema_version"):
        WorkflowState.from_dict(data)
