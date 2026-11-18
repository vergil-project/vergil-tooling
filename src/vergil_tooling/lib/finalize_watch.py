"""The watch phase of ``vrg-finalize-pr`` (epic vergil-project/.github#1218, spec §6.3).

``vrg-submit-pr`` opens each PR with auto-merge on, and GitHub merges it once
its required checks pass. :func:`watch` waits on a batch of such PRs, polling
each until it reaches a terminal :data:`Outcome` or the batch deadline
(:func:`deadline_secs`) passes. It holds no lock and changes nothing locally;
the settle phase that follows is the caller's.

The watch never merges a PR and never turns auto-merge on. Auto-merge found
off, at the start or mid-watch, is an exception for the human, reported as
``failed: auto-merge off`` and never repaired. The watch turns auto-merge off
itself in one case only: a provenance violation on an open PR.

**Start-up and every poll** read one ``gh pr view`` snapshot per open PR and
judge it in this order:

1. **Develop PRs only.** A PR whose base is not ``develop``, or whose head is
   ``release/*``, ends as ``failed: not a develop PR``, untouched.
2. **Merged.** The PR ends as ``merged``, a PR that merged before the watch
   started included. Provenance (:func:`vergil_tooling.lib.pr_provenance.check_pr`)
   runs on it first: a violation cannot be undone, so nothing is disabled,
   and the result carries ``provenance_violation=True``, printed loudly. The
   caller treats such a run as not a full success, whatever
   ``allow_provenance_violation`` says: the override covers only an open PR.
3. **Closed** without merging: ``failed: closed``.
4. **Provenance** runs at start-up and again each time the PR's head
   changes. A violation turns auto-merge off (GraphQL
   ``disablePullRequestAutoMerge``, confirmed by reading it back) and ends the
   PR as ``failed: provenance``, unless ``allow_provenance_violation``: then
   the violation is printed loudly, flagged on the result, and the PR is
   watched on with its auto-merge left on. If it then merges, it is a merged
   PR with a violation, as in step 2.
5. **Auto-merge off:** ``failed: auto-merge off``.
6. **A required check concluded in failure:** ``failed: CI``, naming the
   check. :func:`check_failures` judges the snapshot's checks, and leaves
   out a failed or cancelled check-run of a superseded workflow run. When
   the base branch's required checks cannot be read, every check counts as
   required.
7. **A required check was cancelled** on a cancelled run that no newer run
   of its workflow has replaced, for :data:`CANCEL_GRACE_SECS` on end:
   ``failed: cancelled``, naming the check. Until then the PR is pending,
   since a re-run may still start.
8. **Conflict** (``mergeStateStatus`` ``DIRTY``): ``failed: conflict``.

**A superseded run is not a failure** (issue #4692). A push to an open PR
starts a new CI run, and ``concurrency: cancel-in-progress`` cancels the run
in flight on the same head. GitHub keeps the cancelled run's check-runs in
the PR's check rollup, and for a while they are the only ones there: the new
run registers its check-runs only once its jobs start. So a failed or
cancelled check-run is judged by its workflow run, read from the Actions
runs API (:func:`vergil_tooling.lib.github.head_workflow_runs`), which lists
the new run as soon as it is created.

**A merged PR is never reported as not merged.** A PR that ended
``failed: CI`` or ``failed: cancelled`` is read again on every later poll
while the watch goes on. If GitHub has merged it, it ends ``merged`` instead.

A PR still pending at the deadline is ``timed out``; its auto-merge stays on.
Every failure to read a PR, to run the provenance check or to turn auto-merge
off raises; none is read as a pending or a clean PR.

**A status table** (issue #4677) is built after start-up and after every
poll: one row per PR in the batch, a PR that has ended included, with its
number, its shortened title, its state (``checks pending (<n>)``,
``checks green, merging``, ``behind develop``, ``waiting (<merge state>)``,
or the outcome it ended with) and how long it has waited since the watch
started, frozen when it ended. A totals line closes it: merged, pending and
failed, and the time left before the batch deadline. It goes to
:func:`vergil_tooling.lib.progress.status_block` (issue #4694), marked
changed when some PR's state has changed since the last one: on a TTY it
is redrawn in place every poll; off a TTY it is printed only when it
changed, with a heartbeat line every few minutes between; and the run log
keeps every changed table with a timestamp.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib import auto_merge, github, pr_provenance, progress
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

#: Every way a watched PR can end.
Outcome = Literal[
    "merged",
    "failed: CI",
    "failed: cancelled",
    "failed: conflict",
    "failed: closed",
    "failed: auto-merge off",
    "failed: provenance",
    "failed: not a develop PR",
    "timed out",
]

#: The batch deadline's environment variable: a positive whole number of
#: seconds covering the whole watch phase.
DEADLINE_ENV = "VRG_FINALIZE_DEADLINE"
_DEFAULT_DEADLINE_SECS = 7200

#: How long a cancelled required check with no newer run holds a PR pending
#: before the PR ends ``failed: cancelled``: time for a re-run to start.
CANCEL_GRACE_SECS = 120.0

_DEVELOP = "develop"
_RELEASE_PREFIX = "release/"
_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})
_VIEW_FIELDS = (
    "id,url,title,state,baseRefName,headRefName,headRefOid,autoMergeRequest,"
    "mergeStateStatus,statusCheckRollup,mergeCommit"
)
# A status context's failing states; a check-run is judged by
# github.check_run_bucket.
_FAILED_CONTEXT_STATES = frozenset({"FAILURE", "ERROR"})
_FAILED_BUCKETS = frozenset({"fail", "cancel"})
_CANCEL_BUCKET = "cancel"
_RUN_COMPLETED = "completed"
_RUN_CANCELLED = "cancelled"
# The outcomes judged from checks alone: a later poll may still find the PR merged.
_CHECK_OUTCOMES = frozenset({"failed: CI", "failed: cancelled"})
_PENDING_CONTEXT_STATES = frozenset({"PENDING", "EXPECTED"})
_PENDING_BUCKET = "pending"
# The merge states in which GitHub merges an auto-merge PR with no check left.
_MERGING_STATES = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})
_TITLE_WIDTH = 50

_DISABLE_AUTO_MERGE_MUTATION = """
mutation($id: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $id}) {
    pullRequest { number }
  }
}
"""


class FinalizeWatchError(RuntimeError):
    """A PR could not be read or its auto-merge turned off; the message names the PR."""


@dataclass(frozen=True)
class PRResult:
    """How one watched PR ended.

    *pr* is the PR's URL and *head* its head commit when it ended. *detail*
    names the failed check, the provenance actions, or why the PR was not
    watched. *provenance_violation* is true whenever the watch found a
    provenance violation on the PR, whatever its outcome.
    """

    pr: str
    outcome: Outcome
    detail: str
    head: str
    provenance_violation: bool = False


def deadline_secs() -> int:
    """The batch deadline in seconds: ``$VRG_FINALIZE_DEADLINE`` or 7200.

    A malformed or non-positive value is a configuration error and raises
    :class:`ValueError`, like ``$VRG_CD_TIMEOUT``.
    """
    raw = os.environ.get(DEADLINE_ENV, "").strip()
    if not raw:
        return _DEFAULT_DEADLINE_SECS
    try:
        secs = int(raw)
    except ValueError:
        secs = 0
    if secs <= 0:
        msg = f"{DEADLINE_ENV} must be a positive whole number of seconds, got {raw!r}"
        raise ValueError(msg)
    return secs


@dataclass(frozen=True)
class Snapshot:
    """One ``gh pr view`` read of a PR.

    *merge_commit* is the merge commit's SHA, or ``""`` while GitHub reports
    none. ``vrg-pr-await`` and ``vrg-repo-status`` read a PR through
    :func:`snapshot` and judge its checks with :func:`check_failures` too.
    """

    node_id: str
    url: str
    title: str
    state: str
    base: str
    head_ref: str
    head: str
    auto_merge_on: bool
    merge_state: str
    rollup: tuple[dict[str, object], ...]
    merge_commit: str


def _field(data: dict[str, object], name: str, pr: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        msg = f"{pr}: gh pr view returned no {name}: {value!r}"
        raise FinalizeWatchError(msg)
    return value


def snapshot(pr: str) -> Snapshot:
    """Read *pr*'s state in one ``gh pr view`` call; a bad answer raises."""
    try:
        data = github.read_json("pr", "view", pr, "--json", _VIEW_FIELDS)
    except subprocess.CalledProcessError as exc:
        msg = f"{pr}: reading the PR failed: {error_text(exc)}"
        raise FinalizeWatchError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{pr}: gh pr view returned {data!r}"
        raise FinalizeWatchError(msg)
    state = _field(data, "state", pr)
    if state not in _STATES:
        msg = f"{pr}: gh pr view returned an unknown state: {state!r}"
        raise FinalizeWatchError(msg)
    request = data.get("autoMergeRequest")
    if request is not None and not isinstance(request, dict):
        msg = f"{pr}: autoMergeRequest is neither an object nor null: {request!r}"
        raise FinalizeWatchError(msg)
    merge_state = data.get("mergeStateStatus")
    if not isinstance(merge_state, str):
        msg = f"{pr}: mergeStateStatus is not a string: {merge_state!r}"
        raise FinalizeWatchError(msg)
    rollup = data.get("statusCheckRollup")
    if rollup is None:
        rollup = []
    if not isinstance(rollup, list) or not all(isinstance(n, dict) for n in rollup):
        msg = f"{pr}: statusCheckRollup is not a list of objects: {rollup!r}"
        raise FinalizeWatchError(msg)
    commit = data.get("mergeCommit")
    if commit is None:
        merge_commit = ""
    elif isinstance(commit, dict) and isinstance(commit.get("oid"), str) and commit["oid"]:
        merge_commit = str(commit["oid"])
    else:
        msg = f"{pr}: mergeCommit is neither null nor an object with an oid: {commit!r}"
        raise FinalizeWatchError(msg)
    return Snapshot(
        node_id=_field(data, "id", pr),
        url=_field(data, "url", pr),
        title=_field(data, "title", pr),
        state=state,
        base=_field(data, "baseRefName", pr),
        head_ref=_field(data, "headRefName", pr),
        head=_field(data, "headRefOid", pr),
        auto_merge_on=request is not None,
        merge_state=merge_state,
        rollup=tuple(rollup),
        merge_commit=merge_commit,
    )


