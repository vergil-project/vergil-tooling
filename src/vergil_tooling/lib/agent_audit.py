"""The daily audit of agent merges: ``vrg-agent-audit`` (epic vergil-project/.github#1384).

Submit and the finalize watch enforce the gates of
:mod:`~vergil_tooling.lib.agent_gates` before a PR merges. This audit
re-checks, after the fact, every merge into ``develop`` and ``main`` since the
last one it checked (spec §4.8), so a merge that bypassed the tools is visible
within a day. It never prevents a merge.

**Markers.** Each audited branch has a marker branch in the audited repo,
``vergil/agent-audit/develop`` and ``vergil/agent-audit/main``
(:data:`MARKER_BRANCHES`), holding the last commit on that branch the audit
has checked. A ruleset ("Agent audit markers") lets only the admin role and
the org automation App create, move or delete them, so the identity being
audited cannot move them. :func:`create_marker` is the one way a marker is
created: :func:`init_markers` (``--init``) and repo init use it. A scheduled
run that finds a marker missing reports a ``missing-marker`` finding and never
re-creates it, since a marker re-created at the current tip would silently
skip every merge since the last run.

**An absent branch.** A repo that is never released has no ``main``. An
audited branch the repo has neither of nor its marker is skipped, by
``--init`` and by the audit, and each says so with the :func:`not_audited`
line, so "not audited because absent" never reads as "audited clean". An
absent branch whose marker is set is refused by ``--init`` and is a
``missing-branch`` finding in the audit, since deleting a branch must never
be a way out of its audit. ``develop`` is required
(:data:`REQUIRED_BRANCHES`): a repo with no ``develop`` is always refused and
a finding.

**What one run checks.** For each branch, every commit on its first-parent
history after the marker belongs to a *landing*: the merge of one PR (a merge
commit, a squash commit, or the run of commits a rebase merge adds), found
through the commit's associated PRs on GitHub. A commit that is no merged
PR's is a ``no-pr`` finding. For each landing:

1. **The actor check**, on every PR: whoever merged it or turned auto-merge on
   must be in the expected set, judged by
   :func:`~vergil_tooling.lib.pr_provenance.classify` with account types. On
   ``develop`` that is the human, or the user agent or the automation App on
   a PR it authored itself or on any PR the one PR path accepts, a same-repo
   PR whose branch has a trusted author (epic vergil-project/.github#1664,
   spec §5.1 and §5.9). On ``main`` it is the human alone (and the
   automation App if the release flow merged as it, which it does not:
   :data:`~vergil_tooling.lib.github_config.APP_MERGES_RELEASES`). An
   ``UNTRUSTED`` actor, a deleted account and a merge with no recorded actor
   are always reported. The PR's full provenance
   (:func:`~vergil_tooling.lib.pr_provenance.check_pr`) is checked too.
2. **The gates**, on an agent-involved PR merged into ``develop``
   (:func:`~vergil_tooling.lib.agent_gates.agent_involved`, from the PR's
   author and every actor GitHub records on its timeline, bar the events
   :func:`~vergil_tooling.lib.pr_provenance.not_involvement` leaves out:
   arming, and on a PR the path accepts its merge and the merge's closing,
   the rule the finalize watch uses), and, on a same-repo PR, every
   account that pushed to, created or force-pushed its head branch in the
   repository's activity (:func:`_branch_pushers`): the merge-review
   coverage of :func:`~vergil_tooling.lib.agent_gates.evaluate`, on the
   change from the branch tip before the landing to the PR's head,
   ``refs/pull/<n>/head`` (merged branches are deleted). The task issues are
   the linkage lines of the PR's body, read through the trust layer; an
   untrusted body is a finding.

A PR done entirely by the human or the automation App is not agent-involved,
so only the actor check applies to it.

**Fail closed.** Any read failure is reported (a ``read-failure`` finding)
and stops the branch's audit there. The marker then advances only to the
last landing fully checked, so the next run checks the rest again. A landing
with a finding has been checked, so the marker moves past it: the finding is
reported in that run, whose log ``hoshin`` reads.

The audit is a security-signal job: it files no issue and posts nothing. Its
findings are its exit status and its output.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from vergil_tooling.lib import agent_gates, git, github, pr_provenance, trust
from vergil_tooling.lib.github_config import APP_MERGES_RELEASES
from vergil_tooling.lib.linkage import extract_tracking_refs

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

__all__ = [
    "AUDITED_BRANCHES",
    "MARKER_BRANCHES",
    "AuditError",
    "BranchAudit",
    "Finding",
    "Landing",
    "MarkerInit",
    "audit",
    "audit_branch",
    "create_marker",
    "init_markers",
    "marker_branch",
    "not_audited",
    "read_marker",
]

#: The branches the audit covers, each from its own marker.
AUDITED_BRANCHES = ("develop", "main")
#: The audited branches every repo has. Any other may be absent (a repo that
#: is never released has no ``main``): it is then skipped, and said so.
REQUIRED_BRANCHES = ("develop",)
MARKER_PREFIX = "vergil/agent-audit/"


def marker_branch(branch: str) -> str:
    """The marker branch that records how far *branch* has been audited."""
    return f"{MARKER_PREFIX}{branch}"


def not_audited(repo: str, branch: str) -> str:
    """The line that reports *branch* skipped because *repo* has no such branch and no marker."""
    return f"{repo}: no {branch} branch; not audited"


#: The marker branches, one per audited branch: the one definition repo init reuses.
MARKER_BRANCHES = tuple(marker_branch(branch) for branch in AUDITED_BRANCHES)

# Finding kinds.
MISSING_MARKER = "missing-marker"
MISSING_BRANCH = "missing-branch"
MARKER_NOT_ANCESTOR = "marker-not-ancestor"
NO_PR = "no-pr"
UNEXPECTED_ACTOR = "unexpected-actor"
UNTRUSTED_ACTOR = "untrusted-actor"
MAIN_MERGE = "main-merge"
PROVENANCE = "provenance"
TASK_ISSUE = "task-issue"
GATES = "gates"
READ_FAILURE = "read-failure"

# Timeline events whose actor is not the one who acted on the PR: GitHub
# names the mentioned or subscribed account, or the PR was referenced from
# another item, an action on that item.
_NOT_AN_ACTION = frozenset({"mentioned", "subscribed", "unsubscribed", "cross-referenced"})
_AUTO_MERGE_ENABLED = re.compile(r"auto_\w+_enabled")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_LOCAL_PREFIX = "refs/vergil-audit"

# The fields of an account a REST answer is projected to: never content.
_ACCOUNT = "if . then {login, type} else null end"

# The repository activity that ends a branch's life. It changes no content,
# so its actor is never a pusher, and the last one before a PR was opened
# bounds the head branch's current life from below.
_BRANCH_DELETION = "branch_deletion"


class AuditError(Exception):
    """A read the audit needs failed: never a pass."""


@dataclass(frozen=True)
class Finding:
    """One condition the audit reports: its *kind*, where, and the detail."""

    kind: str
    repo: str
    branch: str
    subject: str
    detail: str

    def render(self) -> str:
        return f"{self.kind}: {self.repo} {self.branch}: {self.subject}: {self.detail}"


@dataclass(frozen=True)
class Landing:
    """One PR's merge into the audited branch.

    *before* is the branch's tip before the landing; *commits* are the
    commits it added to the first-parent history, oldest first, and *end*,
    the last of them, is the PR's merge commit.
    """

    pr: int
    before: str
    commits: tuple[str, ...]

    @property
    def end(self) -> str:
        return self.commits[-1]


@dataclass
class BranchAudit:
    """One branch's result: its findings and the commit its marker may move to.

    *audited_to* is ``None`` when the marker stays where it is. *skipped* is
    true when the repo has neither the branch nor its marker, so the branch
    was not audited at all (:func:`not_audited` gives the line to report).
    """

    branch: str
    findings: list[Finding] = field(default_factory=list)
    audited_to: str | None = None
    skipped: bool = False


@dataclass(frozen=True)
class MarkerInit:
    """What :func:`init_markers` did for one audited branch.

    *sha* is the commit the marker holds, or ``None`` when the branch was
    skipped because the repo has neither it nor its marker. *created* is
    true when this run created the marker.
    """

    branch: str
    sha: str | None
    created: bool

    @property
    def marker(self) -> str:
        return marker_branch(self.branch)

    @property
    def skipped(self) -> bool:
        return self.sha is None


# --- the markers ---------------------------------------------------------------


def _ref_endpoint(repo: str, branch: str) -> str:
    return f"repos/{repo}/git/ref/heads/{branch}"


def _read_ref(repo: str, branch: str) -> str | None:
    """The commit *branch* of *repo* points at, read from GitHub, or ``None`` if absent."""
    endpoint = _ref_endpoint(repo, branch)
    try:
        result = github.run_tolerating(("gh", "api", "-i", endpoint), is_answer=github.is_http_404)
    except github.GitHubAPIError as exc:
        msg = f"cannot read {repo} branch {branch}: {_error_text(exc)}"
        raise AuditError(msg) from exc
    if github.is_http_404(result):
        return None
    # ``-i`` puts the status line and headers before the JSON body.
    payload = re.split(r"\r?\n\r?\n", result.stdout, maxsplit=1)[-1]
    try:
        data = cast("dict[str, Any]", json.loads(payload))
        ref = data["ref"]
        sha = data["object"]["sha"]
    except (ValueError, KeyError, TypeError) as exc:
        msg = f"cannot read {repo} branch {branch}: the answer is not a ref"
        raise AuditError(msg) from exc
    if ref != f"refs/heads/{branch}" or not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        msg = f"cannot read {repo} branch {branch}: GitHub answered with another ref"
        raise AuditError(msg)
    return sha


def read_marker(repo: str, branch: str) -> str | None:
    """The commit *branch*'s marker holds in *repo*, or ``None`` when it has none."""
    return _read_ref(repo, marker_branch(branch))


