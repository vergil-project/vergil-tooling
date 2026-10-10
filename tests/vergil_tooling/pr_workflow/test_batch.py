"""Tests for the batch orchestrator (vergil_tooling.lib.pr_workflow.batch)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow.batch import (
    BatchAbortError,
    BatchReport,
    ItemOutcome,
    ItemResult,
    PostStep,
    format_report,
    run_batch,
)

_MOD = "vergil_tooling.lib.pr_workflow.batch"


def _confirm_yes():
    return patch(_MOD + ".confirm", return_value=True)


def test_all_completed_true_only_when_every_item_completed() -> None:
    merged = BatchReport(items=[ItemResult("a", ItemOutcome.MERGED)])
    assert merged.all_completed is True

    mixed = BatchReport(
        items=[
            ItemResult("a", ItemOutcome.MERGED),
            ItemResult("b", ItemOutcome.FAILED, "boom"),
        ]
    )
    assert mixed.all_completed is False


def test_all_completed_false_when_empty() -> None:
    assert BatchReport().all_completed is False


def test_all_items_processed_in_order_on_success() -> None:
    seen: list[str] = []
    with _confirm_yes():
        report = run_batch(
            ["a", "b", "c"],
            process=seen.append,
            label=lambda it: it,
            plan=["do a", "do b", "do c"],
            assume_yes=True,
            completed=ItemOutcome.MERGED,
        )
    assert seen == ["a", "b", "c"]
    assert report.all_completed is True


def test_first_failure_stops_and_marks_rest_not_started() -> None:
    def process(it: str) -> None:
        if it == "b":
            raise BatchAbortError("gate red")

    with _confirm_yes():
        report = run_batch(
            ["a", "b", "c"],
            process=process,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            completed=ItemOutcome.MERGED,
        )
    outcomes = [(i.label, i.outcome.value, i.reason) for i in report.items]
    assert outcomes == [
        ("a", "merged", None),
        ("b", "failed", "gate red"),
        ("c", "not-started", None),
    ]
    assert report.all_completed is False


def test_post_steps_run_once_on_full_success() -> None:
    calls: list[str] = []
    with _confirm_yes():
        run_batch(
            ["a"],
            process=lambda it: None,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            completed=ItemOutcome.MERGED,
            post_steps=[PostStep("release", lambda: calls.append("release"))],
        )
    assert calls == ["release"]


def test_post_steps_skipped_when_any_item_failed() -> None:
    calls: list[str] = []

    def process(it: str) -> None:
        raise BatchAbortError("nope")

    with _confirm_yes():
        report = run_batch(
            ["a"],
            process=process,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            completed=ItemOutcome.MERGED,
            post_steps=[PostStep("release", lambda: calls.append("release"))],
        )
    assert calls == []
    assert report.post_failure is None


def test_post_step_failure_recorded_not_raised() -> None:
    def boom() -> None:
        raise BatchAbortError("release blew up")

    with _confirm_yes():
        report = run_batch(
            ["a"],
            process=lambda it: None,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            completed=ItemOutcome.MERGED,
            post_steps=[PostStep("release", boom)],
        )
    assert report.post_failure == "release: release blew up"


def test_decline_marks_all_not_started_and_runs_nothing() -> None:
    seen: list[str] = []
    with patch(_MOD + ".confirm", return_value=False):
        report = run_batch(
            ["a", "b"],
            process=seen.append,
            label=lambda it: it,
            plan=[],
            assume_yes=False,
            completed=ItemOutcome.MERGED,
        )
    assert seen == []
    assert [i.outcome.value for i in report.items] == ["not-started", "not-started"]


def test_format_report_groups_buckets() -> None:
    report = BatchReport(
        items=[
            ItemResult("a", ItemOutcome.MERGED),
            ItemResult("b", ItemOutcome.FAILED, "gate red"),
            ItemResult("c", ItemOutcome.NOT_STARTED),
        ]
    )
    out = format_report(report)
    assert "Merged:" in out
    assert "a" in out
    assert "Failed:" in out
    assert "b — gate red" in out
    assert "Not started:" in out


def test_format_report_includes_post_failure() -> None:
    report = BatchReport(
        items=[ItemResult("a", ItemOutcome.MERGED)],
        post_failure="release: boom",
    )
    assert "Post-step failed: release: boom" in format_report(report)


def test_all_completed_counts_submitted_items() -> None:
    report = BatchReport(
        items=[ItemResult("a", ItemOutcome.SUBMITTED), ItemResult("b", ItemOutcome.SUBMITTED)]
    )
    assert report.all_completed is True


def test_submit_only_batch_records_submitted_never_merged() -> None:
    with _confirm_yes():
        report = run_batch(
            ["a", "b"],
            process=lambda it: None,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            completed=ItemOutcome.SUBMITTED,
        )
    assert [i.outcome for i in report.items] == [ItemOutcome.SUBMITTED, ItemOutcome.SUBMITTED]
    out = format_report(report)
    assert "Submitted (PR opened, not merged):" in out
    assert "Merged" not in out


def test_finalize_batch_summary_says_merged() -> None:
    with _confirm_yes():
        report = run_batch(
            ["a"],
            process=lambda it: None,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            completed=ItemOutcome.MERGED,
        )
    out = format_report(report)
    assert "Merged:" in out
    assert "Submitted" not in out


def test_run_batch_rejects_non_completion_outcome() -> None:
    with pytest.raises(ValueError, match="completed must be one of"):
        run_batch(
            ["a"],
            process=lambda it: None,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            completed=ItemOutcome.FAILED,
        )
