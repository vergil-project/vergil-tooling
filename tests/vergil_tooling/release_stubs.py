"""Test helpers for the vrg-release pipeline's context."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from vergil_tooling.lib import trust
from vergil_tooling.lib.release.context import INDEX_STAGE
from vergil_tooling.lib.release.state import FailedJob, initial_state
from vergil_tooling.lib.release_lock.model import LockRecord

if TYPE_CHECKING:
    from collections.abc import Generator

    from vergil_tooling.lib.release.context import ReleaseContext

# The trust layer's branch lookup, which every release lookup by branch reads.
BRANCH_LOOKUP = "vergil_tooling.lib.trust.prs_for_branch"
FORK_LOGIN = "fork-owner"


def branch_pr(
    number: int, state: str, *, head: str = "h0", branch: str = "release/2.1.0"
) -> trust.PRRef:
    """A same-repo PR with a trusted author, as :func:`trust.prs_for_branch` returns it."""
    return trust.PRRef(
        number=number,
        url=f"https://github.com/owner/repo/pull/{number}",
        title="T",
        head=branch,
        head_oid=head,
        base="develop",
        author_role=trust.Role.AGENTS,
        state=state,
    )


def fork_skip(number: int) -> trust.Skipped:
    """The report :func:`trust.prs_for_branch` gives for a fork PR on the branch."""
    return trust.Skipped(
        "owner/repo",
        number,
        "pr",
        FORK_LOGIN,
        "2026-11-22T00:00:00Z",
        "head",
        trust.CROSS_REPOSITORY,
    )


@contextmanager
def branch_lookup(
    *prs: trust.PRRef, skipped: tuple[trust.Skipped, ...] = ()
) -> Generator[MagicMock]:
    """Patch the trust layer's branch lookup to return *prs*, reporting *skipped*."""
    lookup = MagicMock(return_value=(list(prs), list(skipped)))
    with patch(BRANCH_LOOKUP, lookup):
        yield lookup


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


class FakeLockOwner:
    """A stand-in for :class:`LockOwner`: its record, and every write, in *events*.

    Each :meth:`update` appends ``("update", fields)`` and :meth:`release`
    ``("release",)``. *update_error* and *release_error*, when set, are
    raised by the next such call.
    """

    def __init__(self, events: list[tuple[object, ...]] | None = None) -> None:
        self.events: list[tuple[object, ...]] = [] if events is None else events
        self.record = LockRecord(
            holder_identity="maintainer",
            holder_host="laptop",
            version="2.1.0",
            worktree="",
            tracking_issue="",
            stage="preflight",
            health="running",
            broken_stage="",
            started_at="2026-11-27T10:00:00Z",
            heartbeat_at="2026-11-27T10:00:00Z",
            token="t" * 32,
        )
        self.sha = "5" * 40
        self.update_error: Exception | None = None
        self.release_error: Exception | None = None

    @property
    def token(self) -> str:
        return self.record.token

    def update(self, **fields: str) -> None:
        self.events.append(("update", fields))
        if self.update_error is not None:
            raise self.update_error
        self.record = replace(self.record, **fields)

    def release(self) -> None:
        self.events.append(("release",))
        if self.release_error is not None:
            raise self.release_error