def create_marker(repo: str, branch: str, sha: str) -> None:
    """Create *branch*'s marker in *repo* at *sha*.

    The one create helper, shared with repo init. GitHub refuses it when the
    marker exists already, or the caller is neither the admin role nor the
    automation App; either raises :class:`AuditError`.
    """
    body: dict[str, object] = {"ref": f"refs/heads/{marker_branch(branch)}", "sha": sha}
    try:
        github.write_json("POST", f"repos/{repo}/git/refs", body)
    except subprocess.CalledProcessError as exc:
        msg = f"cannot create {repo} marker {marker_branch(branch)}: {_error_text(exc)}"
        raise AuditError(msg) from exc


def _advance_marker(repo: str, branch: str, sha: str) -> None:
    """Move *branch*'s marker to *sha*, a fast-forward only."""
    body: dict[str, object] = {"sha": sha, "force": False}
    endpoint = f"repos/{repo}/git/refs/heads/{marker_branch(branch)}"
    try:
        github.write_json("PATCH", endpoint, body)
    except subprocess.CalledProcessError as exc:
        msg = f"cannot move {repo} marker {marker_branch(branch)}: {_error_text(exc)}"
        raise AuditError(msg) from exc


def _absent_branch_error(repo: str, branch: str, marker: str | None) -> str | None:
    """Why an audited *branch* that *repo* does not have is an error, or ``None`` to skip it.

    A required branch (:data:`REQUIRED_BRANCHES`) is always an error. Any
    other is one when its marker is set: the branch disappeared after its
    audit began, and deleting a branch never ends its audit.
    """
    if branch in REQUIRED_BRANCHES:
        return f"{repo} has no {branch} branch; every repo has one, and it is always audited"
    if marker is not None:
        return (
            f"{repo} has no {branch} branch, but its marker {marker_branch(branch)} is set at"
            f" {marker[:12]}: an audited branch that disappeared is never skipped"
        )
    return None


