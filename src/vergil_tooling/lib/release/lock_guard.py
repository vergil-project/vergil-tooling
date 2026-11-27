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

from vergil_tooling.lib import github
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.resume import is_own_lock
from vergil_tooling.lib.release_lock import refs as lock_refs
from vergil_tooling.lib.release_lock import sweeps
from vergil_tooling.lib.release_lock.holder import LockLost
from vergil_tooling.lib.release_lock.model import ReleaseLockError

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext
    from vergil_tooling.lib.release_lock.holder import LockOwner

__all__ = ["UnlockGuard", "holder_identity", "rearm", "rearm_lines", "release_lock"]

_PHASE = "unlock-on-exit"


def holder_identity(phase: str = "preflight") -> str:
    """The GitHub login this release runs as: the identity its lock names as the holder."""
    try:
        login = github.read_output("api", "user", "--jq", ".login")
    except Exception as exc:
        raise ReleaseError(
            phase=phase,
            command="gh api user --jq .login",
            message="Could not read the GitHub login the release lock names as its holder.",
            detail=error_text(exc),
        ) from exc
    if not login:
        raise ReleaseError(
            phase=phase,
            command="gh api user --jq .login",
            message="gh api user returned no login for the release lock's holder.",
        )
    return login


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


# --- the unlock path of a release that has its tracking issue ------------------
#
# The ``unlock`` and ``rearm`` stages (spec §5.3 step 2 of epic
# vergil-project/.github#1664), and the abandon step of a stray merge, which
# ends with both.

_BREAK = 'vrg-release-lock break --reason "<why>"'


def _describe(state: lock_refs.LockState) -> str:
    record = state.record
    if record is None:
        return f"it cannot be read ({state.error})"
    issue = record.tracking_issue or "none recorded"
    return (
        f"release {record.version}, held by {record.holder_identity} on"
        f" {record.holder_host}, tracking issue {issue}"
    )


def release_lock(ctx: ReleaseContext, *, phase: str, remedy: str) -> None:
    """Delete the release lock this run holds, by compare-and-swap on its last write.

    The heartbeat stops first (:meth:`LockOwner.release`). Once the lock is
    deleted, ``ctx.lock`` is ``None``: nothing after the unlock writes or
    reads the lock. A run with no lock owner (a resume at ``unlock``) takes
    and adopts none: it reads the lock (:func:`_release_without_owner`). A
    failure raises :class:`ReleaseError` naming it and *remedy*; a lost lock
    (broken or reclaimed meanwhile) is no longer this run's, so ``ctx.lock``
    is cleared then too.
    """
    owner = ctx.lock
    command = f"release lock on {ctx.repo}"
    if owner is None:
        _release_without_owner(ctx, phase=phase)
        return
    try:
        owner.release()
    except (ReleaseError, ReleaseLockError, RuntimeError) as exc:
        if isinstance(exc, LockLost):
            ctx.lock = None
        raise ReleaseError(
            phase=phase,
            command=command,
            message=f"Releasing the release lock on {ctx.repo} failed: {exc}. {remedy}",
        ) from exc
    ctx.lock = None
    print(f"Released the release lock on {ctx.repo}.")


