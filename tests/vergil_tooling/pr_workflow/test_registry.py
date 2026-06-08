"""Tests for vergil_tooling.lib.pr_workflow.registry."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import registry
from vergil_tooling.lib.pr_workflow.errors import WorkflowError


def test_check_ids_are_the_six_seed_checks() -> None:
    assert registry.check_ids() == (
        "site-docs-reflection",
        "docstring-accuracy",
        "pr-description-fidelity",
        "commit-message-fidelity",
        "scope-coherence",
        "test-adequacy",
    )


def test_check_ids_have_no_duplicates() -> None:
    ids = registry.check_ids()
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("check_id", registry.check_ids())
def test_check_prompt_loads_named_check(check_id: str) -> None:
    text = registry.check_prompt(check_id)
    assert check_id in text
    assert "check.v1" in text


def test_check_prompt_rejects_unknown_id() -> None:
    with pytest.raises(WorkflowError, match="unknown check"):
        registry.check_prompt("made-up")
