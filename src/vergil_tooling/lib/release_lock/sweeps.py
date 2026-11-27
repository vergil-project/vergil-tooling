"""The release lock's disarm and re-arm sweeps.

Spec §5.1, §5.3 (steps 1.3, 1.4 and 2.2) and §5.4 of epic
vergil-project/.github#1664. A release that has written the lock quiesces
``develop`` with :func:`quiesce`, and at the unlock re-arms it with
:func:`rearm_all`:

- :func:`wait_for_arming_refs` waits until every arming ref is gone,
  deleting one that has expired (the rule below).
- :func:`disarm_all` turns auto-merge off on every open PR into ``develop``
  and confirms each is off. It is the only code that turns auto-merge off
  (spec §5.1); ``tests/vergil_tooling/test_one_pr_path_guard.py`` holds every
  other module to that.
- :func:`quiesce` is the full sequence: the wait, the disarm sweep, a second
  disarm sweep if the wait deleted any arming ref, then a final check that
  lists the arming refs and every open PR's auto-merge state once more and
  disarms any PR found armed.
- :func:`rearm_all` offers every open, non-draft PR into ``develop`` with
  auto-merge off to the one PR path (:func:`pr_path.arm`), which works out
  each PR's merge method itself, so the sweep needs no record of what a
  disarm turned off. It is idempotent: a re-run offers only the PRs still
  unarmed. A ``refused`` or ``held`` arm is reported, never raised; so is a
  collision with another caller arming the same PR (an agent's watch
  re-arming its own PR the moment the lock is gone): the sweep reads the
  PR's auto-merge state and counts it as armed or merged, or reports it
  refused with the collision as its reason.

Drafts cannot be auto-merged, so the re-arm sweep skips them; the disarm
sweep lists them too and finds nothing to turn off. Every failed GitHub
call, ref read or ref write raises :class:`SweepError`, naming what failed
(spec §6): a failure is never read as "nothing armed" or "nothing to wait
for". Every read is ``gh``'s JSON (``--json``, GraphQL), and a write is
judged by reading the PR's state back, never by the text of an error.

The release-side arming-ref expiry rule
---------------------------------------

The arming side of the gate (:func:`pr_path.arm`) writes a PR's arming ref
by a create-only push, *then* reads the lock; the release writes the lock,
*then* runs :func:`wait_for_arming_refs`. A crashed arming side would block
every release, so an arming ref expires. The rule, and all of it, is here:

1. **Who.** Only a release that has written the lock runs the wait, and it
   waits on every arming ref, whoever wrote it, with no trusted-author
   check (§5.10).
2. **The clock.** Expiry is measured from the wait's own first sighting of
   a ref's current value, never from a time the ref carries. Any change of
   the value (the arming side's renewal; each write carries a fresh nonce)
   restarts it, as does a ref that goes and comes back. A value is expired
   only once this call has seen it unchanged for :data:`ARMING_EXPIRY_SECS`.
   Each call starts its own clock: nothing is carried over from an earlier
   wait.
3. **The delete.** An expired value is deleted only by compare-and-swap
   against that value (:func:`arming.delete`). A mismatch (it changed
   between the listing and the delete) deletes nothing, and the new value
   is measured afresh.
4. **The bound.** The whole wait is bounded by
   :data:`ARMING_WAIT_BOUND_SECS`; past it the wait raises
   :class:`SweepError`, naming the refs still there.
5. **After a delete.** :func:`quiesce` runs the disarm sweep again, then the
   final check, before it returns.

Why this keeps the race closed (the **quiesce guarantee**: when
:func:`quiesce` returns, no PR into ``develop`` is armed, and none can be
armed by a call that started before the lock was written). The arming side
renews its ref by compare-and-swap immediately before the arm call, aborts
if the ref is gone, and bounds the arm call, retries included, by
:data:`ARM_CALL_BOUND_SECS`, well below the expiry. So the value the release
deletes is either one written before the renewal, whose renewal then fails
and arms nothing, or the renewed value, seen unchanged for the full expiry
from a sighting no earlier than the renewal: the arm call has ended by the
time it is deleted, and the second disarm sweep, which runs after the
delete, turns off whatever it armed. An arming ref that appears after the
wait has seen none was written after the lock, so its caller reads the lock
as held and arms nothing; the final check reports any it sees
(:attr:`QuiesceResult.arming_at_check`) and disarms any PR armed all the
same.

Anything else that deletes an arming ref (an arming-side clean-up of a
stale ref outside a release, say) must keep that guarantee: it must never
delete, while a lock is held, a ref whose value this rule has not expired,
and never delete a value whose arm call could still land unseen by the
quiesce's disarm sweeps.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import github, pr_path
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock import arming, status
from vergil_tooling.lib.release_lock.model import (
    ARMING_EXPIRY_SECS,
    ARMING_POLL_SECS,
    ARMING_WAIT_BOUND_SECS,
    RefReadError,
    RefWriteError,
    ReleaseLockError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "QuiesceResult",
    "RearmResult",
    "RearmSweepError",
    "RefReadError",
    "RefWriteError",
    "SweepError",
    "disarm_all",
    "quiesce",
    "rearm_all",
    "wait_for_arming_refs",
]

_OPEN = "OPEN"
_ENDED = ("MERGED", "CLOSED")

_DISABLE_AUTO_MERGE_MUTATION = """
mutation($id: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $id}) {
    pullRequest { number }
  }
}
"""

# Every way a gh read or GraphQL call can fail: a gh failure, a response
# that does not parse (or a GraphQL error), no App installation or token,
# and gh not runnable.
_GH_ERRORS = (
    subprocess.CalledProcessError,
    ValueError,
    OSError,
    RuntimeError,
    github.AppAuthError,
    github.NoInstallationError,
)


class SweepError(ReleaseLockError):
    """A step of a sweep could not be completed; the message names what failed."""


@dataclass(frozen=True)
class RearmResult:
    """What :func:`rearm_all` did with each PR it offered to the one PR path.

    *refused* pairs each refused PR with the reason; a collision with
    another caller that left the PR unarmed is one.
    """

    armed: list[str]
    merged: list[str]
    held: list[str]
    refused: list[tuple[str, str]]


class RearmSweepError(SweepError):
    """The re-arm sweep could not offer some PRs; it offered every other one first.

    *result* is what the sweep did with the rest, and *failed* pairs each PR
    it could not offer with the error.
    """

    def __init__(self, result: RearmResult, failed: list[tuple[str, str]]) -> None:
        self.result = result
        self.failed = failed
        lines = "; ".join(f"{url}: {error}" for url, error in failed)
        super().__init__(f"the re-arm sweep could not offer {len(failed)} PR(s): {lines}")


@dataclass(frozen=True)
class QuiesceResult:
    """What :func:`quiesce` did.

    *expired*: the PRs whose arming refs the wait deleted as expired.
    *disarmed*: the PRs the disarm sweeps turned off (the second sweep's
    included). *late*: the PRs the final check found armed and turned off.
    *arming_at_check*: the arming refs the final check listed, each written
    after the lock, whose caller reads the lock as held.
    """

    expired: tuple[int, ...]
    disarmed: tuple[str, ...]
    late: tuple[str, ...]
    arming_at_check: tuple[int, ...]


def _list_arming(repo: str) -> dict[int, str]:
    try:
        return arming.list_arming(repo)
    except ReleaseLockError as exc:
        msg = f"listing the arming refs of {repo} failed: {exc}"
        raise SweepError(msg) from exc


def _ref_names(refs: dict[int, str]) -> str:
    return ", ".join(f"{arming.arming_ref(pr)} ({sha[:12]})" for pr, sha in sorted(refs.items()))


def wait_for_arming_refs(
    repo: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[int]:
    """Wait until every arming ref of *repo* is gone, deleting each that expires.

    The release-side expiry rule is in the module docstring: a value seen
    unchanged by this call for :data:`ARMING_EXPIRY_SECS` is deleted by
    compare-and-swap. Returns the PRs whose arming refs it deleted, in the
    order deleted. Past :data:`ARMING_WAIT_BOUND_SECS` it raises
    :class:`SweepError` naming the refs still there; a failed listing or
    delete raises it too.
    """
    start = clock()
    first_seen: dict[int, tuple[str, float]] = {}
    deleted: list[int] = []
    while True:
        listed = _list_arming(repo)
        if not listed:
            return deleted
        now = clock()
        seen: dict[int, tuple[str, float]] = {}
        for pr, sha in listed.items():
            earlier = first_seen.get(pr)
            if earlier is not None and earlier[0] == sha:
                seen[pr] = earlier
            else:
                seen[pr] = (sha, now)
                print(f"waiting on {arming.arming_ref(pr)} ({sha[:12]}) to clear")
        first_seen = seen
        remaining: dict[int, str] = {}
        for pr, (sha, since) in sorted(first_seen.items()):
            if now - since >= ARMING_EXPIRY_SECS and _delete_expired(repo, pr, sha):
                deleted.append(pr)
                continue
            remaining[pr] = sha
        for pr in deleted:
            first_seen.pop(pr, None)
        if not remaining:
            continue
        elapsed = now - start
        if elapsed >= ARMING_WAIT_BOUND_SECS:
            msg = (
                f"the arming refs of {repo} did not clear within {ARMING_WAIT_BOUND_SECS}s: "
                f"{_ref_names(remaining)}"
            )
            raise SweepError(msg)
        sleep(min(ARMING_POLL_SECS, ARMING_WAIT_BOUND_SECS - elapsed))


def _delete_expired(repo: str, pr: int, sha: str) -> bool:
    """Delete PR *pr*'s expired arming ref at *sha*: False when it changed meanwhile."""
    try:
        done = arming.delete(repo, pr, expected=sha)
    except ReleaseLockError as exc:
        msg = f"deleting the expired arming ref of #{pr} ({arming.arming_ref(pr)}) failed: {exc}"
        raise SweepError(msg) from exc
    if done:
        print(
            f"deleted {arming.arming_ref(pr)} ({sha[:12]}): unchanged for {ARMING_EXPIRY_SECS}s"
        )
    return done


