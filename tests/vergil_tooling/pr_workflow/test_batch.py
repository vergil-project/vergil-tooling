"""Tests for the batch orchestrator (vergil_tooling.lib.pr_workflow.batch)."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow.batch import (
    BatchReport,
    ItemOutcome,
    ItemResult,
)


def test_all_merged_true_only_when_every_item_merged() -> None:
    merged = BatchReport(items=[ItemResult("a", ItemOutcome.MERGED)])
    assert merged.all_merged is True

    mixed = BatchReport(
        items=[
            ItemResult("a", ItemOutcome.MERGED),
            ItemResult("b", ItemOutcome.FAILED, "boom"),
        ]
    )
    assert mixed.all_merged is False


def test_all_merged_false_when_empty() -> None:
    assert BatchReport().all_merged is False
