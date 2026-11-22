"""Test helpers for the vrg-release pipeline's context."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from vergil_tooling.lib.release.context import INDEX_STAGE
from vergil_tooling.lib.release.state import FailedJob, initial_state

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def defer_publish(ctx: ReleaseContext, *names: str) -> None:
    """Defer *names* on *ctx*, as the confirm and package-index stages do.

    ``ctx.deferred_publish_failures`` is a view of the state block, so each
    CD job name is recorded as a failed job of main CD run 1 and
    ``package-index`` as this run's index miss. A context with no state
    block is given an initial one first.
    """
    current = ctx.state or initial_state(
        version=ctx.version, cut="c" * 40, issues=(), without_deps=False
    )
    jobs = tuple(FailedJob("main", "1", name) for name in names if name != INDEX_STAGE)
    ctx.state = replace(current, failed_jobs=(*current.failed_jobs, *jobs))
    if INDEX_STAGE in names:
        ctx.index_missed = True