def _develop_prs(repo: str) -> list[dict[str, object]]:
    try:
        return status.develop_prs(repo)
    except ReleaseLockError as exc:
        raise SweepError(str(exc)) from exc


def _pr_view(url: str, fields: str, step: str) -> dict[str, object]:
    try:
        data = github.read_json("pr", "view", url, "--json", fields)
    except _GH_ERRORS as exc:
        msg = f"{url}: {step} failed: {error_text(exc)}"
        raise SweepError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{url}: {step}: gh pr view returned {data!r}"
        raise SweepError(msg)
    return data


def _disarm(url: str) -> bool:
    """Turn auto-merge off on *url* and confirm it: False when the PR ended meanwhile."""
    node_id = _pr_view(url, "id", "reading the PR's node ID").get("id")
    if not isinstance(node_id, str) or not node_id:
        msg = f"{url}: turning auto-merge off: the PR has no node ID"
        raise SweepError(msg)
    failure = ""
    try:
        github.graphql(_DISABLE_AUTO_MERGE_MUTATION, id=node_id)
    except _GH_ERRORS as exc:
        failure = error_text(exc)
    # The mutation is retried on a transient error, so an attempt that landed
    # with its response lost can be followed by one that fails: the PR read
    # back is the judge, never the error's text.
    data = _pr_view(url, "state,autoMergeRequest", "reading auto-merge back")
    state, request = data.get("state"), data.get("autoMergeRequest")
    if state == _OPEN and request is None:
        if failure:
            print(f"{url}: turning auto-merge off returned an error, but it is off: {failure}")
        return True
    if state in _ENDED:
        print(f"{url}: it was {str(state).lower()} before auto-merge was turned off")
        return False
    if state != _OPEN:
        msg = f"{url}: turning auto-merge off: its state reads {state!r}"
        raise SweepError(msg)
    if failure:
        msg = f"{url}: turning auto-merge off failed: {failure}"
        raise SweepError(msg)
    msg = f"{url}: auto-merge is still on after turning it off (autoMergeRequest {request!r})"
    raise SweepError(msg)


