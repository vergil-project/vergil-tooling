"""The release's early-exit unlock path (spec §5.3 step 1 of epic vergil-project/.github#1664).

A fresh ``vrg-release`` takes the release lock in ``preflight`` and records
its tracking issue in the lock in ``prepare``. Every exit between the two, a
refusal or a failure, must leave no lock held and no PR disarmed: a PR is
never left disarmed with no lock held, and no lock is left held by a release
that no tracking issue records. The window spans two stages, so the guard is
held by the orchestrator's pipeline state, around the stage loop, rather
than by either stage:

- :meth:`UnlockGuard.arm` is called by ``preflight`` the moment the lock is
  taken, and :meth:`UnlockGuard.disarm_started` the moment the quiesce
  starts its first disarm sweep.
- The guard is **armed** from then until the lock records a tracking issue
  (:attr:`UnlockGuard.armed`).
- :meth:`UnlockGuard.unlock` runs the unlock path when a stage exits while
  it is armed: it releases the lock (a compare-and-swap delete against the
  owner's last write), then, if the disarm sweep had started, the re-arm
  sweep with no holder's pass. A failure in either is raised with the
  error that caused the exit, never swallowed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release_lock import sweeps

if TYPE_CHECKING:
    from vergil_tooling.lib.release_lock.holder import LockOwner

__all__ = ["UnlockGuard"]

_PHASE = "unlock-on-exit"


class UnlockGuard:
    """Whether this run holds a lock that no tracking issue records, and its unlock path."""

    def __init__(self) -> None:
        self._owner: LockOwner | None = None
        self._repo = ""
        self._disarm_started = False

    def arm(self, repo: str, owner: LockOwner) -> None:
        """Arm the guard for *owner*, the lock on *repo* this run has just taken."""
        if self._owner is not None:
            msg = "the release's unlock guard is already armed"
            raise RuntimeError(msg)
        self._owner = owner
        self._repo = repo
        self._disarm_started = False

    def disarm_started(self) -> None:
        """Record that the quiesce has started disarming PRs."""
        self._disarm_started = True

    @property
    def armed(self) -> bool:
        """Whether a lock this run took is held and records no tracking issue yet."""
        return self._owner is not None and not self._owner.record.tracking_issue

    def unlock(self, cause: BaseException) -> None:
        """Run the unlock path for an exit on *cause*: release the lock, then re-arm if needed.

        The guard is spent whatever the outcome, so the path runs at most
        once. The re-arm sweep runs only once the lock is released: a lock
        still held, or lost to a break or a reclaim (whose own sweep
        re-armed), is left for ``vrg-release-lock break``, which re-arms.
        Raises :class:`ReleaseError` naming *cause* and every failure of the
        path; returns quietly only when the whole path succeeded.
        """
        owner = self._owner
        if owner is None:
            return
        self._owner = None
        repo = self._repo
        disarmed = self._disarm_started
        print(f"Releasing the release lock on {repo}: stopped before the tracking issue existed")
        try:
            owner.release()
        except Exception as exc:
            raise _failure(
                repo,
                cause,
                f"releasing the lock failed: {exc}. The lock may still be held: check it"
                f" with `vrg-release-lock status`, and if it is, remove it with"
                f' `vrg-release-lock break --reason "<why>"`, which also re-arms'
                f" the PRs{' this run disarmed' if disarmed else ''}.",
            ) from exc
        print(f"Released the release lock on {repo}.")
        if not disarmed:
            return
        try:
            result = sweeps.rearm_all(repo)
        except sweeps.SweepError as exc:
            raise _failure(
                repo,
                cause,
                f"the lock was released, but the re-arm sweep failed: {exc}. Every open PR"
                " into develop it did not re-arm is still disarmed: turn auto-merge back"
                " on for each.",
            ) from exc
        print(
            f"Re-armed {len(result.armed)} PR(s) on {repo}"
            + (f": {', '.join(result.armed)}" if result.armed else "")
        )
        for url, why in result.refused:
            print(f"  Not re-armed (refused by the one PR path): {url}: {why}")
        for url in result.held:
            print(f"  Not re-armed (held): {url}")


def _failure(repo: str, cause: BaseException, what: str) -> ReleaseError:
    reason = str(cause) or type(cause).__name__
    return ReleaseError(
        phase=_PHASE,
        command=f"release lock on {repo}",
        message=(
            f"The release stopped before its tracking issue existed ({reason}), and its"
            f" unlock path did not complete: {what}"
        ),
    )