def _not_develop(snap: Snapshot) -> str | None:
    """Why *snap* is not a develop PR, or ``None`` when it is one."""
    if snap.base != _DEVELOP:
        return f"its base is {snap.base}, not {_DEVELOP}"
    if snap.head_ref.startswith(_RELEASE_PREFIX):
        return f"its head is the release branch {snap.head_ref}"
    return None


def _violations(pr: str) -> str:
    """Run the provenance check on *pr*; its violations as text, ``""`` for none.

    Advisories and violations are printed to stderr. A check that cannot run
    raises, as :func:`vergil_tooling.lib.pr_provenance.check_pr` does.
    """
    result = pr_provenance.check_pr(pr)
    for adv in result.advisories:
        print(
            f"  ADVISORY: {pr}: {adv.login} ({adv.role.value}) performed "
            f"'{adv.action}' (permitted but advisory)",
            file=sys.stderr,
        )
    return ", ".join(f"{v.login} ({v.role.value}): {v.action}" for v in result.violations)


def _disable_auto_merge(snap: Snapshot) -> None:
    """Turn auto-merge off for *snap*'s PR and confirm it is off; any failure raises."""
    try:
        github.graphql(_DISABLE_AUTO_MERGE_MUTATION, id=snap.node_id)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        msg = f"{snap.url}: turning auto-merge off failed: {error_text(exc)}"
        raise FinalizeWatchError(msg) from exc
    try:
        still_on = auto_merge.auto_merge_enabled(snap.url)
    except auto_merge.AutoMergeError as exc:
        msg = f"{snap.url}: auto-merge was turned off, but reading it back failed: {exc}"
        raise FinalizeWatchError(msg) from exc
    if still_on:
        msg = f"{snap.url}: auto-merge is still on after turning it off"
        raise FinalizeWatchError(msg)


