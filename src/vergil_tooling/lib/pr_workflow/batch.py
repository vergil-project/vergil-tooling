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