def init_markers(repo: str) -> list[MarkerInit]:
    """Create each missing marker of *repo* at its branch's current tip; audit nothing.

    An existing marker is left untouched. A branch the repo has neither of
    nor its marker is skipped (``skipped`` in its :class:`MarkerInit`), unless
    it is a required branch. Every branch and marker is read before any
    marker is created, and an absent required branch, or an absent branch
    whose marker is set, raises :class:`AuditError` with nothing created.
    """
    found: list[tuple[str, str | None, str | None]] = []
    for branch in AUDITED_BRANCHES:
        existing = read_marker(repo, branch)
        tip = _read_ref(repo, branch)
        if tip is None:
            error = _absent_branch_error(repo, branch, existing)
            if error is not None:
                raise AuditError(error)
        found.append((branch, existing, tip))
    done: list[MarkerInit] = []
    for branch, existing, tip in found:
        if existing is not None:
            done.append(MarkerInit(branch, existing, created=False))
        elif tip is None:
            done.append(MarkerInit(branch, None, created=False))
        else:
            create_marker(repo, branch, tip)
            done.append(MarkerInit(branch, tip, created=True))
    return done


# --- git ----------------------------------------------------------------------------


def _error_text(exc: subprocess.CalledProcessError) -> str:
    stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
    return stderr or f"exit {exc.returncode}"