@dataclass(frozen=True)
class CheckFailures:
    """The required checks that refuse a snapshot's merge, by kind.

    *failed* concluded in failure; *cancelled* were cancelled on a completed,
    cancelled workflow run that no newer run of the workflow has replaced.
    """

    failed: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Occurrence:
    """The newest failed or cancelled occurrence of a check in a rollup.

    *run* is its Actions run as ``(owner/repo, run_id)``, or ``None`` for a
    status context or a check-run with no Actions job link.
    """

    run: tuple[str, int] | None
    cancelled: bool


def _rank(occurrence: _Occurrence) -> float:
    """Order occurrences by run id; one with no Actions run outranks every run."""
    return float("inf") if occurrence.run is None else float(occurrence.run[1])


def _failed_occurrences(snap: Snapshot, required: Collection[str] | None) -> dict[str, _Occurrence]:
    """Each required check with a failed or cancelled occurrence in *snap*'s rollup.

    A check that repeats across runs keeps its occurrence on the newest run.
    """
    found: dict[str, _Occurrence] = {}
    for node in snap.rollup:
        context = node.get("__typename") == "StatusContext"
        if context:
            name = str(node.get("context") or "")
            bucket = (
                "fail" if str(node.get("state") or "").upper() in _FAILED_CONTEXT_STATES else ""
            )
        else:
            name = str(node.get("name") or "")
            bucket = github.check_run_bucket(
                str(node.get("status") or ""), str(node.get("conclusion") or "")
            )
        if bucket not in _FAILED_BUCKETS or (required is not None and name not in required):
            continue
        run = None
        if not context:
            try:
                link = github.actions_run_of(str(node.get("detailsUrl") or ""))
            except ValueError as exc:
                msg = f"{snap.url}: {exc}"
                raise FinalizeWatchError(msg) from exc
            run = None if link is None else (link[0], int(link[1]))
        occurrence = _Occurrence(run=run, cancelled=bucket == _CANCEL_BUCKET)
        held = found.get(name)
        if held is None or _rank(occurrence) > _rank(held):
            found[name] = occurrence
    return found


