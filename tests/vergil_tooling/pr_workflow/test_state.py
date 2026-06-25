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