def _git(clone: Path, *args: str) -> str:
    try:
        return git.read_output("-C", str(clone), *args)
    except subprocess.CalledProcessError as exc:
        msg = f"git {' '.join(args)} failed: {_error_text(exc)}"
        raise AuditError(msg) from exc


def _fetch(clone: Path, remote_ref: str, local_ref: str) -> str:
    """Fetch *remote_ref* from ``origin`` into *local_ref* and give its commit."""
    _git(clone, "fetch", "--no-tags", "--quiet", "origin", f"+{remote_ref}:{local_ref}")
    return _git(clone, "rev-parse", "--verify", "--quiet", f"{local_ref}^{{commit}}")


def _first_parent(clone: Path, since: str, tip: str) -> list[str]:
    """The commits on *tip*'s first-parent history after *since*, oldest first."""
    out = _git(clone, "rev-list", "--first-parent", "--reverse", f"{since}..{tip}")
    return out.split()


def _parent(clone: Path, commit: str) -> str:
    return _git(clone, "rev-parse", "--verify", "--quiet", f"{commit}^1")


# --- GitHub reads -------------------------------------------------------------------


def _identity(value: object) -> trust.Identity:
    """The identity of a projected REST account ``{login, type}``; ``None`` is deleted."""
    data = cast("Mapping[str, object]", value) if isinstance(value, dict) else {}
    login = data.get("login")
    typename = data.get("type")
    return trust.Identity(
        login if isinstance(login, str) and login else None,
        typename if isinstance(typename, str) else None,
    )


def _read(what: str, reader: Callable[[], Any]) -> Any:
    try:
        return reader()
    except (subprocess.CalledProcessError, ValueError) as exc:
        detail = _error_text(exc) if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        msg = f"cannot read {what}: {detail}"
        raise AuditError(msg) from exc


def _merged_prs_of(repo: str, branch: str, commit: str) -> dict[int, str]:
    """The PRs merged into *branch* that GitHub associates with *commit*, to their merge commit."""
    rows = _read(
        f"the PRs of {repo} commit {commit}",
        lambda: github.read_json_paginated(
            f"repos/{repo}/commits/{commit}/pulls",
            jq="[.[] | {number, merged_at, merge_commit_sha, base: .base.ref}]",
        ),
    )
    found: dict[int, str] = {}
    for row in cast("list[object]", rows):
        data = cast("Mapping[str, object]", row) if isinstance(row, dict) else {}
        number = data.get("number")
        merge = data.get("merge_commit_sha")
        if not isinstance(number, int) or isinstance(number, bool):
            msg = f"cannot read the PRs of {repo} commit {commit}: a PR has no number"
            raise AuditError(msg)
        if data.get("merged_at") is None or data.get("base") != branch:
            continue
        if not isinstance(merge, str) or not _SHA_RE.fullmatch(merge):
            msg = f"cannot read the PRs of {repo} commit {commit}: PR #{number} has no merge commit"
            raise AuditError(msg)
        found[number] = merge
    return found


@dataclass(frozen=True)
class _PullRequest:
    """A merged PR. *same_repo* is whether its head was in the audited repo itself."""

    number: int
    url: str
    author: trust.Identity
    merged_by: trust.Identity | None
    merge_commit: str
    same_repo: bool
    head_ref: str
    created_at: datetime
    merged_at: datetime


# The REST pull's head projected to the fields :func:`trust.is_same_repo`
# reads. A head whose repository is gone (a deleted fork) is a fork's.
_SAME_REPO = (
    "isCrossRepository: (.head.repo == null or .head.repo.full_name != .base.repo.full_name),"
    " headRepositoryOwner: {login: .head.repo.owner.login},"
    " headRepository: {name: .head.repo.name}"
)