def _superseded(run: github.WorkflowRun, runs: dict[int, github.WorkflowRun]) -> bool:
    """True when a newer run of *run*'s workflow, for the same event, is on the head."""
    return any(
        other.workflow_id == run.workflow_id
        and other.event == run.event
        and other.run_id > run.run_id
        for other in runs.values()
    )


def check_failures(snap: Snapshot, required: Collection[str] | None) -> CheckFailures:
    """Judge the required checks on *snap*'s head; see the module docstring.

    The rollup decides whether to look. :func:`github.failed_check_names`
    confirms, leaving out a check whose every check-run is on a superseded
    run. A confirmed check with no failed or cancelled check-run in the
    snapshot failed after the snapshot was read, so it is not yet decided:
    the next snapshot judges it. Each other confirmed check is judged by the
    workflow run of its newest failed or cancelled check-run, read from the
    head's Actions runs:

    - a newer run of the same workflow, for the same event, is on the head:
      superseded, so not a failure (issue #4692);
    - cancelled, on a run that has not completed: not yet decided, as the
      run is still being cancelled or re-run;
    - on a completed run that concluded ``cancelled``: *cancelled*;
    - anything else, a status context and a run the Actions API does not
      list included: *failed*.

    *required* ``None`` counts every check as required. A failure to read
    GitHub raises; none is read as a passing check.
    """
    occurrences = _failed_occurrences(snap, required)
    if not occurrences:
        return CheckFailures()
    names = github.failed_check_names(snap.url)
    confirmed = [n for n in dict.fromkeys(names) if required is None or n in required]
    runs_by_repo: dict[str, dict[int, github.WorkflowRun]] = {}
    failed: list[str] = []
    cancelled: list[str] = []
    for name in confirmed:
        occurrence = occurrences.get(name)
        if occurrence is None:
            continue  # it failed after the snapshot was read: judged on the next one
        if occurrence.run is None:
            failed.append(name)
            continue
        repo, run_id = occurrence.run
        if repo not in runs_by_repo:
            try:
                runs_by_repo[repo] = github.head_workflow_runs(repo, snap.head)
            except (subprocess.CalledProcessError, ValueError) as exc:
                msg = f"{snap.url}: reading the workflow runs on {snap.head} failed: "
                raise FinalizeWatchError(msg + error_text(exc)) from exc
        runs = runs_by_repo[repo]
        run = runs.get(run_id)
        if run is None:
            failed.append(name)
        elif _superseded(run, runs) or (occurrence.cancelled and run.status != _RUN_COMPLETED):
            continue
        elif run.status == _RUN_COMPLETED and run.conclusion == _RUN_CANCELLED:
            cancelled.append(name)
        else:
            failed.append(name)
    return CheckFailures(failed=tuple(failed), cancelled=tuple(cancelled))