def disarm_all(repo: str) -> list[str]:
    """Turn auto-merge off on every open PR into ``develop`` of *repo*, confirming each.

    Drafts are listed too: they cannot be armed, so there is nothing to turn
    off. Returns the PRs turned off; a PR that merged or closed before its
    disarm is not one. Any failure raises :class:`SweepError`.
    """
    disarmed: list[str] = []
    for item in _develop_prs(repo):
        if item["autoMergeRequest"] is None:
            continue
        url = str(item["url"])
        if _disarm(url):
            disarmed.append(url)
    return disarmed


def quiesce(
    repo: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> QuiesceResult:
    """Quiesce ``develop`` of *repo* for a release that has written the lock.

    :func:`wait_for_arming_refs`, then :func:`disarm_all`, then
    :func:`disarm_all` again if the wait deleted any arming ref, then the
    final check: the arming refs are listed once more, and every open PR's
    auto-merge state, any PR found armed being turned off. *clock* and
    *sleep* are the wait's. Any failure raises :class:`SweepError`.
    """
    expired = wait_for_arming_refs(repo, clock=clock, sleep=sleep)
    disarmed = disarm_all(repo)
    if expired:
        disarmed += disarm_all(repo)
    at_check = _list_arming(repo)
    late = disarm_all(repo)
    return QuiesceResult(
        expired=tuple(expired),
        disarmed=tuple(disarmed),
        late=tuple(late),
        arming_at_check=tuple(sorted(at_check)),
    )


def _after_collision(url: str, collision: pr_path.ArmingCollisionError) -> tuple[str, str]:
    """Count a PR another caller was arming: ``(outcome, detail)`` from its auto-merge state."""
    data = _pr_view(url, "state,autoMergeRequest", "reading auto-merge after a collision")
    state, request = data.get("state"), data.get("autoMergeRequest")
    if state == "MERGED":
        return "merged", ""
    if state == _OPEN and request is not None:
        return "armed", ""
    return "refused", str(collision)


def rearm_all(repo: str, *, holder_pass: str | None = None) -> RearmResult:
    """Offer every open, non-draft PR into ``develop`` with auto-merge off to the one PR path.

    *holder_pass* is passed to :func:`pr_path.arm`. A ``refused`` or ``held``
    arm is reported, and so is a collision with another caller (see the
    module docstring). A PR that cannot be offered (a :class:`PRPathError`
    or a failed read) does not stop the sweep: every other PR is offered
    first, then :class:`RearmSweepError` names each failure and carries the
    result. A failed listing raises :class:`SweepError`.
    """
    armed: list[str] = []
    merged: list[str] = []
    held: list[str] = []
    refused: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for item in _develop_prs(repo):
        if item["isDraft"] or item["autoMergeRequest"] is not None:
            continue
        url = str(item["url"])
        outcome: str
        detail: str
        try:
            result = pr_path.arm(url, holder_pass=holder_pass)
            outcome, detail = result.outcome, result.detail
        except pr_path.ArmingCollisionError as exc:
            try:
                outcome, detail = _after_collision(url, exc)
            except SweepError as read_exc:
                failed.append((url, f"{exc}; then {read_exc}"))
                continue
        except pr_path.PRPathError as exc:
            failed.append((url, str(exc)))
            continue
        if outcome == "armed":
            armed.append(url)
        elif outcome == "merged":
            merged.append(url)
        elif outcome == "held":
            held.append(url)
        else:
            refused.append((url, detail))
    result = RearmResult(armed=armed, merged=merged, held=held, refused=refused)
    if failed:
        raise RearmSweepError(result, failed)
    return result