def _read_pull(repo: str, number: int) -> _PullRequest:
    jq = (
        "{number, html_url, merged, merge_commit_sha, created_at, merged_at, head_ref: .head.ref,"
        f" user: (.user | {_ACCOUNT}), merged_by: (.merged_by | {_ACCOUNT}), {_SAME_REPO}}}"
    )
    data = _read(
        f"{repo} PR #{number}",
        lambda: github.read_json("api", f"repos/{repo}/pulls/{number}", "--jq", jq),
    )
    if not isinstance(data, dict) or data.get("number") != number or data.get("merged") is not True:
        msg = f"cannot read {repo} PR #{number}: it does not read as a merged PR"
        raise AuditError(msg)
    url = data.get("html_url")
    merge = data.get("merge_commit_sha")
    if not isinstance(url, str) or not isinstance(merge, str):
        msg = f"cannot read {repo} PR #{number}: it has no URL or merge commit"
        raise AuditError(msg)
    head_ref = data.get("head_ref")
    if not isinstance(head_ref, str) or not head_ref:
        msg = f"cannot read {repo} PR #{number}: it has no head branch"
        raise AuditError(msg)
    what = f"{repo} PR #{number}"
    created_at = _timestamp(data.get("created_at"), what, "its creation time")
    merged_at = _timestamp(data.get("merged_at"), what, "its merge time")
    try:
        same_repo = trust.is_same_repo(repo, data)
    except trust.TrustReadError as exc:
        msg = f"cannot read {repo} PR #{number}: {exc}"
        raise AuditError(msg) from exc
    merged_by = data.get("merged_by")
    return _PullRequest(
        number=number,
        url=url,
        author=_identity(data.get("user")),
        merged_by=None if merged_by is None else _identity(merged_by),
        merge_commit=merge,
        same_repo=same_repo,
        head_ref=head_ref,
        created_at=created_at,
        merged_at=merged_at,
    )


def _timestamp(value: object, what: str, name: str) -> datetime:
    """The ISO 8601 time *value* GitHub gives as *what*'s *name*; anything else raises."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed
    msg = f"cannot read {what}: {name} is not a time"
    raise AuditError(msg)


def _branch_pushers(repo: str, pr: _PullRequest) -> tuple[trust.Identity, ...]:
    """Every account that pushed *pr*'s head branch in its current life, up to the merge.

    The repository activity records every push, force push and creation of
    a branch with its actor, including those made before the PR was opened:
    this reads that of ``refs/heads/<head branch>`` for a same-repo *pr*.

    A branch name can be reused, so the window is the branch's current
    life: after its last deletion before the PR was opened, up to the
    merge. A deletion is never a push. A ``null`` actor is a deleted
    account, which :func:`~vergil_tooling.lib.agent_gates.agent_involved`
    counts. When the window holds no activity at all, who pushed the
    branch cannot be told, so the deleted-account identity is given and
    the PR counts as involved: its gates are checked, never skipped. A
    read failure, or an entry with no type, actor or time or for another
    ref, raises :class:`AuditError`.
    """
    ref = f"refs/heads/{pr.head_ref}"
    what = f"the activity of {repo} branch {pr.head_ref}"
    jq = (
        "[.[] | {activity_type, timestamp, ref}"
        f' + (if has("actor") then {{actor: (.actor | {_ACCOUNT}), has_actor: true}} else {{}} end)]'
    )
    rows = _read(
        what,
        lambda: github.read_json_paginated(
            f"repos/{repo}/activity?ref={quote(ref, safe='/')}", jq=jq
        ),
    )
    entries: list[tuple[datetime, str, trust.Identity]] = []
    for row in cast("list[object]", rows):
        data = cast("Mapping[str, object]", row) if isinstance(row, dict) else {}
        kind = data.get("activity_type")
        if not isinstance(kind, str) or not kind:
            msg = f"cannot read {what}: an entry has no type"
            raise AuditError(msg)
        if data.get("ref") != ref:
            msg = f"cannot read {what}: an entry is for another ref"
            raise AuditError(msg)
        if data.get("has_actor") is not True:
            msg = f"cannot read {what}: an entry has no actor"
            raise AuditError(msg)
        at = _timestamp(data.get("timestamp"), what, "an entry's time")
        entries.append((at, kind, _identity(data.get("actor"))))
    start = max(
        (at for at, kind, _ in entries if kind == _BRANCH_DELETION and at < pr.created_at),
        default=None,
    )
    pushers = tuple(
        who
        for at, kind, who in entries
        if kind != _BRANCH_DELETION and (start is None or at > start) and at <= pr.merged_at
    )
    return pushers or (trust.Identity(None, None),)


@dataclass(frozen=True)
class _Timeline:
    """The actors of a PR's timeline: everyone who acted, and who merged or armed it."""

    actors: tuple[trust.Identity, ...]
    mergers: tuple[trust.Identity, ...]


