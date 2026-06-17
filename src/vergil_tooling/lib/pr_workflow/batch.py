"""Single-threaded, fail-fast batch orchestrator for the PR pipeline.

Runs a sequence of items through a per-item ``process`` callback one at a
time, stopping at the first failure (fail-fast). Completed items are
reported MERGED, the failed one FAILED with its reason, and the rest
NOT_STARTED. Post-steps (end-of-batch validation, a single release) run
only when every item merged cleanly.

The whole run is gated by exactly one up-front confirmation: per-item
prompts are pre-suppressed by the callers (they thread ``assume_yes``), so
once confirmed the batch runs unattended. (Issue #1673.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.confirm import confirm

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class BatchAbort(Exception):
    """A per-item step (or post-step) failed; the message is the reason.

    Callers convert expected failures (rebase conflict, gate red, merge
    abort, provenance violation, non-zero subprocess) into this so the
    orchestrator can record them and stop without masking unexpected bugs,
    which propagate.
    """


class ItemOutcome(StrEnum):
    MERGED = "merged"
    FAILED = "failed"
    NOT_STARTED = "not-started"


@dataclass(frozen=True)
class ItemResult:
    label: str
    outcome: ItemOutcome
    reason: str | None = None


@dataclass(frozen=True)
class PostStep:
    name: str
    run: Callable[[], None]


@dataclass
class BatchReport:
    items: list[ItemResult] = field(default_factory=list)
    post_failure: str | None = None

    @property
    def all_merged(self) -> bool:
        return bool(self.items) and all(i.outcome is ItemOutcome.MERGED for i in self.items)


def run_batch(
    items: Sequence[Any],
    process: Callable[[Any], None],
    *,
    label: Callable[[Any], str],
    plan: Sequence[str],
    assume_yes: bool,
    post_steps: Sequence[PostStep] = (),
) -> BatchReport:
    """Run *items* through *process* serially, fail-fast, then *post_steps*.

    Prints *plan* and asks exactly one confirmation (skipped with
    *assume_yes*). On decline, returns an all-NOT_STARTED report and runs
    nothing. Each item that raises ``BatchAbort`` stops the batch: it is
    recorded FAILED and the remaining items NOT_STARTED. ``post_steps`` run
    in order only when every item merged; a post-step ``BatchAbort`` is
    recorded in ``post_failure`` (never un-doing a merge) and stops the
    remaining post-steps.
    """
    report = BatchReport()

    print("Batch plan:")
    for line in plan:
        print(f"  {line}")
    if not confirm("\nRun this batch?", assume_yes=assume_yes, default=False):
        print("Aborted.")
        report.items = [ItemResult(label(it), ItemOutcome.NOT_STARTED) for it in items]
        return report

    stopped = False
    for it in items:
        if stopped:
            report.items.append(ItemResult(label(it), ItemOutcome.NOT_STARTED))
            continue
        try:
            process(it)
        except BatchAbort as exc:
            report.items.append(ItemResult(label(it), ItemOutcome.FAILED, str(exc)))
            stopped = True
        else:
            report.items.append(ItemResult(label(it), ItemOutcome.MERGED))

    if report.all_merged:
        for step in post_steps:
            try:
                step.run()
            except BatchAbort as exc:
                report.post_failure = f"{step.name}: {exc}"
                break

    return report


def format_report(report: BatchReport) -> str:
    """Render the merged / failed / not-started buckets as a summary block."""
    lines = ["", "Batch summary:"]
    for bucket, outcome in (
        ("Merged", ItemOutcome.MERGED),
        ("Failed", ItemOutcome.FAILED),
        ("Not started", ItemOutcome.NOT_STARTED),
    ):
        members = [i for i in report.items if i.outcome is outcome]
        if not members:
            continue
        lines.append(f"  {bucket}:")
        for i in members:
            suffix = f" — {i.reason}" if i.reason else ""
            lines.append(f"    {i.label}{suffix}")
    if report.post_failure:
        lines.append(f"  Post-step failed: {report.post_failure}")
    return "\n".join(lines)
