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
   check. A failure in the snapshot's check rollup is confirmed with
   :func:`vergil_tooling.lib.github.failed_check_names`, which leaves out
   check-runs of superseded workflow runs. When the base branch's required
   checks cannot be read, every check counts as required.
7. **Conflict** (``mergeStateStatus`` ``DIRTY``): ``failed: conflict``.

A PR still pending at the deadline is ``timed out``; its auto-merge stays on.
Every failure to read a PR, to run the provenance check or to turn auto-merge
off raises; none is read as a pending or a clean PR.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib import auto_merge, github, pr_provenance
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

#: Every way a watched PR can end.
Outcome = Literal[
    "merged",
    "failed: CI",
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

_DEVELOP = "develop"
_RELEASE_PREFIX = "release/"
_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})
_VIEW_FIELDS = (
    "id,url,state,baseRefName,headRefName,headRefOid,autoMergeRequest,"
    "mergeStateStatus,statusCheckRollup,mergeCommit"
)
# A status context's failing states; a check-run is judged by
# github.check_run_bucket.
_FAILED_CONTEXT_STATES = frozenset({"FAILURE", "ERROR"})
_FAILED_BUCKETS = frozenset({"fail", "cancel"})

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
    none. ``vrg-pr-await`` reads a PR through :func:`snapshot` and judges its
    checks with :func:`failed_required_checks` too.
    """

    node_id: str
    url: str
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


def failed_required_checks(snap: Snapshot, required: Collection[str] | None) -> list[str]:
    """The required checks on *snap*'s head that concluded in failure, confirmed.

    The rollup decides whether to look; :func:`github.failed_check_names`
    confirms, leaving out check-runs of superseded runs. *required* ``None``
    counts every check as required.
    """
    candidates: set[str] = set()
    for node in snap.rollup:
        if node.get("__typename") == "StatusContext":
            name = str(node.get("context") or "")
            failed = str(node.get("state") or "").upper() in _FAILED_CONTEXT_STATES
        else:
            name = str(node.get("name") or "")
            bucket = github.check_run_bucket(
                str(node.get("status") or ""), str(node.get("conclusion") or "")
            )
            failed = bucket in _FAILED_BUCKETS
        if failed and (required is None or name in required):
            candidates.add(name)
    if not candidates:
        return []
    names = github.failed_check_names(snap.url)
    return [n for n in dict.fromkeys(names) if required is None or n in required]


@dataclass
class _Watched:
    """What the watch remembers about one PR between snapshots."""

    required: frozenset[str] | None
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


def _judge(snap: Snapshot, watched: _Watched, *, allow: bool) -> PRResult | None:
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
    failed = failed_required_checks(snap, watched.required)
    if failed:
        return _result(snap, "failed: CI", f"required check failed: {', '.join(failed)}", watched)
    if snap.merge_state == "DIRTY":
        return _result(snap, "failed: conflict", "the PR can no longer merge cleanly", watched)
    return None


def watch(
    prs: Sequence[str], *, allow_provenance_violation: bool, poll_interval: float = 15.0
) -> list[PRResult]:
    """Watch *prs* until each reaches a terminal :data:`Outcome` or the deadline passes.

    Returns one :class:`PRResult` per distinct PR, in the order given. The
    deadline (:func:`deadline_secs`) and *poll_interval* are checked before any
    PR is read. Each open PR is read once at start-up and once per interval.
    """
    deadline = deadline_secs()
    if poll_interval <= 0:
        msg = f"poll_interval must be positive, got {poll_interval!r}"
        raise ValueError(msg)
    results: dict[str, PRResult] = {}
    pending: dict[str, tuple[_Watched, Snapshot]] = {}
    order: list[str] = []
    for pr in prs:
        snap = snapshot(pr)
        if snap.url in order:
            continue
        order.append(snap.url)
        watchable = snap.state == "OPEN" and _not_develop(snap) is None
        watched = _Watched(required=github.required_check_names(snap.url) if watchable else None)
        result = _judge(snap, watched, allow=allow_provenance_violation)
        if result is None:
            pending[snap.url] = (watched, snap)
        else:
            results[snap.url] = result

    end = time.monotonic() + deadline
    while pending:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
        for url, (watched, _) in list(pending.items()):
            snap = snapshot(url)
            result = _judge(snap, watched, allow=allow_provenance_violation)
            if result is None:
                pending[url] = (watched, snap)
            else:
                results[url] = result
                del pending[url]

    for url, (watched, snap) in pending.items():
        detail = f"still pending at the {deadline}s batch deadline ({DEADLINE_ENV})"
        results[url] = _result(snap, "timed out", f"{detail}; its auto-merge stays on", watched)
    return [results[url] for url in order]