def _read_timeline(repo: str, number: int, *, on_path: bool) -> _Timeline:
    """Every actor GitHub records on PR *number*'s timeline.

    An event's ``actor``, and a review's ``user``. A ``null`` account is a
    deleted one, which counts as an actor. The events whose named account did
    not act on the PR (:data:`_NOT_AN_ACTION`) are left out of the actors,
    and so are those that are not involvement
    (:func:`~vergil_tooling.lib.pr_provenance.not_involvement`, *on_path*
    being whether the one PR path accepts the PR). The mergers keep every
    merge and auto-merge enable: the actor check judges each one.
    """
    jq = (
        "[.[] | {event, created_at}"
        f' + (if has("actor") then {{actor: (.actor | {_ACCOUNT}), has_actor: true}} else {{}} end)'
        f' + (if has("user") then {{user: (.user | {_ACCOUNT}), has_user: true}} else {{}} end)]'
    )
    rows = _read(
        f"the timeline of {repo} PR #{number}",
        lambda: github.read_json_paginated(f"repos/{repo}/issues/{number}/timeline", jq=jq),
    )
    events = [cast("Mapping[str, object]", r) if isinstance(r, dict) else {} for r in rows]
    uninvolved = pr_provenance.not_involvement(events, on_path=on_path)
    actors: list[trust.Identity] = []
    mergers: list[trust.Identity] = []
    for position, data in enumerate(events):
        event = data.get("event")
        if not isinstance(event, str):
            msg = f"cannot read the timeline of {repo} PR #{number}: an event has no name"
            raise AuditError(msg)
        if event in _NOT_AN_ACTION:
            continue
        who: list[trust.Identity] = []
        if data.get("has_actor") is True:
            who.append(_identity(data.get("actor")))
        if data.get("has_user") is True:
            who.append(_identity(data.get("user")))
        if position not in uninvolved:
            actors += who
        if (event == "merged" or _AUTO_MERGE_ENABLED.fullmatch(event)) and who:
            mergers.append(who[0])
    return _Timeline(actors=tuple(actors), mergers=tuple(mergers))


def _task_refs(repo: str, number: int, authors: TrustedAuthors) -> list[str] | None:
    """The task issues PR *number*'s trusted body links, or ``None`` when its body is untrusted."""
    try:
        content = trust.read_issue(repo, number, authors=authors)
    except (trust.TrustReadError, subprocess.CalledProcessError) as exc:
        msg = f"cannot read the body of {repo} PR #{number}: {exc}"
        raise AuditError(msg) from exc
    if content.body is None:
        return None
    return [
        ref if not ref.startswith("#") else f"{repo}{ref}"
        for _, ref in extract_tracking_refs(content.body)
    ]


# --- landings -------------------------------------------------------------------------


def _landings(
    clone: Path, repo: str, branch: str, commits: list[str], findings: list[Finding]
) -> list[Landing | str]:
    """Group *commits* into landings, in order; a commit no landing claims stays a bare SHA.

    A commit is a landing's end when a PR merged into *branch* has it as its
    merge commit. The commits before it that GitHub associates with the same
    PR, back to the previous landing, are that landing's too (a rebase
    merge). Any other commit is a ``no-pr`` finding, appended to *findings*.
    Raises :class:`AuditError` on a read failure.
    """
    out: list[Landing | str] = []
    pending: list[tuple[str, dict[int, str]]] = []
    for commit in commits:
        prs = _merged_prs_of(repo, branch, commit)
        ends = [number for number, merge in prs.items() if merge == commit]
        if len(ends) > 1:
            msg = f"{repo} commit {commit} is the merge commit of several PRs: {sorted(ends)}"
            raise AuditError(msg)
        if not ends:
            pending.append((commit, prs))
            continue
        number = ends[0]
        claimed: list[str] = []
        while pending and number in pending[-1][1]:
            claimed.insert(0, pending.pop()[0])
        for stray, _ in pending:
            findings.append(
                Finding(NO_PR, repo, branch, stray[:12], "the commit landed with no merged PR")
            )
            out.append(stray)
        pending = []
        first = claimed[0] if claimed else commit
        out.append(Landing(pr=number, before=_parent(clone, first), commits=(*claimed, commit)))
    for stray, _ in pending:
        findings.append(
            Finding(NO_PR, repo, branch, stray[:12], "the commit landed with no merged PR")
        )
        out.append(stray)
    return out


