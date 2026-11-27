"""Shared wait-and-merge engine with fail-fast ordering.

Used by the release PR's merge into ``main`` (merge strategy) through
:func:`wait_and_merge`, which merges only a PR whose base is ``main``, and by
the mechanized-PR building block, ``vrg-update-deps`` and the release's
back-merge through :func:`wait_until_merged`, which shares the same loop
pieces but treats a merged PR as success, runs under one overall deadline
(epic vergil-project/.github#1064) and only watches: it never merges, leaving
the merge to the auto-merge the one PR path armed (epic
vergil-project/.github#1664). Doomed outcomes — already merged, draft,
conflicting, behind — are checked *before* waiting, never after
letting a pointless CI run finish:

- MERGED: the caller's premise is wrong. What "already merged" means
  is a caller-level decision, so the engine raises. ``vrg-pr-await`` does not use this
  engine: it waits on auto-merge and reports a merge as success.
- Draft: can go green but ``gh pr merge`` refuses it.
- CONFLICTING: cannot merge no matter what CI says. Re-checked every
  iteration — a conflict can arise mid-loop when another PR merges.
- BEHIND: the current CI run is irrelevant; update-branch cancels it
  and starts a fresh one, so update immediately instead of waiting.

The BEHIND precheck reads ``mergeStateStatus``, which GitHub computes lazily
and serves stale for a window after the base branch advances — so in a merge
train (the old serial batch finalize, issue #1673) a freshly-behind branch can pass the
precheck and only be caught by the merge endpoint itself, which rejects with
"the head branch is not up to date with the base branch." That authoritative
rejection is fed back into the same update-and-retry path rather than surfaced
as a hard failure (issue #2856). Failed checks get the same treatment: a branch
that is BEHIND when its checks come back red is updated and re-checked rather
than aborted, since those failures may be stale runs against the old base
(issue #2914).

"Green" means every registered check is terminal **and** every required status
check of the base branch is registered and terminal (issue #3061). CI aggregator
jobs (``test / evidence`` …) only register once their dependencies finish, so
there is a window where every *registered* check is done yet a required one does
not exist; merging there is refused with "the base branch policy prohibits the
merge". The engine resolves the required set once, keeps waiting while any
required check is outstanding, and treats that policy rejection with no failed
checks as *not ready* — poll and re-attempt — rather than fatal, bounded by the
shared pending-checks deadline (``github.checks_timeout_secs``). A review requirement
is surfaced at once. When the check wait itself expires with checks still pending,
the engine aborts naming each pending check and its link (issue #3170). A check
whose own Actions job is in progress is reported as likely slow, not stuck, with the
run's URL and the advice to wait for it and then resume; any other pending check
keeps the re-run / close-and-reopen advice (issue #4200). A required check that a
job's upload creates (``Semgrep OSS``) never registers when that job fails first, so
the wait ends as soon as that producing job has failed, even with other checks still
running, and the abort names the unregistered check and the failed job with its
result, instead of waiting out the deadline (issue #4294).

A caller may ask for one merge to use the caller's ruleset bypass
(``bypass_ruleset``): the release PR's merge into ``main``, which the "Main
updates" ruleset lets only its bypass actor (the human, through the repository
admin role) make. ``gh pr merge --admin`` also skips the checks and reviews the
base branch requires, so the engine re-proves them itself, at a head it then pins
with ``--match-head-commit``, immediately before the bypassed merge; a refusal of
the bypass stops with a message naming the ruleset instead of polling
(vergil-project/vergil-tooling#5095).
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import github

# Imported directly (not via the module) so the `except` clause holds the
# real class even when tests replace the whole `github` module with a mock.
from vergil_tooling.lib.github import (
    _POLL_INTERVAL_SECS,
    ChecksTimeoutError,
    GitHubAPIError,
    OrphanedCheckError,
    checks_timeout_secs,
    describe_check_results,
    describe_outstanding,
    producer_job_names,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_BRANCH_UPDATES = 5
_UPDATE_SETTLE_SECS = 5
# Poll cadence and ceiling while the PR is "not ready" — required checks not yet
# registered/terminal, or a branch-policy rejection with no failed checks (#3061).
# The ceiling is the shared pending-checks deadline (checks_timeout_secs(), read
# per call so $VRG_CHECKS_TIMEOUT applies) so the waits never drift.
_NOT_READY_POLL_SECS = _POLL_INTERVAL_SECS
# How long the watch-only wait_until_merged sleeps between reads of a ready PR
# it is waiting for GitHub's auto-merge to merge: long enough that a PR left
# with auto-merge off does not spend the token's API quota until the deadline.
_MERGE_POLL_SECS = 30
# The command to run again once the checks can finish.
_RESUME = "vrg-release --resume for a release, vrg-update-deps for a dependency update"
# How to get a merge moving again once a wait gives up on a check that is not
# making progress: pending with no backing run, or an app-posted status (#3170).
_RECOVERY_HINT = (
    "Re-run the stuck job (or close and reopen the PR to re-run the gate), then re-run "
    f"the waiting command: {_RESUME}."
)


def _timeout_hint(exc: ChecksTimeoutError) -> str:
    """What to do about a check wait that ran out, told apart by whether each run is going.

    A check whose own Actions job is in progress is most likely slow, not stuck:
    re-running it would throw away its progress, so the hint is to wait for the
    run and then resume (#4200), re-running only a job whose log has stopped
    moving. Any other pending check (a queued job, an app-posted status), or a
    timeout with none pending, keeps the re-run / close-and-reopen advice.
    """
    running = exc.running
    running_names = {name for name, _ in running}
    stuck = [name for name, _ in exc.pending if name not in running_names]
    parts: list[str] = []
    if running:
        runs = ", ".join(f"{name} (run {url})" for name, url in running)
        parts.append(
            f"Still running, not stuck: {runs}; the job is in progress. Wait for the run "
            f"to finish, then re-run the waiting command: {_RESUME}. Re-run the job "
            "instead only if its log has stopped making progress. Set "
            "$VRG_CHECKS_TIMEOUT to wait longer."
        )
    if not running:
        parts.append(_RECOVERY_HINT)
    elif stuck:
        parts.append(f"Not in progress: {', '.join(stuck)}. {_RECOVERY_HINT}")
    return " ".join(parts)


# reviewDecision values that mean branch policy wants a human review — a block
# no amount of waiting on CI clears, so it is surfaced immediately.
_REVIEW_BLOCKS = frozenset({"REVIEW_REQUIRED", "CHANGES_REQUESTED"})


class MergeAbortError(Exception):
    """The PR cannot be merged; the message explains why and what to do."""


class MergeConflictError(MergeAbortError):
    """The PR conflicts with its base branch.

    A distinct subclass so a caller that knows where the PR's branch lives
    can replace the generic message with the exact commands to resolve it
    (issue #3555); every other caller still catches it as a plain
    ``MergeAbortError``.
    """


# Substring of the merge endpoint's rejection when the repo requires branches to
# be up to date and the head is behind base. This is the authoritative signal the
# lazily-computed mergeStateStatus precheck can miss in a merge train (issue #2856).
_BEHIND_REJECTION_SIGNATURE = "not up to date"


def _is_behind_rejection(exc: GitHubAPIError) -> bool:
    """True when a merge rejection means the head branch is behind base."""
    return _BEHIND_REJECTION_SIGNATURE in (exc.stderr or "").lower()


# Substring of the merge endpoint's rejection when a branch-protection or ruleset
# requirement is unmet — most often a required check that has not registered or
# finished yet, but also e.g. a required review (issue #3061).
_POLICY_REJECTION_SIGNATURE = "base branch policy prohibits the merge"


def _is_policy_rejection(exc: GitHubAPIError) -> bool:
    """True when a merge rejection is the generic base-branch-policy block."""
    return _POLICY_REJECTION_SIGNATURE in (exc.stderr or "").lower()


# Substring of the merge endpoint's rejection when ``--match-head-commit`` no
# longer names the PR's head: someone pushed after the batch check ran.
_HEAD_MOVED_REJECTION_SIGNATURE = "head branch was modified"


def _is_head_moved_rejection(exc: GitHubAPIError) -> bool:
    """True when a pinned merge was refused because the PR's head moved."""
    return _HEAD_MOVED_REJECTION_SIGNATURE in (exc.stderr or "").lower()


# Substrings of a merge rejection that a ruleset or branch protection refused the
# update itself: what a bypassed merge gets when the caller is not a bypass actor.
_RULESET_REJECTION_SIGNATURES = (
    _POLICY_REJECTION_SIGNATURE,
    "repository rule violation",
    "protected ref",
    "protected branch",
)


def _is_ruleset_rejection(exc: GitHubAPIError) -> bool:
    """True when a merge rejection is a ruleset or branch-protection refusal."""
    stderr = (exc.stderr or "").lower()
    return any(signature in stderr for signature in _RULESET_REJECTION_SIGNATURES)


def _bypass_refused(pr: str, ruleset: str, exc: GitHubAPIError) -> MergeAbortError:
    """The abort for a bypassed merge that GitHub refused under a ruleset."""
    detail = (exc.stderr or "").strip()
    return MergeAbortError(
        f"PR {pr} could not be merged with the ruleset bypass: GitHub refused it "
        f"({detail}). Only a bypass actor of the {ruleset!r} ruleset may merge it, "
        "and that is the repository admin role: run the release as a repository "
        "admin, then re-run vrg-release --resume."
    )


def _bypass_head(pr: str, required: frozenset[str]) -> str:
    """Re-prove the merge requirements the bypass would skip; return the head read.

    ``gh pr merge --admin`` skips every check and review the base branch
    requires, so the engine checks them itself (#5095). The head is read
    first, then the checks and the review decision: if the head moves after
    it is read, the merge pinned to it is refused by GitHub, so a merge never
    lands at a head other than the one whose checks were read here.
    """
    head = github.head_sha(pr)
    failed = github.failed_check_names(pr)
    if failed:
        msg = f"Checks failed on PR {pr}: {', '.join(failed)}"
        raise MergeAbortError(msg)
    outstanding = github.required_checks_outstanding(pr, required)
    if outstanding:
        msg = (
            f"PR {pr} has required checks outstanding: {describe_outstanding(outstanding)}; "
            "refusing a ruleset-bypassed merge until they pass."
        )
        raise MergeAbortError(msg)
    review = github.merge_status(pr).get("reviewDecision", "")
    if review in _REVIEW_BLOCKS:
        msg = (
            f"PR {pr} is blocked by base branch policy: reviewDecision={review}. "
            "It needs an approving review before it can merge."
        )
        raise MergeAbortError(msg)
    return head


# --- The loop's shared pieces ------------------------------------------------
#
# ``wait_and_merge`` and ``wait_until_merged`` share these, so the fail-fast
# ordering, the BEHIND handling and the update cap are written once.


def _resolve_required(pr: str) -> tuple[frozenset[str] | None, frozenset[str]]:
    """The base branch's required checks: the lookup result and the gate set.

    The lookup result is ``None`` when the required set could not be read; the
    gate set is then empty, so the engine falls back to registered checks.
    """
    resolved = github.required_check_names(pr)
    return resolved, resolved if resolved is not None else frozenset()


def _refuse_unmergeable_shape(pr: str) -> None:
    """The draft and conflict checks: raise when *pr* cannot merge whatever CI says.

    A draft can go green but ``gh pr merge`` refuses it. A conflict is re-checked
    on every iteration, since it can arise mid-loop when another PR merges.
    """
    if github.is_draft(pr):
        msg = f"PR {pr} is a draft — mark it ready (gh pr ready {pr}) and re-run."
        raise MergeAbortError(msg)
    if github.mergeable(pr) == "CONFLICTING":
        msg = (
            f"PR {pr} has merge conflicts. Resolve them in the PR's worktree "
            "(merge the target branch in, push), then re-run."
        )
        raise MergeConflictError(msg)


def _is_behind(pr: str) -> bool:
    """The behind check: True when *pr*'s branch is behind its base."""
    return github.merge_state_status(pr) == "BEHIND"


def _unregistered_detail(pr: str, unregistered: list[str], failed: list[str]) -> str:
    """Name each unregistered required check and, where known, the failed job behind it.

    A check with a known producing job (``github.UPLOADED_CHECK_JOBS``) names
    that job and its result, read from the PR's checks; any other is named
    with the failed checks as the likely cause (#4294).
    """
    parts: list[str] = []
    unknown: list[str] = []
    checks: list[dict[str, str]] | None = None
    for check in unregistered:
        producers = producer_job_names(check, failed)
        if not producers:
            unknown.append(check)
            continue
        if checks is None:
            checks = github.pr_checks(pr)
        parts.append(
            f"Required check {check} was never registered and will not arrive: its "
            f"producing job {describe_check_results(checks, producers)} ended before "
            "uploading it."
        )
    if unknown:
        parts.append(
            f"Required checks never registered: {', '.join(unknown)}; a failed job "
            "above most likely produces them."
        )
    return " ".join(parts)


def _failed_checks_are_stale(pr: str, required: frozenset[str]) -> bool:
    """Judge *pr*'s failed checks after a wait: stale (update the branch) or real (raise).

    Returns False when no check failed, and True when some did but the branch is
    BEHIND. The loop-top BEHIND precheck can miss a freshly-behind branch (lazy
    mergeStateStatus), and the wait returns at once on checks that are already
    terminal, so those may be stale failures from before the base advanced: the
    caller updates the branch (which cancels them and starts a fresh run) rather
    than going back through the precheck that just missed (issue #2914). A
    failure on a branch that is not behind is real and raises. The waiter also
    returns once a failure leaves a *required* check unregistered for good
    (#4294), so the abort names each such check and, for one a job's upload
    creates, the failed job that never uploaded it.
    """
    failed = github.failed_check_names(pr)
    if not failed:
        return False
    if _is_behind(pr):
        return True
    msg = f"Checks failed on PR {pr}: {', '.join(failed)}"
    outstanding = github.required_checks_outstanding(pr, required)
    unregistered = [name for name, why in outstanding.items() if why == "not registered"]
    if unregistered:
        msg = f"{msg}. {_unregistered_detail(pr, unregistered, failed)}"
    raise MergeAbortError(msg)


def _checks_wait_abort(pr: str, exc: OrphanedCheckError | ChecksTimeoutError) -> MergeAbortError:
    """The abort for a check wait that ended in an orphaned check or a timeout."""
    if isinstance(exc, OrphanedCheckError):
        # The waiter's message names each orphaned check, why it cannot pass
        # (e.g. a required check GitHub will block on, #3379) and the
        # close-and-reopen recovery, so carry it through verbatim.
        return MergeAbortError(f"PR {pr} cannot be merged (orphaned check-run): {exc}")
    # exc.stderr already names every pending check and its link.
    detail = (exc.stderr or "").strip()
    return MergeAbortError(f"PR {pr} cannot be merged yet: {detail}. {_timeout_hint(exc)}")


class _BranchUpdater:
    """The update cap and the branch update, shared by every behind path.

    The mergeStateStatus precheck, the failed-checks gate (#2914) and the merge
    endpoint's own stale-behind rejection all update through one instance, so
    one cap of :data:`_MAX_BRANCH_UPDATES` bounds them all. A batch PR is
    rebased, not merged into, and re-checked after the update.
    """

    def __init__(
        self,
        pr: str,
        *,
        batch_check: Callable[[], str] | None = None,
        on_branch_updated: Callable[[], None] | None = None,
    ) -> None:
        self.pr = pr
        self.batch_check = batch_check
        self.on_branch_updated = on_branch_updated
        self.updates = 0

    def spend(self) -> None:
        """Count one branch update or head re-check; abort past the cap."""
        self.updates += 1
        if self.updates > _MAX_BRANCH_UPDATES:
            msg = (
                f"PR {self.pr} still behind after {_MAX_BRANCH_UPDATES} branch updates "
                "— the merge train is busy; re-run when it settles."
            )
            if self.batch_check is not None:
                msg = (
                    f"PR {self.pr} still behind or moving after {_MAX_BRANCH_UPDATES} branch "
                    "updates and head re-checks — the merge train is busy or the "
                    "branch is still being pushed; re-run when it settles."
                )
            raise MergeAbortError(msg)

    def update(self) -> None:
        """Update the behind branch, or abort once the merge-train cap is hit."""
        self.spend()
        try:
            if self.batch_check is None:
                print("Branch is behind base — updating and re-checking...")
                github.update_branch(self.pr)
            else:
                print("Branch is behind base — updating by rebase and re-checking...")
                github.update_branch_rebase(self.pr, expected_head=github.head_sha(self.pr))
        except GitHubAPIError as exc:
            msg = f"update-branch failed for PR {self.pr}: {exc}"
            raise MergeAbortError(msg) from exc
        time.sleep(_UPDATE_SETTLE_SECS)
        if self.on_branch_updated is not None:
            self.on_branch_updated()
        if self.batch_check is not None:
            self.batch_check()


class _NotReadyWait:
    """Pause one poll while the PR is not ready, bounded by a shared deadline.

    The deadline starts at the first not-ready observation and is shared by every
    later one, so the retry loop is always bounded. *clamp*, when given, is an
    outer ``time.monotonic()`` deadline no pause may run past (a caller's
    overall deadline).
    """

    def __init__(self, pr: str, *, clamp: float | None = None) -> None:
        self.pr = pr
        self.timeout = checks_timeout_secs()
        self.deadline: float | None = None
        self.clamp = clamp

    def pause(self, reason: str) -> None:
        """Sleep one poll, or abort once the not-ready deadline has passed."""
        now = time.monotonic()
        if self.deadline is None:
            self.deadline = now + self.timeout
        elif now >= self.deadline:
            msg = (
                f"PR {self.pr} is still not mergeable after {self.timeout}s: {reason}. "
                "Check the base branch's protection rules and rulesets, then re-run."
            )
            raise MergeAbortError(msg)
        secs: float = _NOT_READY_POLL_SECS
        if self.clamp is not None:
            secs = max(0.0, min(secs, self.clamp - now))
        print(f"Not ready to merge — {reason}; re-checking in {_NOT_READY_POLL_SECS}s...")
        time.sleep(secs)


def _policy_block_reason(pr: str, required: frozenset[str], resolved: frozenset[str] | None) -> str:
    """Explain a base-branch-policy rejection, or abort if it cannot clear by waiting.

    A failed check or a required review will not clear by polling, so both
    abort now; otherwise the rejection is "not ready" and the reason names
    what is still awaited.
    """
    failed = github.failed_check_names(pr)
    if failed:
        msg = f"Checks failed on PR {pr}: {', '.join(failed)}"
        raise MergeAbortError(msg)
    status = github.merge_status(pr)
    review = status.get("reviewDecision", "")
    if review in _REVIEW_BLOCKS:
        msg = (
            f"PR {pr} is blocked by base branch policy: reviewDecision={review}. "
            "It needs an approving review before it can merge."
        )
        raise MergeAbortError(msg)
    prefix = "the base branch policy prohibits the merge"
    outstanding = github.required_checks_outstanding(pr, required)
    if outstanding:
        return f"{prefix}; awaiting required checks: {describe_outstanding(outstanding)}"
    if resolved is None:
        return (
            f"{prefix} with no failed checks; the required checks could not be "
            "read (see warning above)"
        )
    state = status.get("mergeStateStatus", "")
    return (
        f"{prefix} with no failed checks and no outstanding required checks "
        f"(mergeStateStatus={state or 'unknown'})"
    )


#: The one base branch :func:`wait_and_merge` merges into.
_MAIN = "main"


def _require_main_base(pr: str) -> None:
    """Refuse to merge *pr* unless its base is ``main`` (spec §5.4 of epic .github#1664)."""
    base = github.base_ref(pr)
    if base != _MAIN:
        msg = (
            f"Refusing to merge PR {pr}: its base is {base!r}, not {_MAIN!r}. "
            "wait_and_merge serves the release PR into main only; a PR into develop "
            "merges only through the one PR path."
        )
        raise MergeAbortError(msg)


def wait_and_merge(
    pr: str,
    *,
    strategy: str,
    wait_checks: Callable[[str], None] | None = None,
    on_branch_updated: Callable[[], None] | None = None,
    batch_check: Callable[[], str] | None = None,
    bypass_ruleset: str | None = None,
) -> None:
    """Block until *pr* is green and current, then merge it.

    ``wait_checks`` lets callers substitute their own check-waiting
    primitive (the release workflow passes its verbose-aware wrapper);
    the default is ``github.wait_for_checks``.

    ``on_branch_updated`` is called after each successful server-side
    update-branch, so a caller that holds a local copy of the head branch
    (the release workflow) learns that GitHub moved it (#3175). The engine
    itself stays local-checkout agnostic; an exception from the hook
    propagates unchanged.

    ``batch_check`` marks a batch PR (epic vergil-project/.github#1019). It
    fetches the PR's head, checks the commit-per-issue invariant on it, and
    returns the SHA it checked; a failed check raises, and the engine lets
    that propagate unchanged. With it, a behind PR is updated by rebase
    (``github.update_branch_rebase``), never by merging its base in, and
    ``batch_check`` runs after every update and immediately before the merge
    call. The merge is then pinned to the checked SHA
    (``--match-head-commit``), so a head pushed after the check is refused by
    GitHub and re-checked, never merged unchecked. A refused pin counts
    against the same update cap as a branch update. Without it, the engine
    behaves exactly as before.

    ``bypass_ruleset`` names the ruleset whose bypass the merge uses
    (``gh pr merge --admin``): the release workflow's merge of its release
    PR into ``main`` (#5095), and no other caller. The bypass also skips the
    base branch's required checks and reviews, so immediately before the
    merge the engine re-checks them itself (no failed check, no required
    check outstanding, no review required) at a head it pins with
    ``--match-head-commit``. A head moved after that read is refused by
    GitHub and re-checked, within the update cap. A refusal of the bypass
    itself (the caller is not the ruleset's bypass actor) raises naming the
    ruleset, never a poll. A required set that cannot be read raises before
    any wait, since the bypass would skip whatever it holds. It cannot be
    combined with ``batch_check``.

    The base branch's required status checks are resolved once up front (a
    lookup failure warns and falls back to registered checks only; a PR whose
    URL names no repo raises). Whatever waiter runs, the engine re-checks the
    required set afterwards, so a custom waiter that only watches registered
    checks still cannot merge early.

    It merges only a PR whose base is ``main``: nothing but the one PR path
    (:mod:`vergil_tooling.lib.pr_path`) merges a PR into ``develop`` (epic
    vergil-project/.github#1664, spec §5.4). The base is read before any
    wait and again immediately before each merge call; any other base raises
    :class:`MergeAbortError` with nothing merged.

    Raises ``MergeAbortError`` on any unmergeable condition.
    """
    if bypass_ruleset is not None and batch_check is not None:
        msg = "bypass_ruleset and batch_check cannot be combined"
        raise ValueError(msg)
    _require_main_base(pr)
    resolved, required = _resolve_required(pr)
    if bypass_ruleset is not None and resolved is None:
        # Without the required set the engine cannot prove what --admin would
        # skip, and GitHub no longer enforces it, so never fall back here.
        msg = (
            f"PR {pr}: the base branch's required checks could not be read (see warning "
            f"above), so the {bypass_ruleset!r} ruleset bypass will not be used. Re-run "
            "once they can be read."
        )
        raise MergeAbortError(msg)
    if wait_checks is not None:
        wait = wait_checks
    else:

        def wait(p: str) -> None:
            # Pass the lookup result through as-is: ``None`` tells the waiter
            # the required set is unknown, so it falls back to registered
            # checks but never passes a stale check (#3379).
            github.wait_for_checks(p, required=resolved)

    updater = _BranchUpdater(pr, batch_check=batch_check, on_branch_updated=on_branch_updated)
    not_ready = _NotReadyWait(pr)

    while True:
        if github.pr_state(pr) == "MERGED":
            msg = (
                f"PR {pr} is already merged — nothing to wait for. "
                "If cleanup is what remains, run vrg-finalize-pr without arguments."
            )
            raise MergeAbortError(msg)
        _refuse_unmergeable_shape(pr)
        if _is_behind(pr):
            updater.update()
            continue

        print(f"Waiting for checks on {pr}...")
        try:
            wait(pr)
        except (OrphanedCheckError, ChecksTimeoutError) as exc:
            raise _checks_wait_abort(pr, exc) from exc

        if _failed_checks_are_stale(pr, required):
            updater.update()
            continue

        # A waiter that watches only registered checks (the release workflow's
        # streaming one) can return while a required aggregator check has not
        # even registered yet — keep waiting rather than merge into a policy
        # rejection (#3061).
        outstanding = github.required_checks_outstanding(pr, required)
        if outstanding:
            not_ready.pause(f"awaiting required checks: {describe_outstanding(outstanding)}")
            continue

        if _is_behind(pr):
            continue  # something merged while we waited -> update at loop top

        # Re-read at every merge: a base edited while the loop waited is caught.
        _require_main_base(pr)
        try:
            if bypass_ruleset is not None:
                pinned = _bypass_head(pr, required)
                print(
                    f"Checks passed. Merging {pr} (--{strategy}) at {pinned} with the "
                    f"{bypass_ruleset!r} ruleset bypass..."
                )
                github.merge(pr, strategy=strategy, match_head=pinned, admin=True)
            elif batch_check is None:
                print(f"Checks passed. Merging {pr} (--{strategy})...")
                github.merge(pr, strategy=strategy)
            else:
                checked = batch_check()
                print(f"Checks passed. Merging {pr} (--{strategy}) at checked head {checked}...")
                github.merge(pr, strategy=strategy, match_head=checked)
        except GitHubAPIError as exc:
            # The merge endpoint enforces "require branches up to date" itself.
            # A stale-behind rejection here is the authoritative signal the lazy
            # mergeStateStatus precheck missed (issue #2856) — feed it back into
            # the same update-and-retry path.
            if _is_behind_rejection(exc):
                updater.update()
                continue
            # A batch merge pinned to the checked head was refused because the
            # head moved after the check: loop, so the new head is re-checked
            # before any merge, within the update cap.
            if batch_check is not None and _is_head_moved_rejection(exc):
                print(f"PR {pr}'s head moved after the batch check — re-checking...")
                updater.spend()
                continue
            if bypass_ruleset is not None:
                # The bypassed merge was pinned to the head its checks were
                # read at: a moved head is re-checked, within the update cap.
                if _is_head_moved_rejection(exc):
                    print(f"PR {pr}'s head moved after its checks were read — re-checking...")
                    updater.spend()
                    continue
                # A ruleset refusal of a bypassed merge cannot clear by waiting:
                # the caller is not a bypass actor (#5095).
                if _is_ruleset_rejection(exc):
                    raise _bypass_refused(pr, bypass_ruleset, exc) from exc
                raise
            # A branch-policy rejection with no failed checks means "not ready
            # yet" (e.g. a required check still registering), not fatal: poll and
            # re-attempt within the deadline (#3061). Any other failure is real.
            if not _is_policy_rejection(exc):
                raise
            not_ready.pause(_policy_block_reason(pr, required, resolved))
            continue
        print("Merged.")
        return


# --- Wait until merged (mechanized PRs, epic vergil-project/.github#1064) ----


class MergeDeadlineError(MergeAbortError):
    """The PR did not merge before the caller's overall deadline."""


def _merged_or_refuse_closed(pr: str) -> bool:
    """True when *pr* is merged; raise when it was closed without merging."""
    state = github.pr_state(pr)
    if state == "MERGED":
        print(f"PR {pr} is merged.")
        return True
    if state == "CLOSED":
        msg = f"PR {pr} was closed without merging."
        raise MergeAbortError(msg)
    return False


def wait_until_merged(pr: str, *, deadline_secs: int) -> None:
    """Watch *pr* until GitHub merges it; the watch itself never merges.

    The watch-only loop for a PR the one PR path armed
    (:func:`vergil_tooling.lib.pr_path.arm`, epic vergil-project/.github#1664,
    spec §5.4): nothing but the path turns auto-merge on or merges a PR into
    ``develop``, so this loop never calls ``gh pr merge``. Unlike
    :func:`wait_and_merge`, a merged PR is this loop's success, not an abort.
    Each iteration, in order,

    - **merged:** returns;
    - **closed** (without merging): raises;
    - **past the deadline:** raises :class:`MergeDeadlineError`;
    - **draft or conflicting:** raises (the shared shape checks);
    - **behind its base:** updates the branch (one of the shared update cap's
      :data:`_MAX_BRANCH_UPDATES`) and goes round again;
    - otherwise waits for the checks, then: a failed check on a behind branch
      updates it (#2914), any other failed check raises; a required check still
      outstanding waits a poll; a branch that fell behind meanwhile goes round
      again; and a ready PR waits one poll of :data:`_MERGE_POLL_SECS` for
      GitHub's auto-merge to merge it, then goes round again. A ready PR whose
      auto-merge is off is waited for the same way, never merged here: it
      ends merged (someone turned auto-merge on, or merged it), closed, or at
      the deadline.

    *deadline_secs* is the whole watch's wall-clock limit. Every wait inside
    it, the check wait and the polls included, is clamped to what is left, so
    the watch ends within it (give or take one GitHub API call). Every stop
    raises :class:`MergeAbortError` (a :class:`MergeDeadlineError` for the
    deadline) whose message names the PR and the reason; a GitHub API failure
    propagates as :class:`GitHubAPIError`.
    """
    start = time.monotonic()
    deadline = start + deadline_secs
    resolved, required = _resolve_required(pr)
    updater = _BranchUpdater(pr)
    not_ready = _NotReadyWait(pr, clamp=deadline)

    def _past_deadline(detail: str = "") -> MergeDeadlineError:
        waited = github.format_wait(time.monotonic() - start)
        msg = f"PR {pr} did not merge within the {deadline_secs}s deadline (waited {waited})"
        return MergeDeadlineError(f"{msg}: {detail}" if detail else f"{msg}.")

    while True:
        if _merged_or_refuse_closed(pr):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _past_deadline()
        _refuse_unmergeable_shape(pr)
        if _is_behind(pr):
            updater.update()
            continue

        print(f"Waiting for checks on {pr}...")
        poll_timeout = max(1, min(checks_timeout_secs(), math.ceil(remaining)))
        try:
            github.wait_for_checks(pr, poll_timeout=poll_timeout, required=resolved)
        except ChecksTimeoutError as exc:
            if time.monotonic() >= deadline:
                raise _past_deadline((exc.stderr or "").strip()) from exc
            raise _checks_wait_abort(pr, exc) from exc
        except OrphanedCheckError as exc:
            raise _checks_wait_abort(pr, exc) from exc

        if _merged_or_refuse_closed(pr):
            return  # auto-merge finished it during the wait
        if _failed_checks_are_stale(pr, required):
            updater.update()
            continue
        outstanding = github.required_checks_outstanding(pr, required)
        if outstanding:
            not_ready.pause(f"awaiting required checks: {describe_outstanding(outstanding)}")
            continue
        if _is_behind(pr):
            continue  # something merged while we waited -> update at loop top

        print(f"Checks passed on {pr}; waiting for GitHub's auto-merge to merge it...")
        time.sleep(max(0.0, min(_MERGE_POLL_SECS, deadline - time.monotonic())))