def _release_without_owner(ctx: ReleaseContext, *, phase: str) -> None:
    """The unlock of a resume at ``unlock``: delete what is left of this release's own lock.

    Spec §5.3 of epic vergil-project/.github#1664. The lock is read: none
    held means the stage is done. One that names this release's identity and
    tracking issue is deleted by compare-and-swap against the sha just read.
    Anything else (an unreadable lock, another release's, or a lock that
    changed between the read and the delete) is refused, naming it.
    """
    command = f"read release lock on {ctx.repo}"
    try:
        state = lock_refs.read_lock(ctx.repo)
    except ReleaseLockError as exc:
        raise ReleaseError(
            phase=phase,
            command=command,
            message=f"Reading the release lock on {ctx.repo} failed: {exc}.",
        ) from exc
    if state.sha is None:
        print(f"No release lock is held on {ctx.repo}: nothing to release.")
        return
    tracking_issue = f"{ctx.repo}#{ctx.issue_number}"
    if (
        state.record is not None
        and ctx.issue_number is not None
        and is_own_lock(
            state.record, identity=holder_identity(phase), tracking_issue=tracking_issue
        )
    ):
        _delete_own(ctx, state.sha, phase=phase)
        return
    raise ReleaseError(
        phase=phase,
        command=command,
        message=(
            f"This run holds no release lock (it is a resume), and the release lock on"
            f" {ctx.repo} is held: {_describe(state)}. It is not release {ctx.version}'s"
            f" own ({tracking_issue}), and a resume deletes only its own release's lock."
            " Check it with `vrg-release-lock status`; once no run of the release holding"
            f" it is alive, remove it with `{_BREAK}`, which also re-arms the PRs, then"
            f" re-run `vrg-release --resume {ctx.version}`."
        ),
    )


def _delete_own(ctx: ReleaseContext, sha: str, *, phase: str) -> None:
    """Delete this release's remaining lock, by compare-and-swap against *sha*, as just read."""
    command = f"delete {lock_refs.LOCK_REF} on {ctx.repo}"
    retry = (
        "Check it with `vrg-release-lock status`, then re-run"
        f" `vrg-release --resume {ctx.version}`."
    )
    try:
        deleted = lock_refs.delete_lock(ctx.repo, expected=sha)
    except ReleaseLockError as exc:
        raise ReleaseError(
            phase=phase,
            command=command,
            message=(
                f"Deleting release {ctx.version}'s release lock on {ctx.repo} failed: {exc}."
                f" {retry}"
            ),
        ) from exc
    if not deleted:
        raise ReleaseError(
            phase=phase,
            command=command,
            message=(
                f"The release lock on {ctx.repo} changed between this run's read of it"
                f" (at {sha}) and its delete, so it was not deleted. {retry}"
            ),
        )
    print(f"Deleted release {ctx.version}'s remaining release lock on {ctx.repo} (at {sha}).")


def rearm_lines(result: sweeps.RearmResult) -> list[str]:
    """What the re-arm sweep did, one line per outcome and per PR it left unarmed."""
    armed = f"Re-armed {len(result.armed)} PR(s)"
    lines = [f"{armed}: {', '.join(result.armed)}" if result.armed else f"{armed}."]
    if result.merged:
        lines.append(f"Merged by the one PR path: {', '.join(result.merged)}")
    lines.extend(f"Not re-armed (held): {url}" for url in result.held)
    lines.extend(
        f"Not re-armed (refused by the one PR path): {url}: {why}" for url, why in result.refused
    )
    return lines


def rearm(ctx: ReleaseContext, *, phase: str, remedy: str) -> sweeps.RearmResult:
    """Run the re-arm sweep with no holder's pass: the lock is gone.

    A PR the one PR path refused or held is in the result, never a failure.
    A sweep that could not be made, or could not offer some PRs, raises
    :class:`ReleaseError` naming what is still disarmed and *remedy*.
    """
    command = f"re-arm sweep on {ctx.repo}"
    try:
        result = sweeps.rearm_all(ctx.repo)
    except sweeps.RearmSweepError as exc:
        raise ReleaseError(
            phase=phase,
            command=command,
            message=(
                f"{exc}. Each of those PRs is still disarmed. {remedy}"
                f" What the sweep did with the rest: {'; '.join(rearm_lines(exc.result))}"
            ),
        ) from exc
    except sweeps.SweepError as exc:
        raise ReleaseError(
            phase=phase,
            command=command,
            message=(
                f"The re-arm sweep on {ctx.repo} failed: {exc}. Every open PR into develop"
                f" it did not re-arm is still disarmed. {remedy}"
            ),
        ) from exc
    for line in rearm_lines(result):
        print(line)
    return result