@dataclass
class CancelGrace:
    """How long the same set of cancelled required checks has been seen.

    A cancelled check with no newer run holds the PR pending for *grace*
    seconds, so a re-run has time to start. A different set starts it again.
    """

    grace: float
    names: tuple[str, ...] = ()
    since: float = 0.0

    def expired(self, names: tuple[str, ...], now: float) -> bool:
        """Record *names* as cancelled at *now*; true once they have stayed so for *grace*."""
        if names != self.names:
            self.names, self.since = names, now
        return bool(names) and now - self.since >= self.grace


@dataclass
class _Watched:
    """What the watch remembers about one PR between snapshots."""

    required: frozenset[str] | None
    cancel: CancelGrace
    checked_head: str | None = None
    violation: str = ""


def _result(snap: Snapshot, outcome: Outcome, detail: str, watched: _Watched) -> PRResult:
    print(f"{snap.url}: {outcome}{f' ({detail})' if detail else ''}")
    return PRResult(
        pr=snap.url,
        outcome=outcome,
        detail=detail,
        head=snap.head,
        provenance_violation=bool(watched.violation),
    )


def _check_provenance(snap: Snapshot, watched: _Watched) -> bool:
    """Check *snap*'s PR at its head; record a violation on *watched*. True if one was found."""
    watched.checked_head = snap.head
    found = _violations(snap.url)
    if found:
        watched.violation = found
    return bool(found)


def _judge(snap: Snapshot, watched: _Watched, *, allow: bool, now: float) -> PRResult | None:
    """*snap*'s terminal result, in the module docstring's order, or ``None`` while pending."""
    why = _not_develop(snap)
    if why is not None:
        return _result(snap, "failed: not a develop PR", why, watched)
    if snap.state == "MERGED":
        if _check_provenance(snap, watched):
            print(
                f"PROVENANCE VIOLATION: {snap.url} merged with forbidden actions: "
                f"{watched.violation}. Nothing can be disabled; investigate before any release.",
                file=sys.stderr,
            )
        return _result(snap, "merged", watched.violation, watched)
    if snap.state == "CLOSED":
        return _result(snap, "failed: closed", "closed without merging", watched)
    if snap.head != watched.checked_head and _check_provenance(snap, watched):
        if not allow:
            print(
                f"PROVENANCE VIOLATION: {snap.url}: {watched.violation}. "
                "Turning its auto-merge off; investigate before re-enabling auto-merge.",
                file=sys.stderr,
            )
            if snap.auto_merge_on:
                _disable_auto_merge(snap)
            return _result(snap, "failed: provenance", watched.violation, watched)
        print(
            f"PROVENANCE VIOLATION: {snap.url}: {watched.violation}. "
            "Watching it on, per --allow-provenance-violation; if it merges, "
            "this run is still not a full success.",
            file=sys.stderr,
        )
    if not snap.auto_merge_on:
        detail = "auto-merge is off; investigate before re-enabling auto-merge"
        return _result(snap, "failed: auto-merge off", detail, watched)
    checks = check_failures(snap, watched.required)
    if checks.failed:
        detail = f"required check failed: {', '.join(checks.failed)}"
        return _result(snap, "failed: CI", detail, watched)
    if watched.cancel.expired(checks.cancelled, now):
        detail = (
            f"required check cancelled: {', '.join(checks.cancelled)}; no newer run "
            f"started within {github.format_wait(watched.cancel.grace)}"
        )
        return _result(snap, "failed: cancelled", detail, watched)
    if snap.merge_state == "DIRTY":
        return _result(snap, "failed: conflict", "the PR can no longer merge cleanly", watched)
    return None


