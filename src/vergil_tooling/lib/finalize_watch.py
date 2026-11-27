"""The watch phase of ``vrg-finalize-pr`` (epic vergil-project/.github#1218, spec §6.3).

``vrg-submit-pr`` opens each PR with auto-merge on, and GitHub merges it once
its required checks pass. :func:`watch` waits on a batch of such PRs, polling
each until it reaches a terminal :data:`Outcome` or the batch deadline
(:func:`deadline_secs`) passes. It holds no lock and changes nothing locally;
the settle phase that follows is the caller's.

The watch never merges a PR and never turns auto-merge on or off itself
(epic vergil-project/.github#1664, spec §5): it watches and reports. An
open PR's auto-merge is the release lock's business (spec §5.5, §5.7
layer 2): on every read of an open PR the watch asks its *lock* rule
(:data:`LockRule`; ``vrg-finalize-pr`` passes
:class:`vergil_tooling.lib.release_lock.watch.LockWatch`), before it would
judge auto-merge off, and the answer (a :class:`LockView`) decides:

- **No lock held:** an armed PR is judged as below. An unarmed one is
  re-armed through the one PR path (:func:`vergil_tooling.lib.pr_path.arm`,
  never by the watch itself); a re-arm the path refuses ends the PR as
  ``failed: refused``, naming the path's reason, with no further re-arm or
  poll.
- **Held by a live release:** the PR is reported as
  ``held by release <version> (stage <stage>, <health>)`` and the watch
  polls at its normal pace.
- **Held by a broken release, or one whose heartbeat is stale:** it is
  reported as such, the watch polls every :data:`BROKEN_WATCH_POLL_SECS`,
  and a stale lock is reclaimed.
- **Held but unreadable:** reported as ``held by an unreadable release
  lock (<error>)``, polled every :data:`BROKEN_WATCH_POLL_SECS`, never
  reclaimed: only ``vrg-release-lock break`` clears it.

So auto-merge off never ends a PR: there is no ``failed: auto-merge off``.
A PR still pending at the deadline while the lock holds it ends
``held by release``, which ``vrg-finalize-pr`` exits
:data:`EXIT_HELD_BY_RELEASE` for when every PR that did not merge ended so.
The watch judges no head of an open PR: the one PR path
(:mod:`vergil_tooling.lib.pr_path`) runs the provenance check and the
agent gates on every head the tooling arms or pushes, before it is armed
or pushed, so a head pushed after submit was judged before it reached the
PR.

**Start-up and every poll** read one ``gh pr view`` snapshot per open PR and
judge it in this order:

1. **Develop PRs only.** A PR whose base is not ``develop``, or whose head is
   ``release/*``, ends as ``failed: not a develop PR``, untouched. The head
   branch is taken from the trust layer, read once at start-up
   (:func:`confirmed_head`): a PR that is not a same-repo PR with a trusted
   author on its branch ends the same way.
2. **Merged.** The PR ends as ``merged``, a PR that merged before the watch
   started included. Provenance (:func:`vergil_tooling.lib.pr_provenance.check_pr`)
   runs on it: a violation cannot be undone, so nothing is disabled, and
   the result carries ``provenance_violation=True``, printed loudly. The
   caller treats such a run as not a full success.
3. **Closed** without merging: ``failed: closed``.
4. **The release lock**, as above: a refused re-arm is ``failed: refused``;
   every other answer leaves the PR to the steps below.
5. **A required check concluded in failure:** ``failed: CI``, naming the
   check. :func:`check_failures` judges the snapshot's checks, and leaves
   out a failed or cancelled check-run of a superseded workflow run. When
   the base branch's required checks cannot be read, every check counts as
   required.
6. **A required check was cancelled** on a cancelled run that no newer run
   of its workflow has replaced, for :data:`CANCEL_GRACE_SECS` on end:
   ``failed: cancelled``, naming the check. Until then the PR is pending,
   since a re-run may still start.
7. **Conflict** (``mergeStateStatus`` ``DIRTY``) reported at the same head
   for :data:`CONFLICT_SETTLE_SECS` on end: ``failed: conflict``. Until
   then the PR is pending: GitHub recomputes a PR's mergeable status after
   a push, and may report the old conflict meanwhile (issue #4998). A new
   head starts the count again, and any other merge state clears it.

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
while the watch goes on. If GitHub has merged it (its auto-merge is still
on, so a fix pushed to it merges by itself once its checks pass), it ends
``merged`` instead.

**The gates at a head**, for the one PR path (epic
vergil-project/.github#1384, plan Task 11; the path since epic
vergil-project/.github#1664). The watch no longer calls them; they live
here beside the snapshot they read. :func:`gates_at_head` judges a PR's
pushed head and :func:`gates_before_push` a head about to be pushed. Each:

- refuses, before reading anything, when the clone it runs in is not a
  clone of the PR's repository (:class:`FinalizeWatchError`: the gates are
  judged on local git objects, so another repo's clone cannot judge them);
- asks whether the PR is **agent-involved**: an agent login opened it or
  GitHub attributes an action on it to one
  (:func:`~vergil_tooling.lib.pr_provenance.pr_actors`,
  :func:`~vergil_tooling.lib.agent_gates.agent_involved`; arming the PR,
  and on a PR the one PR path accepts its merge, are not involvement:
  :func:`~vergil_tooling.lib.pr_provenance.not_involvement`), or its ready
  state, on the relay ref ``refs/vergil/pr-workflow/<branch>``
  (:func:`ready_state`), says so. A ready state counts as agent-involved
  unless its commit's author is trusted and its ``recorded_by`` is an
  identity that is not an agent: one with no ``recorded_by``, or a value
  the gates do not recognise, counts, so a PR an agent pushed to is never
  human-only for want of a pusher GitHub does not record. ``recorded_by`` is
  written by whoever ran ``report-ready`` and can be forged, so it only
  ever adds agent involvement: ``human`` never makes a PR that GitHub's
  actors show an agent acting on human-only. A PR with no ready state is
  judged from its GitHub actors alone;
- on an agent-involved PR, fetches ``develop`` (and, for a pushed head,
  the PR's head commit, which may have been made outside this clone),
  without moving any branch's remote-tracking ref, and runs
  :func:`~vergil_tooling.lib.agent_gates.evaluate` from the fetched
  ``origin/develop`` to the head, on the task issues of the PR's trusted
  body's linkage lines (or, when it has none, its trusted ready state's).

A head the gates do not pass is returned as the reasons, and any read or
fetch the gates cannot complete as the reason too, never as a pass.

A PR still pending at the deadline is ``timed out``, its auto-merge left as
it is, or ``held by release`` when the lock held it at its last read.
Every failure to read a PR, to run the provenance check, or to read the
lock, re-arm the PR or reclaim a stale lock raises; none is read as a
pending or a clean PR.

**A status table** (issue #4677) is built after start-up and after every
poll: one row per PR in the batch, a PR that has ended included, with its
number, its shortened title, its state (``held by release …`` while the
lock holds it, ``checks pending (<n>)``, ``checks green, merging``,
``behind develop``, ``waiting (<merge state>)``, or the outcome it ended
with) and how long it has waited since the watch started, frozen when it
ended. A totals line closes it: merged, pending and failed, and the time
left before the batch deadline. It goes to
:func:`vergil_tooling.lib.progress.status_block` (issue #4694), marked
changed when some PR's state has changed since the last one: on a TTY it
is redrawn in place every poll; off a TTY it is printed only when it
changed, with a heartbeat line every few minutes between; and the run log
keeps every changed table with a timestamp.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib import (
    agent_gates,
    git,
    github,
    linkage,
    pr_provenance,
    progress,
    trust,
)
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.github_transport import GitHubTransport
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock.model import BROKEN_WATCH_POLL_SECS
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from pathlib import Path

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

#: Every way a watched PR can end.
Outcome = Literal[
    "merged",
    "failed: CI",
    "failed: cancelled",
    "failed: conflict",
    "failed: closed",
    "failed: refused",
    "failed: not a develop PR",
    "timed out",
    "held by release",
]

#: The exit code of ``vrg-finalize-pr`` and ``vrg-pr-await`` when the PRs
#: that did not merge are all still held by a release at the deadline
#: (spec §5.5): the caller runs the same watch again.
EXIT_HELD_BY_RELEASE = 6

#: What the release lock means for one open PR, at one read (see the module docstring).
#: ``free``: no lock held, the PR armed or just re-armed. ``held``: a live
#: release holds it. ``broken``: a broken or heartbeat-stale release holds it.
#: ``unreadable``: a lock is held that cannot be read. ``refused``: no lock
#: held, and the one PR path refused to re-arm the PR.
LockState = Literal["free", "held", "broken", "unreadable", "refused"]
_HELD_STATES = frozenset({"held", "broken", "unreadable"})
_SLOW_STATES = frozenset({"broken", "unreadable"})


@dataclass(frozen=True)
class LockView:
    """What the release lock means for one open PR at one read.

    *detail* is what the watch reports: for a held state, the status it
    shows (``held by release <version> (stage <stage>, <health>)``); for
    ``refused``, the path's reason; for ``free``, what was done (a re-arm
    or a reclaim), ``""`` when nothing was.
    """

    state: LockState
    detail: str = ""

    @property
    def held(self) -> bool:
        """Whether the lock holds the PR: it cannot merge until the lock is released."""
        return self.state in _HELD_STATES

    @property
    def slow(self) -> bool:
        """Whether the watch polls every :data:`BROKEN_WATCH_POLL_SECS`, not at its normal pace."""
        return self.state in _SLOW_STATES


#: The view of a PR the lock does not touch.
FREE = LockView("free")


#: The release lock's answer for an open PR, asked on every read before
#: auto-merge is judged. A lock, re-arm or reclaim step that cannot be
#: completed raises :class:`FinalizeWatchError`, naming the PR.
type LockRule = Callable[[Snapshot], LockView]


#: The batch deadline's environment variable: a positive whole number of
#: seconds covering the whole watch phase.
DEADLINE_ENV = "VRG_FINALIZE_DEADLINE"
_DEFAULT_DEADLINE_SECS = 7200

#: How long a cancelled required check with no newer run holds a PR pending
#: before the PR ends ``failed: cancelled``: time for a re-run to start.
CANCEL_GRACE_SECS = 120.0

#: How long a PR must report a conflict at one head before it ends
#: ``failed: conflict``: time for GitHub to recompute its mergeable status
#: after a push (issue #4998).
CONFLICT_SETTLE_SECS = 120.0

_DEVELOP = "develop"
_RELEASE_PREFIX = "release/"
_STATES = frozenset({"OPEN", "MERGED", "CLOSED"})
_VIEW_FIELDS = (
    "id,url,title,state,baseRefName,headRefName,headRefOid,autoMergeRequest,"
    "mergeStateStatus,statusCheckRollup,mergeCommit,labels"
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


class FinalizeWatchError(RuntimeError):
    """A PR could not be read or its gates judged in a clone; the message names the PR."""


@dataclass(frozen=True)
class PRResult:
    """How one watched PR ended.

    *pr* is the PR's URL and *head* its head commit when it ended. *detail*
    names the failed check, the provenance actions of a merged PR, or why
    the PR was not watched. *title*, *branch* (the head branch) and *labels*
    are the PR's as last read, for the report's PR table.
    *provenance_violation* is true when the PR merged with a provenance
    violation.
    """

    pr: str
    outcome: Outcome
    detail: str
    head: str
    title: str
    branch: str
    labels: tuple[str, ...]
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
    none. *labels* are the PR's label names. *head_ref* is the head branch
    as ``gh`` reports it, a candidate only: the watch acts on the branch
    :func:`confirmed_head` gives. ``vrg-pr-await`` and
    ``vrg-repo-status`` read a PR through :func:`snapshot` and judge its
    checks with :func:`check_failures` too.
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
    labels: tuple[str, ...]


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
    labels = data.get("labels")
    if not isinstance(labels, list) or not all(
        isinstance(n, dict) and isinstance(n.get("name"), str) for n in labels
    ):
        msg = f"{pr}: labels is not a list of objects with a name: {labels!r}"
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
        labels=tuple(str(n["name"]) for n in labels),
    )


_PR_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)$")


def confirmed_head(snap: Snapshot) -> str | None:
    """*snap*'s head branch as the trust layer confirms it, or ``None``.

    The snapshot's head branch is only a candidate: it is looked up through
    :func:`trust.prs_for_branch`, and only the PR with *snap*'s own number
    counts, so a fork PR on a branch of the same name never stands in for
    it. The skipped PRs are reported on stderr, metadata only. A URL that
    names no repo and number, or a read the layer cannot complete, raises
    :class:`FinalizeWatchError`.
    """
    repo, number = pr_repo_and_number(snap)
    try:
        prs, skipped = trust.prs_for_branch(repo, snap.head_ref, state="all")
    except trust.TrustReadError as exc:
        msg = f"{snap.url}: confirming the PR through the trust layer failed: {exc}"
        raise FinalizeWatchError(msg) from exc
    trust.report_skipped(skipped, stream=sys.stderr)
    return next((pr.head for pr in prs if pr.number == number), None)


def _not_develop(snap: Snapshot, head: str | None) -> str | None:
    """Why *snap* is not a develop PR, or ``None`` when it is one.

    *head* is the head branch :func:`confirmed_head` gave, ``None`` when the
    trust layer did not confirm the PR.
    """
    if snap.base != _DEVELOP:
        return f"its base is {snap.base}, not {_DEVELOP}"
    if head is None:
        return "it is not a same-repo PR with a trusted author on its branch"
    if head.startswith(_RELEASE_PREFIX):
        return f"its head is the release branch {head}"
    return None


def _violations(pr: str) -> str:
    """Run the provenance check on *pr*; its violations as text, ``""`` for none.

    A check that cannot run raises, as
    :func:`vergil_tooling.lib.pr_provenance.check_pr` does.
    """
    result = pr_provenance.check_pr(pr)
    return ", ".join(f"{v.login} ({v.role.value}): {v.action}" for v in result.violations)


# --- the gates at a head, for the one PR path ----------------------------------------

#: The ready state's file on its relay ref's single-file commit.
_RELAY_FILE = "pr-workflow.json"

#: Every error a read, fetch or parse of the gates raises. Each one is
#: returned as the reason the head does not pass; none is read as a pass.
_RECHECK_ERRORS = (
    subprocess.CalledProcessError,
    trust.TrustReadError,
    trust.UntrustedContentError,
    TrustedAuthorsNotConfiguredError,
    WorkflowError,
    ValueError,
    TypeError,
    KeyError,
)


@dataclass(frozen=True)
class ReadyState:
    """A PR's ready state, as its relay ref carries it.

    *trusted* is whether the relay commit's author is trusted
    (:func:`trust.require_trusted_commit`). An untrusted one's payload is
    never parsed: *issues* is then empty and *recorded_by* ``None``.
    *recorded_by* is the payload's raw ``recorded_by`` value, ``None`` when
    it has none.
    """

    trusted: bool
    issues: tuple[str, ...] = ()
    recorded_by: object = None


def _git(clone: Path, *args: str) -> str:
    return git.read_output("-C", str(clone), *args)


def pr_repo_and_number(snap: Snapshot) -> tuple[str, int]:
    match = _PR_URL_RE.search(snap.url)
    if match is None:
        msg = f"{snap.url}: cannot read the repository and number from the PR's URL"
        raise FinalizeWatchError(msg)
    return match.group(1), int(match.group(2))


def require_clone_of(clone: Path, repo: str, pr: str) -> None:
    """Refuse unless *clone*'s ``origin`` is *repo*: the gates judge local git objects.

    The gates (:func:`gates_at_head`, :func:`gates_before_push`) and the one
    PR path (:mod:`vergil_tooling.lib.pr_path`) check a clone they are given
    this way.
    """
    try:
        url = git.remote_url("origin", repo=clone)
    except subprocess.CalledProcessError as exc:
        msg = f"{pr}: cannot read the origin of {clone}: {error_text(exc)}"
        raise FinalizeWatchError(msg) from exc
    origin = github.github_remote_repo(url)
    if origin is None or origin.lower() != repo.lower():
        shown = origin or "not a GitHub repository"
        msg = (
            f"{pr}: the gates cannot be re-checked from {clone}, whose origin is {shown},"
            f" not the PR's repository {repo}; run it from a clone of {repo}"
        )
        raise FinalizeWatchError(msg)


def ready_state(clone: Path, repo: str, branch: str) -> ReadyState | None:
    """The ready state on *branch*'s relay ref in *repo*, or ``None`` when it has none.

    Read from *clone*'s ``origin``: the ref's commit is fetched by its SHA,
    with nothing written to ``FETCH_HEAD`` or any ref, so the payload parsed
    is the one whose author was judged. A read, fetch or parse failure
    raises; an untrusted author gives an untrusted :class:`ReadyState`.
    """
    ref = GitHubTransport(branch).ref
    listed = _git(clone, "ls-remote", "origin", ref)
    shas = [
        sha
        for sha, _tab, name in (line.partition("\t") for line in listed.splitlines())
        if name == ref
    ]
    if not shas:
        return None
    [sha] = shas
    _git(clone, "fetch", "--no-write-fetch-head", "origin", sha)
    try:
        trust.require_trusted_commit(repo, sha, field=f"the relay ref {ref}")
    except trust.UntrustedContentError:
        return ReadyState(trusted=False)
    data = json.loads(_git(clone, "show", f"{sha}:{_RELAY_FILE}"))
    if not isinstance(data, dict):
        msg = f"the relay ref {ref} carries no JSON object"
        raise TypeError(msg)
    state = WorkflowState.from_dict(data)
    return ReadyState(
        trusted=True, issues=tuple(state.all_issues), recorded_by=data.get("recorded_by")
    )


def _recorded_by_involves(value: object, authors: TrustedAuthors) -> bool:
    """Whether a trusted ready state's ``recorded_by`` *value* makes the PR agent-involved.

    A missing, non-string or unknown value does; a known identity mode does
    when :func:`agent_gates.agent_involved` says so.
    """
    if not isinstance(value, str):
        return True
    try:
        mode = IdentityMode(value)
    except ValueError:
        return True
    return agent_gates.agent_involved(authors=authors, recorded_by=mode)


def agent_involvement(pr: str, ready: ReadyState | None, authors: TrustedAuthors) -> bool:
    """Whether *pr* is agent-involved, from its GitHub actors and its ready state.

    Each piece of evidence can only add involvement: the ready state is
    consulted only when the actors show no agent, so a ``recorded_by`` of
    ``human`` never outweighs them.
    """
    if agent_gates.agent_involved(authors=authors, actors=pr_provenance.pr_actors(pr)):
        return True
    if ready is None:
        return False
    return not ready.trusted or _recorded_by_involves(ready.recorded_by, authors)


def task_issue_refs(repo: str, number: int, ready: ReadyState | None) -> list[str]:
    """The task issues of PR *repo*#*number*, as ``owner/repo#N``.

    The linkage lines (``Ref`` or ``Closes``) of the PR's body, which must be
    trusted (an untrusted body raises :class:`trust.UntrustedContentError`),
    or, when it has none, a trusted ready state's issues. ``[]`` when neither
    names one; :func:`agent_gates.evaluate` then refuses.
    """
    body = trust.read_issue(repo, number, require_body=True).body or ""
    refs = [ref for _keyword, ref in linkage.extract_tracking_refs(body)]
    if not refs and ready is not None and ready.trusted:
        refs = list(ready.issues)
    return [f"{repo}{ref}" if ref.startswith("#") else ref for ref in refs]


def _fetch_for_gates(clone: Path, head: str) -> str:
    """Fetch ``develop`` and the commit *head* into *clone*; the fetched develop's SHA.

    Only ``origin/develop`` moves. The head is fetched by its SHA into no
    ref, so no feature branch's remote-tracking ref (a later
    ``--force-with-lease``'s expectation) changes. Raises when the head is
    still missing after the fetch.
    """
    tracking = f"refs/remotes/origin/{_DEVELOP}"
    _git(
        clone,
        "fetch",
        "--no-write-fetch-head",
        "origin",
        f"+refs/heads/{_DEVELOP}:{tracking}",
        head,
    )
    _git(clone, "cat-file", "-e", f"{head}^{{commit}}")
    return _git(clone, "rev-parse", "--verify", f"{tracking}^{{commit}}")


def _fetch_develop_for_gates(clone: Path, head: str) -> str:
    """Fetch ``develop`` into *clone*, which must already hold *head*; the fetched develop's SHA.

    For a head not yet pushed (:func:`gates_before_push`): only
    ``origin/develop`` moves, and a head missing from *clone* raises.
    """
    tracking = f"refs/remotes/origin/{_DEVELOP}"
    _git(clone, "fetch", "--no-write-fetch-head", "origin", f"+refs/heads/{_DEVELOP}:{tracking}")
    _git(clone, "cat-file", "-e", f"{head}^{{commit}}")
    return _git(clone, "rev-parse", "--verify", f"{tracking}^{{commit}}")


def _gates_at(
    snap: Snapshot, branch: str, clone: Path, *, head: str, pushed: bool, pusher_is_agent: bool
) -> str:
    """Why *head* of *snap*'s PR does not pass the gates, ``""`` when it does.

    *pushed* says whether *head* is on origin (fetched by its SHA) or only in
    *clone*. *pusher_is_agent* makes the PR agent-involved whatever its
    GitHub actors and ready state say.
    """
    repo, number = pr_repo_and_number(snap)
    require_clone_of(clone, repo, snap.url)
    try:
        authors = load_trusted_authors()
        ready = ready_state(clone, repo, branch)
        if not pusher_is_agent and not agent_involvement(snap.url, ready, authors):
            return ""
        refs = task_issue_refs(repo, number, ready)
        fetch = _fetch_for_gates if pushed else _fetch_develop_for_gates
        base = fetch(clone, head)
        result = agent_gates.evaluate(clone, base=base, head=head, issue_refs=refs, authors=authors)
    except _RECHECK_ERRORS as exc:
        return f"the gates could not be re-checked at {head[:12]}: {error_text(exc)}"
    return "" if result.ok else "; ".join(result.reasons)


def gates_at_head(snap: Snapshot, branch: str, clone: Path) -> str:
    """Why *snap*'s pushed head does not pass the gates, or ``""`` when it does.

    The one PR path's arm (:func:`vergil_tooling.lib.pr_path.arm`) judges
    the head it arms this way. See "The gates at a head" in the module
    docstring. A clone of another repository raises
    :class:`FinalizeWatchError` before anything is read. A PR that is not
    agent-involved passes. Any read, fetch or parse failure is returned as
    the reason, never a pass.
    """
    return _gates_at(snap, branch, clone, head=snap.head, pushed=True, pusher_is_agent=False)


def gates_before_push(
    snap: Snapshot, branch: str, clone: Path, *, head: str, pusher_is_agent: bool
) -> str:
    """Why *head*, a commit in *clone* about to be pushed to *snap*'s PR, does not pass the gates.

    The one PR path's push mode
    (:func:`vergil_tooling.lib.pr_path.push_to_open_pr`) judges a new head
    before it pushes it, so *head* is read from *clone*, never fetched. The
    rules are :func:`gates_at_head`'s, with one addition: when
    *pusher_is_agent*, the push itself makes the PR agent-involved, so a PR
    no agent has acted on yet is gated too. ``""`` when it passes.
    """
    return _gates_at(snap, branch, clone, head=head, pushed=False, pusher_is_agent=pusher_is_agent)


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
class ConflictSettle:
    """How long a PR has reported a conflict at one head.

    GitHub recomputes a PR's mergeable status after a push, and until it has
    it may still report the old one: a conflict a rebase has just resolved
    can read ``DIRTY`` for a minute or so (issue #4998). So a conflict ends
    the PR only once it has been reported at the same head for *settle*
    seconds. A new head starts the count again, and any other merge state
    clears it.
    """

    settle: float
    head: str | None = None
    since: float = 0.0

    def settled(self, *, conflicted: bool, head: str, now: float) -> bool:
        """Record whether *head* reads as conflicted at *now*; true once it has for *settle*."""
        if not conflicted:
            self.head = None
            return False
        if head != self.head:
            self.head, self.since = head, now
        return now - self.since >= self.settle

    @property
    def settling(self) -> bool:
        """Whether a conflict has been reported and has not yet settled or cleared."""
        return self.head is not None


@dataclass
class _Watched:
    """What the watch remembers about one PR between snapshots.

    *branch* is the head branch :func:`confirmed_head` gave at start-up, or
    ``None`` when the trust layer did not confirm the PR. *violation* is the
    provenance violation found on the merged PR. *lock* is the release
    lock's view of the PR at its last read.
    """

    required: frozenset[str] | None
    cancel: CancelGrace
    conflict: ConflictSettle
    branch: str | None
    violation: str = ""
    lock: LockView = FREE


def _result(snap: Snapshot, outcome: Outcome, detail: str, watched: _Watched) -> PRResult:
    print(f"{snap.url}: {outcome}{f' ({detail})' if detail else ''}")
    return PRResult(
        pr=snap.url,
        outcome=outcome,
        detail=detail,
        head=snap.head,
        title=snap.title,
        branch=watched.branch or "",
        labels=snap.labels,
        provenance_violation=bool(watched.violation),
    )


def _read_lock(snap: Snapshot, watched: _Watched, lock: LockRule) -> LockView:
    """Ask *lock* about open PR *snap*, record the view, and print it when it changed."""
    view = lock(snap)
    if view != watched.lock and view.detail:
        print(f"{snap.url}: {view.detail}")
    watched.lock = view
    return view


def _judge(snap: Snapshot, watched: _Watched, *, now: float, lock: LockRule) -> PRResult | None:
    """*snap*'s terminal result, in the module docstring's order, or ``None`` while pending."""
    why = _not_develop(snap, watched.branch)
    if why is not None:
        return _result(snap, "failed: not a develop PR", why, watched)
    if snap.state == "MERGED":
        watched.violation = _violations(snap.url)
        if watched.violation:
            print(
                f"PROVENANCE VIOLATION: {snap.url} merged with forbidden actions: "
                f"{watched.violation}. Nothing can be disabled; investigate before any release.",
                file=sys.stderr,
            )
        return _result(snap, "merged", watched.violation, watched)
    if snap.state == "CLOSED":
        return _result(snap, "failed: closed", "closed without merging", watched)
    view = _read_lock(snap, watched, lock)
    if view.state == "refused":
        detail = f"the one PR path refused to re-arm it: {view.detail}"
        return _result(snap, "failed: refused", detail, watched)
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
    conflicted = snap.merge_state == "DIRTY"
    if watched.conflict.settled(conflicted=conflicted, head=snap.head, now=now):
        detail = (
            "the PR can no longer merge cleanly (reported at its head for "
            f"{github.format_wait(watched.conflict.settle)})"
        )
        return _result(snap, "failed: conflict", detail, watched)
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
    if watched.lock.held:
        return watched.lock.detail
    if watched.cancel.names:
        return f"checks cancelled ({len(watched.cancel.names)}), awaiting a re-run"
    if watched.conflict.settling:
        return "conflict reported, waiting for GitHub to settle it"
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
    branch: str
    title: str
    state: str
    waited: float


def _row(snap: Snapshot, watched: _Watched, state: str, waited: float) -> _Row:
    """*snap*'s row, its branch the one *watched* confirmed, ``-`` when none was.

    ``gh``'s head ref is a candidate only, so a PR the trust layer did not
    confirm is never named by its branch.
    """
    number = f"#{snap.url.rsplit('/', 1)[-1]}"
    branch = watched.branch or "-"
    return _Row(number=number, branch=branch, title=snap.title, state=state, waited=waited)


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
    """The table's totals: merged, pending and failed, and the time left.

    A PR the lock holds is pending until the deadline, after which no table
    is shown, so no PR is counted here as ended ``held by release``.
    """
    merged = sum(r.outcome == "merged" for r in results)
    failed = sum(r.outcome.startswith("failed") for r in results)
    return (
        f"{merged} merged, {pending} pending, {failed} failed; "
        f"{_duration(left)} left before the batch deadline"
    )


def _status_lines(rows: Sequence[_Row], totals: str, elapsed: float) -> list[str]:
    """The status table: a header, one line per PR, then the totals line.

    The branch sits between the PR and its title (vergil-tooling#5223):
    branch names are easier to scan than titles.
    """
    table = [("PR", "BRANCH", "TITLE", "STATE", "WAITING")]
    table += [(r.number, r.branch, _shorten(r.title), r.state, _duration(r.waited)) for r in rows]
    widths = [max(len(line[i]) for line in table) for i in range(4)]
    lines = [f"Watch status after {_duration(elapsed)}:"]
    for line in table:
        cells = [cell.ljust(width) for cell, width in zip(line[:4], widths, strict=True)]
        lines.append(f"  {'  '.join([*cells, line[4]])}")
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
    """One line for the watch's stage, such as ``4 merged, 1 failed, 1 held by release``.

    The failed count is always there; the timed-out and held counts only when not zero.
    """
    merged = sum(r.outcome == "merged" for r in results)
    failed = sum(r.outcome.startswith("failed") for r in results)
    timed_out = sum(r.outcome == "timed out" for r in results)
    held = sum(r.outcome == "held by release" for r in results)
    text = f"{merged} merged, {failed} failed"
    if timed_out:
        text += f", {timed_out} timed out"
    if held:
        text += f", {held} held by release"
    return text


def watch(
    prs: Sequence[str],
    *,
    lock: LockRule,
    poll_interval: float = 15.0,
    broken_poll: float = BROKEN_WATCH_POLL_SECS,
    cancel_grace: float = CANCEL_GRACE_SECS,
    conflict_settle: float = CONFLICT_SETTLE_SECS,
) -> list[PRResult]:
    """Watch *prs* until each reaches a terminal :data:`Outcome` or the deadline passes.

    Returns one :class:`PRResult` per distinct PR, in the order given. The
    deadline (:func:`deadline_secs`), *poll_interval* and *broken_poll* are
    checked before any PR is read. Each open PR is read once at start-up and
    once per interval, and *lock* is asked about it on each read; the
    interval is *broken_poll* while the lock's view of any pending PR is
    slow (:attr:`LockView.slow`), *poll_interval* otherwise. The status
    table is shown after start-up and after each poll. A PR
    that ended on its checks alone is read again on every later poll, and
    ends ``merged`` once GitHub reports it merged. *cancel_grace* is how long
    a cancelled required check with no newer run holds a PR pending, and
    *conflict_settle* how long a conflict must be reported at one head
    before the PR ends failed: conflict. No head of an open PR is judged,
    and the watch turns auto-merge on only through *lock*'s re-arm: see the
    module docstring.
    """
    deadline = deadline_secs()
    for name, value in (("poll_interval", poll_interval), ("broken_poll", broken_poll)):
        if value <= 0:
            msg = f"{name} must be positive, got {value!r}"
            raise ValueError(msg)
    results: dict[str, PRResult] = {}
    pending: dict[str, tuple[_Watched, Snapshot]] = {}
    # The PRs that ended on their checks alone, re-read while the watch goes on.
    reread: dict[str, _Watched] = {}
    rows: dict[str, _Row] = {}
    order: list[str] = []
    table = _StatusTable()
    start = time.monotonic()

    def judge(snap: Snapshot, watched: _Watched) -> None:
        now = time.monotonic()
        result = _judge(snap, watched, now=now, lock=lock)
        if result is None:
            pending[snap.url] = (watched, snap)
            rows[snap.url] = _row(snap, watched, _pending_state(snap, watched), now - start)
            return
        results[snap.url] = result
        rows[snap.url] = _row(snap, watched, result.outcome, now - start)
        pending.pop(snap.url, None)
        if result.outcome in _CHECK_OUTCOMES:
            reread[snap.url] = watched

    for pr in prs:
        snap = snapshot(pr)
        if snap.url in order:
            continue
        order.append(snap.url)
        branch = confirmed_head(snap)
        watchable = snap.state == "OPEN" and _not_develop(snap, branch) is None
        required = github.required_check_names(snap.url) if watchable else None
        watched = _Watched(
            required=required,
            cancel=CancelGrace(cancel_grace),
            conflict=ConflictSettle(conflict_settle),
            branch=branch,
        )
        judge(snap, watched)

    end = time.monotonic() + deadline
    rows_now = [rows[u] for u in order]
    table.show(rows_now, list(results.values()), len(pending), 0.0, float(deadline))
    while pending:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        slow = any(watched.lock.slow for watched, _ in pending.values())
        time.sleep(min(broken_poll if slow else poll_interval, remaining))
        for url, watched in list(reread.items()):
            snap = snapshot(url)
            if snap.state == "MERGED":
                del reread[url]
                judge(snap, watched)
        for url, (watched, _) in list(pending.items()):
            judge(snapshot(url), watched)
        now = time.monotonic()
        rows_now = [rows[u] for u in order]
        table.show(rows_now, list(results.values()), len(pending), now - start, end - now)

    for url, (watched, snap) in pending.items():
        at = f"at the {deadline}s batch deadline ({DEADLINE_ENV})"
        if watched.lock.held:
            detail = f"still {watched.lock.detail} {at}; run the watch again"
            results[url] = _result(snap, "held by release", detail, watched)
        else:
            detail = f"still pending {at}; its auto-merge stays on"
            results[url] = _result(snap, "timed out", detail, watched)
    return [results[url] for url in order]
