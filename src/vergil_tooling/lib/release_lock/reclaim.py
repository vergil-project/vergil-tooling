"""Reclaiming a stale release lock, and breaking one (spec §5.6, epic vergil-project/.github#1664).

- :func:`reclaim_if_stale` is for any trusted identity, the agent's
  included. It judges the lock with :func:`stale.judge`; a lock that is not
  stale (live, held by a broken release whose tracking issue is open, or
  unreadable) is left alone. A stale one is deleted by compare-and-swap
  against the value judged, so of several reclaimers only one wins and a
  holder whose heartbeat landed meanwhile keeps its lock. The winner records
  a ``reclaim`` entry in the lock history and a comment on the tracking
  issue, then runs the re-arm sweep, then records a ``reclaim-rearm`` entry
  listing the PRs it armed (and naming the sweep's failure, if it failed).
- :func:`break_lock` is the human's escape hatch, behind
  ``vrg-release-lock break``, which refuses every agent identity and agent
  signal. It removes any held lock, a live or an unparseable one included.
  It records the ``break`` entry and the comment *first*, so a break never
  lands unrecorded, then deletes by compare-and-swap. A live holder's
  heartbeat can rewrite the lock between the read and the delete, so a
  mismatch is re-read and the delete retried against the new value, while
  the lock is still the one the entry recorded (the same holder token, or
  for an unparseable lock the same text). A break that still loses, or
  finds the lock gone or held by another holder, records a
  ``break-failed`` entry and a comment, and raises: it never runs the
  re-arm sweep with the lock in place.

Each entry's ``old`` is the old lock's file text as read, and a holder that
loses its lock tells a break or reclaim of *its* lock by the token in that
text (:mod:`.holder`). A ``break-failed`` entry's ``old`` is the text the
``break`` entry recorded, so the two match.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, NoReturn

from vergil_tooling.lib import github
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock import history, refs, stale, sweeps
from vergil_tooling.lib.release_lock.model import ReleaseLockError, utc_now
from vergil_tooling.lib.release_lock.sweeps import RearmResult, RearmSweepError, SweepError

if TYPE_CHECKING:
    from vergil_tooling.lib.release_lock.refs import LockState

__all__ = ["ReclaimError", "ReclaimResult", "break_lock", "reclaim_if_stale"]

#: How many compare-and-swap deletes a break makes before it reports a lost race.
BREAK_ATTEMPTS: Final = 3

# Every way posting a comment can fail: a gh failure, no App installation or
# token, and gh not runnable.
_GH_ERRORS = (
    subprocess.CalledProcessError,
    OSError,
    RuntimeError,
    github.AppAuthError,
    github.NoInstallationError,
)


class ReclaimError(ReleaseLockError):
    """A reclaim or break could not be completed or recorded; the message names what failed."""


@dataclass(frozen=True)
class ReclaimResult:
    """A reclaim or break that landed: the lock it removed, and what the re-arm sweep did."""

    repo: str
    action: Literal["reclaim", "break"]
    old_sha: str
    tracking_issue: str
    reason: str
    rearm: RearmResult

    def as_json(self) -> dict[str, object]:
        """The result as one JSON object."""
        return {
            "repo": self.repo,
            "action": self.action,
            "old_sha": self.old_sha,
            "tracking_issue": self.tracking_issue,
            "reason": self.reason,
            "rearm": {
                "armed": list(self.rearm.armed),
                "merged": list(self.rearm.merged),
                "held": list(self.rearm.held),
                "refused": [{"url": url, "reason": why} for url, why in self.rearm.refused],
            },
        }


def _comment(issue: str, body: str) -> None:
    """Post *body* on tracking issue *issue*; :class:`ReclaimError` on any failure.

    *issue* is a lock record's ``tracking_issue``, which :class:`LockRecord`
    holds to the ``<owner>/<repo>#<n>`` form.
    """
    repo, _hash, number = issue.rpartition("#")
    try:
        with github.target_org(repo.split("/", 1)[0]):
            github.run(
                "issue",
                "comment",
                number,
                "--repo",
                repo,
                "--body",
                body,
                echo=False,
            )
    except _GH_ERRORS as exc:
        msg = f"commenting on the lock's tracking issue {issue} failed: {error_text(exc)}"
        raise ReclaimError(msg) from exc


def _append(repo: str, entry: history.HistoryEntry) -> None:
    try:
        history.append(repo, entry)
    except ReleaseLockError as exc:
        msg = f"recording the {entry.action} in {refs.HISTORY_REF} failed: {exc}"
        raise ReclaimError(msg) from exc


def _describe(state: LockState) -> str:
    rec = state.record
    if rec is None:
        return f"an unreadable lock at {state.sha}"
    return (
        f"the lock held by {rec.holder_identity} on {rec.holder_host} for release"
        f" {rec.version} (stage {rec.stage}, health {rec.health})"
    )


def _issue(state: LockState) -> str:
    return state.record.tracking_issue if state.record is not None else ""


def _rearm(repo: str) -> tuple[RearmResult, SweepError | None]:
    """Run the re-arm sweep: its result (what it did before a failure) and its error."""
    try:
        return sweeps.rearm_all(repo), None
    except RearmSweepError as exc:
        return exc.result, exc
    except SweepError as exc:
        return RearmResult(armed=[], merged=[], held=[], refused=[]), exc


def reclaim_if_stale(repo: str, *, actor: str) -> ReclaimResult | None:
    """Reclaim *repo*'s lock if it is stale, as *actor*; ``None`` when it is not, or another won.

    A failed lock read raises :class:`RefReadError` and a failed issue read
    :class:`stale.IssueReadError`: neither ever means stale. Once the delete
    has won, a failed sweep is recorded and its :class:`SweepError`
    propagates; a failed record still lets the sweep run (the lock is gone,
    so the PRs it held must be re-armed), then raises :class:`ReclaimError`
    naming every failure.
    """
    state = refs.read_lock(repo)
    verdict = stale.judge(repo, state, now=datetime.now(UTC))
    if not verdict.stale or state.sha is None:
        return None
    if not refs.delete_lock(repo, expected=state.sha):
        return None
    at = utc_now()
    issue = _issue(state)
    failures: list[str] = []
    try:
        _append(
            repo,
            history.HistoryEntry(
                action="reclaim",
                old=state.text,
                actor=actor,
                at=at,
                reason=verdict.reason,
                rearmed=(),
            ),
        )
    except ReclaimError as exc:
        failures.append(str(exc))
    if issue:
        body = (
            f"vergil: the release lock on `{repo}` ({_describe(state)}) was reclaimed by"
            f" {actor} at {at}, as stale: {verdict.reason}. Its old contents are recorded"
            f" in `{refs.HISTORY_REF}`; the re-arm sweep runs next, and the release has"
            " stopped: it does not re-take the lock."
        )
        try:
            _comment(issue, body)
        except ReclaimError as exc:
            failures.append(str(exc))
    result, sweep_error = _rearm(repo)
    try:
        _append(
            repo,
            history.HistoryEntry(
                action="reclaim-rearm",
                old=state.text,
                actor=actor,
                at=utc_now(),
                reason=str(sweep_error) if sweep_error is not None else "",
                rearmed=tuple(result.armed),
            ),
        )
    except ReclaimError as exc:
        failures.append(str(exc))
    if failures:
        if sweep_error is not None:
            failures.append(f"the re-arm sweep failed: {sweep_error}")
        msg = f"the release lock on {repo} was reclaimed, but: " + "; ".join(failures)
        raise ReclaimError(msg) from sweep_error
    if sweep_error is not None:
        raise sweep_error
    return ReclaimResult(
        repo=repo,
        action="reclaim",
        old_sha=state.sha,
        tracking_issue=issue,
        reason=verdict.reason,
        rearm=result,
    )


def _same_lock(recorded: LockState, now: LockState) -> bool:
    """Whether *now* is still the lock the break recorded: same holder token, or same text."""
    if recorded.record is not None:
        return now.record is not None and now.record.token == recorded.record.token
    return now.record is None and now.text == recorded.text


def _not_landed(repo: str, recorded: LockState, *, actor: str, reason: str, why: str) -> NoReturn:
    """Record that the break of *recorded* did not land, then raise :class:`ReclaimError`."""
    at = utc_now()
    failures: list[str] = []
    try:
        _append(
            repo,
            history.HistoryEntry(
                action="break-failed",
                old=recorded.text,
                actor=actor,
                at=at,
                reason=f"{reason}; the break did not land: {why}",
                rearmed=(),
            ),
        )
    except ReclaimError as exc:
        failures.append(str(exc))
    issue = _issue(recorded)
    if issue:
        body = (
            f"vergil: the break of the release lock on `{repo}` by {actor} did not land"
            f" ({at}): {why}. The break recorded above did not remove the lock; nothing"
            " was re-armed."
        )
        try:
            _comment(issue, body)
        except ReclaimError as exc:
            failures.append(str(exc))
    msg = f"the break of the release lock on {repo} did not land: {why}"
    if failures:
        msg += "; recording that also failed: " + "; ".join(failures)
    raise ReclaimError(msg)


def break_lock(repo: str, *, actor: str, reason: str) -> ReclaimResult:
    """Break *repo*'s lock as *actor*, for *reason*: record it, delete it, re-arm.

    Human only: the caller (``vrg-release-lock break``) refuses every agent
    identity and agent signal. Raises :class:`ValueError` for an empty
    *reason*, and :class:`ReclaimError` when no lock is held, when recording
    the break fails (nothing is deleted), when the delete does not land (see
    the module docstring), and when the re-arm sweep fails after the lock is
    gone. A failed lock read raises :class:`RefReadError`.
    """
    reason = reason.strip()
    if not reason:
        msg = "a break needs a non-empty reason"
        raise ValueError(msg)
    state = refs.read_lock(repo)
    if state.sha is None:
        msg = f"no release lock is held on {repo}: there is nothing to break"
        raise ReclaimError(msg)
    recorded = state
    entry_reason = reason
    if state.record is None:
        entry_reason = f"{reason} (the lock was unreadable: {state.error})"
    at = utc_now()
    _append(
        repo,
        history.HistoryEntry(
            action="break", old=state.text, actor=actor, at=at, reason=entry_reason, rearmed=()
        ),
    )
    issue = _issue(state)
    if issue:
        _comment(
            issue,
            f"vergil: the release lock on `{repo}` ({_describe(state)}) is being broken by"
            f" {actor} at {at}: {reason}. Its old contents are recorded in"
            f" `{refs.HISTORY_REF}`; the lock is deleted next, then the re-arm sweep runs."
            " The release has stopped: it does not re-take the lock.",
        )
    sha = state.sha
    attempt = 1
    while not refs.delete_lock(repo, expected=sha):
        now = refs.read_lock(repo)
        if now.sha is None:
            _not_landed(
                repo,
                recorded,
                actor=actor,
                reason=reason,
                why="the lock is no longer held: another writer removed it before the delete",
            )
        if not _same_lock(recorded, now):
            _not_landed(
                repo,
                recorded,
                actor=actor,
                reason=reason,
                why=f"the lock was replaced by another holder's ({_describe(now)})",
            )
        if attempt == BREAK_ATTEMPTS:
            _not_landed(
                repo,
                recorded,
                actor=actor,
                reason=reason,
                why=(
                    f"its compare-and-swap delete lost {BREAK_ATTEMPTS} times to writes of"
                    " the lock (a live holder's heartbeat)"
                ),
            )
        sha = now.sha
        attempt += 1
    result, sweep_error = _rearm(repo)
    if sweep_error is not None:
        msg = (
            f"the release lock on {repo} was broken (recorded in {refs.HISTORY_REF}), but the"
            f" re-arm sweep failed: {sweep_error}"
        )
        raise ReclaimError(msg) from sweep_error
    return ReclaimResult(
        repo=repo,
        action="break",
        old_sha=sha,
        tracking_issue=issue,
        reason=reason,
        rearm=result,
    )
