"""Tests for vergil_tooling.lib.pr_workflow.registry."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow import registry


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