# --- the checks ----------------------------------------------------------------------


def _shown(identity: trust.Identity) -> str:
    return "(deleted account)" if identity.login is None else trust.normalise_login(identity.login)


def _same(a: trust.Identity, b: trust.Identity) -> bool:
    if a.login is None or b.login is None:
        return False
    return trust.normalise_login(a.login) == trust.normalise_login(b.login) and (
        a.typename == b.typename
    )


def _actor_findings(
    repo: str,
    branch: str,
    pr: _PullRequest,
    timeline: _Timeline,
    authors: TrustedAuthors,
    *,
    on_path: bool,
) -> list[Finding]:
    """The merge and auto-merge actors of *pr* outside the expected set.

    On ``develop``, *on_path* (the one PR path accepts *pr*) lets the user
    agent and the automation App arm or merge it whoever authored it.
    """
    org = repo.split("/", 1)[0]
    subject = f"PR #{pr.number}"
    mergers = list(timeline.mergers)
    if pr.merged_by is not None:
        mergers.append(pr.merged_by)
    if not mergers:
        return [Finding(UNEXPECTED_ACTOR, repo, branch, subject, "GitHub records no merge actor")]
    found: list[Finding] = []
    seen: set[trust.Identity] = set()
    for actor in mergers:
        if actor in seen:
            continue
        seen.add(actor)
        role = pr_provenance.classify(actor, org=org, authors=authors)
        own = _same(actor, pr.author)
        if branch == "main":
            ok = role is pr_provenance.Role.HUMAN or (
                APP_MERGES_RELEASES and role is pr_provenance.Role.AUTOMATION
            )
            if not ok:
                found.append(
                    Finding(
                        MAIN_MERGE,
                        repo,
                        branch,
                        subject,
                        f"merged or armed by {_shown(actor)} ({role.value}); only the human"
                        " merges into main",
                    )
                )
            continue
        ok = role is pr_provenance.Role.HUMAN or (
            (own or on_path) and role in (pr_provenance.Role.USER, pr_provenance.Role.AUTOMATION)
        )
        if ok:
            continue
        kind = UNTRUSTED_ACTOR if role is pr_provenance.Role.UNTRUSTED else UNEXPECTED_ACTOR
        found.append(
            Finding(
                kind,
                repo,
                branch,
                subject,
                f"merged or armed by {_shown(actor)} ({role.value})"
                + ("" if own else f" on a PR {_shown(pr.author)} authored"),
            )
        )
    return found


def _provenance_findings(repo: str, branch: str, pr: _PullRequest) -> list[Finding]:
    """The violations :func:`~vergil_tooling.lib.pr_provenance.check_pr` finds on *pr*.

    The check reads for itself whether the one PR path accepts *pr*, and
    relaxes its arm and merge as the actor check does.
    """
    try:
        result = pr_provenance.check_pr(pr.url)
    except (subprocess.CalledProcessError, ValueError, trust.TrustReadError) as exc:
        msg = f"cannot check the provenance of {repo} PR #{pr.number}: {exc}"
        raise AuditError(msg) from exc
    return [
        Finding(
            PROVENANCE,
            repo,
            branch,
            f"PR #{pr.number}",
            f"{act.login} ({act.role.value}) {act.action}, which its role forbids",
        )
        for act in result.violations
    ]


def _gate_findings(
    clone: Path,
    repo: str,
    branch: str,
    landing: Landing,
    authors: TrustedAuthors,
) -> list[Finding]:
    """The gates on an agent-involved landing."""
    subject = f"PR #{landing.pr}"
    refs = _task_refs(repo, landing.pr, authors)
    if refs is None:
        return [
            Finding(
                TASK_ISSUE,
                repo,
                branch,
                subject,
                "the PR's body is not trusted, so its task issues cannot be read",
            )
        ]
    head = _fetch(clone, f"refs/pull/{landing.pr}/head", f"{_LOCAL_PREFIX}/pull/{landing.pr}")
    result = agent_gates.evaluate(
        clone, base=landing.before, head=head, issue_refs=refs, authors=authors
    )
    if result.read_failure:
        msg = f"the gates on {repo} PR #{landing.pr} could not be read: {'; '.join(result.reasons)}"
        raise AuditError(msg)
    return [Finding(GATES, repo, branch, subject, reason) for reason in result.reasons]