def _pending_checks(snap: Snapshot, required: frozenset[str] | None) -> int:
    """How many of *snap*'s checks have not finished, unregistered required checks included."""
    seen: set[str] = set()
    count = 0
    for node in snap.rollup:
        if node.get("__typename") == "StatusContext":
            name = str(node.get("context") or "")
            pending = str(node.get("state") or "").upper() in _PENDING_CONTEXT_STATES
        else:
            name = str(node.get("name") or "")
            bucket = github.check_run_bucket(
                str(node.get("status") or ""), str(node.get("conclusion") or "")
            )
            pending = bucket == _PENDING_BUCKET
        seen.add(name)
        count += pending
    if required is not None:
        count += len(required - seen)
    return count


def _pending_state(snap: Snapshot, watched: _Watched) -> str:
    """The status table's state for *snap*'s PR while it is still pending."""
    if watched.cancel.names:
        return f"checks cancelled ({len(watched.cancel.names)}), awaiting a re-run"
    checks = _pending_checks(snap, watched.required)
    if checks:
        return f"checks pending ({checks})"
    if snap.merge_state in _MERGING_STATES:
        return "checks green, merging"
    if snap.merge_state == "BEHIND":
        return f"behind {_DEVELOP}"
    return f"waiting ({snap.merge_state.lower() or 'no merge state'})"


@dataclass(frozen=True)
class _Row:
    """One PR's row in the status table; *waited* is in seconds since the watch started."""

    number: str
    title: str
    state: str
    waited: float


def _row(snap: Snapshot, state: str, waited: float) -> _Row:
    number = f"#{snap.url.rsplit('/', 1)[-1]}"
    return _Row(number=number, title=snap.title, state=state, waited=waited)