def _check_landing(
    clone: Path, repo: str, branch: str, landing: Landing, authors: TrustedAuthors
) -> list[Finding]:
    pr = _read_pull(repo, landing.pr)
    if pr.merge_commit != landing.end:
        msg = (
            f"{repo} PR #{landing.pr} reads with merge commit {pr.merge_commit}, not {landing.end}"
        )
        raise AuditError(msg)
    org = repo.split("/", 1)[0]
    branch_trusted = (
        pr_provenance.classify(pr.author, org=org, authors=authors)
        is not pr_provenance.Role.UNTRUSTED
    )
    on_path = pr.same_repo and branch_trusted
    timeline = _read_timeline(repo, landing.pr, on_path=on_path)
    found = _actor_findings(repo, branch, pr, timeline, authors, on_path=on_path)
    found += _provenance_findings(repo, branch, pr)
    if branch != "develop":
        return found
    pushers = _branch_pushers(repo, pr) if pr.same_repo else ()
    involved = agent_gates.agent_involved(
        authors=authors, actors=(pr.author, *timeline.actors, *pushers)
    )
    if involved:
        found += _gate_findings(clone, repo, branch, landing, authors)
    return found


def audit_branch(clone: Path, repo: str, branch: str, authors: TrustedAuthors) -> BranchAudit:
    """Audit every landing on *branch* of *repo* since its marker, in the clone *clone*."""
    result = BranchAudit(branch=branch)
    marker_name = marker_branch(branch)

    def fail(detail: str) -> BranchAudit:
        result.findings.append(Finding(READ_FAILURE, repo, branch, "the audit", detail))
        return result

    try:
        marker = read_marker(repo, branch)
        if _read_ref(repo, branch) is None:
            error = _absent_branch_error(repo, branch, marker)
            if error is None:
                result.skipped = True
            else:
                result.findings.append(Finding(MISSING_BRANCH, repo, branch, branch, error))
            return result
        if marker is None:
            result.findings.append(
                Finding(
                    MISSING_MARKER,
                    repo,
                    branch,
                    marker_name,
                    "the marker is missing; it is never re-created by the audit",
                )
            )
            return result
        tip = _fetch(clone, f"refs/heads/{branch}", f"{_LOCAL_PREFIX}/heads/{branch}")
        fetched = _fetch(clone, f"refs/heads/{marker_name}", f"{_LOCAL_PREFIX}/markers/{branch}")
        if fetched != marker:
            return fail(
                f"the marker {marker_name} reads as {marker} on GitHub but {fetched} in git"
            )
        if not git.is_ancestor(marker, tip, repo=clone):
            result.findings.append(
                Finding(
                    MARKER_NOT_ANCESTOR,
                    repo,
                    branch,
                    marker_name,
                    f"the marker {marker[:12]} is not on {branch}'s history ({tip[:12]})",
                )
            )
            return result
        commits = _first_parent(clone, marker, tip)
    except (AuditError, subprocess.CalledProcessError) as exc:
        return fail(str(exc))
    try:
        items = _landings(clone, repo, branch, commits, result.findings)
    except AuditError as exc:
        return fail(str(exc))
    for item in items:
        if isinstance(item, str):
            result.audited_to = item
            continue
        try:
            result.findings += _check_landing(clone, repo, branch, item, authors)
        except AuditError as exc:
            return fail(str(exc))
        result.audited_to = item.end
    return result


def audit(clone: Path, repo: str, authors: TrustedAuthors) -> list[BranchAudit]:
    """Audit both branches of *repo*, then move each marker to where its audit reached.

    A branch the repo has neither of nor its marker comes back ``skipped``,
    with no finding. A marker that cannot be moved is a ``read-failure``
    finding.
    """
    results: list[BranchAudit] = []
    for branch in AUDITED_BRANCHES:
        result = audit_branch(clone, repo, branch, authors)
        if result.audited_to is not None:
            try:
                _advance_marker(repo, branch, result.audited_to)
            except AuditError as exc:
                result.findings.append(Finding(READ_FAILURE, repo, branch, "the marker", str(exc)))
        results.append(result)
    return results