def _duration(secs: float) -> str:
    """*secs* as ``1h02m03s`` or ``2m03s``, rounded down to the second."""
    hours, rest = divmod(max(int(secs), 0), 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"


def _shorten(title: str) -> str:
    """*title* cut to the table's title width, ending ``...`` when cut."""
    if len(title) <= _TITLE_WIDTH:
        return title
    return title[: _TITLE_WIDTH - 3].rstrip() + "..."


def _totals(results: Collection[PRResult], pending: int, left: float) -> str:
    """The table's totals: merged, pending and failed, and the time left."""
    merged = sum(r.outcome == "merged" for r in results)
    failed = sum(r.outcome.startswith("failed") for r in results)
    return (
        f"{merged} merged, {pending} pending, {failed} failed; "
        f"{_duration(left)} left before the batch deadline"
    )


def _status_lines(rows: Sequence[_Row], totals: str, elapsed: float) -> list[str]:
    """The status table: a header, one line per PR, then the totals line."""
    table = [("PR", "TITLE", "STATE", "WAITING")]
    table += [(r.number, _shorten(r.title), r.state, _duration(r.waited)) for r in rows]
    widths = [max(len(line[i]) for line in table) for i in range(3)]
    lines = [f"Watch status after {_duration(elapsed)}:"]
    for line in table:
        cells = [cell.ljust(width) for cell, width in zip(line[:3], widths, strict=True)]
        lines.append(f"  {'  '.join([*cells, line[3]])}")
    lines.append(f"  {totals}")
    return lines


class _StatusTable:
    """Sends the status table to the progress display, marking each change of state."""

    def __init__(self) -> None:
        self._last: tuple[tuple[str, str], ...] | None = None

    def show(
        self,
        rows: Sequence[_Row],
        results: Collection[PRResult],
        pending: int,
        elapsed: float,
        left: float,
    ) -> None:
        states = tuple((r.number, r.state) for r in rows)
        changed = states != self._last
        self._last = states
        totals = _totals(results, pending, left)
        progress.status_block(
            _status_lines(rows, totals, elapsed),
            changed=changed,
            heartbeat=f"Watch status after {_duration(elapsed)}: no change; {totals}",
        )


def summarize(results: Sequence[PRResult]) -> str:
    """One line for the watch's collapsed stage: ``4 merged, 1 failed[, 1 timed out]``."""
    merged = sum(r.outcome == "merged" for r in results)
    failed = sum(r.outcome.startswith("failed") for r in results)
    timed_out = sum(r.outcome == "timed out" for r in results)
    text = f"{merged} merged, {failed} failed"
    return f"{text}, {timed_out} timed out" if timed_out else text


def watch(
    prs: Sequence[str],
    *,
    allow_provenance_violation: bool,
    poll_interval: float = 15.0,
    cancel_grace: float = CANCEL_GRACE_SECS,
) -> list[PRResult]:
    """Watch *prs* until each reaches a terminal :data:`Outcome` or the deadline passes.

    Returns one :class:`PRResult` per distinct PR, in the order given. The
    deadline (:func:`deadline_secs`) and *poll_interval* are checked before any
    PR is read. Each open PR is read once at start-up and once per interval,
    and the status table is shown after start-up and after each poll. A PR
    that ended on its checks alone is read again on every later poll, and
    ends ``merged`` once GitHub reports it merged. *cancel_grace* is how long
    a cancelled required check with no newer run holds a PR pending.
    """
    deadline = deadline_secs()
    if poll_interval <= 0:
        msg = f"poll_interval must be positive, got {poll_interval!r}"
        raise ValueError(msg)
    results: dict[str, PRResult] = {}
    pending: dict[str, tuple[_Watched, Snapshot]] = {}
    # The PRs that ended on their checks alone, re-read while the watch goes on.
    recheck: dict[str, _Watched] = {}
    rows: dict[str, _Row] = {}
    order: list[str] = []
    table = _StatusTable()
    start = time.monotonic()

    def judge(snap: Snapshot, watched: _Watched) -> None:
        now = time.monotonic()
        result = _judge(snap, watched, allow=allow_provenance_violation, now=now)
        if result is None:
            pending[snap.url] = (watched, snap)
            rows[snap.url] = _row(snap, _pending_state(snap, watched), now - start)
            return
        results[snap.url] = result
        rows[snap.url] = _row(snap, result.outcome, now - start)
        pending.pop(snap.url, None)
        if result.outcome in _CHECK_OUTCOMES:
            recheck[snap.url] = watched

    for pr in prs:
        snap = snapshot(pr)
        if snap.url in order:
            continue
        order.append(snap.url)
        watchable = snap.state == "OPEN" and _not_develop(snap) is None
        required = github.required_check_names(snap.url) if watchable else None
        judge(snap, _Watched(required=required, cancel=CancelGrace(cancel_grace)))

    end = time.monotonic() + deadline
    rows_now = [rows[u] for u in order]
    table.show(rows_now, list(results.values()), len(pending), 0.0, float(deadline))
    while pending:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
        for url, watched in list(recheck.items()):
            snap = snapshot(url)
            if snap.state == "MERGED":
                del recheck[url]
                judge(snap, watched)
        for url, (watched, _) in list(pending.items()):
            judge(snapshot(url), watched)
        now = time.monotonic()
        rows_now = [rows[u] for u in order]
        table.show(rows_now, list(results.values()), len(pending), now - start, end - now)

    for url, (watched, snap) in pending.items():
        detail = f"still pending at the {deadline}s batch deadline ({DEADLINE_ENV})"
        results[url] = _result(snap, "timed out", f"{detail}; its auto-merge stays on", watched)
    return [results[url] for url in order]
