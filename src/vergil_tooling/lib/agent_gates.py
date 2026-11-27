"""The shared gate evaluator: merge-review coverage.

Submit, the finalize watch and the daily audit (epic
vergil-project/.github#1384, spec §4.6 and §4.8) all decide whether a change
may reach ``develop`` with the one function here, :func:`evaluate`. It applies
one rule to the change from the merge base of *base* and *head* to *head*:

- **Merge review.** The change must be covered by fingerprint
  (:mod:`~vergil_tooling.lib.change_fingerprint`). A single-issue change
  needs, on its issue, a merge-review record
  (:func:`~vergil_tooling.lib.merge_review.record.records`; both outcomes,
  ``eligible`` and ``not-eligible (<reason>)``, pass), or a verified, approving
  ``merge-review-exemption`` exchange
  (:func:`~vergil_tooling.lib.rulings.approvals.verified_approvals`) whose
  fingerprint equals the change's. A batch change is covered when either
  of those, on any of its issues, has the whole change's fingerprint (a fix
  pushed to an open batch is reviewed as a whole), or else when each issue's
  own commit, the one commit on the branch whose subject ends in
  `` (#<N>)``, is covered on that issue by its
  :func:`~vergil_tooling.lib.change_fingerprint.commit_fingerprint` and the
  branch holds no other commit (an unfolded ``fixup!`` or a later fix is
  covered only as part of the whole change). The commits are covered in
  order as a chain (spec §5.4): commit k's covering record or approved
  exemption must be at or after the review of commit k-1, so a changed
  earlier commit voids every later record until it is reviewed again.

:func:`evaluate_commits` is the same rule with its verdict kept per commit:
one :class:`CommitVerdict` for each issue of a batch (its own commit, the
chain's verdict on it), and the reasons that belong to no issue's commit
(a commit no issue claims). The batch probe (``vrg-pr-workflow batch-gates``)
reads it, so the probe and :func:`evaluate` judge every commit by one walk.

**Fail closed.** Every lookup failure (an unreadable ref, diff,
comment stream or configured login) gives ``ok=False`` with the error as
the reason, as does a change with no resolvable task issue. Nothing passes
for want of an answer.

**Agent involvement.** :func:`evaluate` does not ask whether a change is
agent-involved; each caller decides when to call it. Submit
(:mod:`vergil_tooling.bin.vrg_submit_pr`) calls it on every submission,
whoever submits it and whoever recorded the ready-state. The finalize watch
(:mod:`vergil_tooling.lib.finalize_watch`) and the daily audit
(:mod:`vergil_tooling.lib.agent_audit`) call it on an agent-involved PR
(for the audit, one merged into ``develop``), asking :func:`agent_involved`
first, the one shared test of that.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, trust
from vergil_tooling.lib.change_fingerprint import (
    FingerprintError,
    change_fingerprint,
    commit_fingerprint,
)
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.merge_review.record import (
    FIXUP_PREFIX,
    RecordError,
    branch_commits,
    records,
    subject_names,
)
from vergil_tooling.lib.rulings import RulingError, comments
from vergil_tooling.lib.rulings.approvals import verified_approvals

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from vergil_tooling.lib.merge_review.record import Record
    from vergil_tooling.lib.rulings.approvals import Approval
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

__all__ = [
    "AGENT_MODES",
    "CommitVerdict",
    "CommitVerdicts",
    "GateError",
    "GateResult",
    "agent_involved",
    "evaluate",
    "evaluate_commits",
]

#: The identity modes that are agent identities: a ready-state one of them
#: recorded makes the change agent-involved.
AGENT_MODES = frozenset({IdentityMode.USER, IdentityMode.AUDIT})

#: Every error a lookup raises; :func:`evaluate` turns each into a reason.
_LOOKUP_ERRORS = (
    trust.TrustReadError,
    RulingError,
    FingerprintError,
    RecordError,
)


class GateError(Exception):
    """A gate was asked a question it cannot answer: a programming error, never a pass."""


@dataclass(frozen=True)
class GateResult:
    """Whether a change passes the merge-review rule, and every reason it does not.

    *read_failure* is true when a reason is a failed lookup rather than a
    rule the change breaks, so a caller that can try again later (the
    audit) tells the two apart.
    """

    ok: bool
    reasons: tuple[str, ...]
    read_failure: bool = False


@dataclass(frozen=True)
class CommitVerdict:
    """The merge-review verdict on one issue's commit of a change.

    *sha* is the issue's own commit (the head's commit for a single-issue
    change), or ``None`` when the branch holds no single commit for the issue,
    or when the verdict was not worked out commit by commit. *reason* is
    ``None`` exactly when *covered* is true.
    """

    issue: str
    sha: str | None
    covered: bool
    reason: str | None


@dataclass(frozen=True)
class CommitVerdicts:
    """The per-commit merge-review verdicts of a change, and the reasons no commit holds.

    *commits* has one verdict per issue, in the batch's order. *extra_reasons*
    are the reasons that belong to no issue's commit: a commit no issue
    claims, or a task issue that cannot be used. *read_failure* is true when
    a lookup failed; the change then has no verdicts, and the failure is in
    *extra_reasons*.
    """

    commits: tuple[CommitVerdict, ...]
    extra_reasons: tuple[str, ...] = ()
    read_failure: bool = False

    @property
    def ok(self) -> bool:
        """Whether every commit is covered and no reason belongs to none."""
        return not self.extra_reasons and all(commit.covered for commit in self.commits)

    @property
    def reasons(self) -> tuple[str, ...]:
        """Every reason the rule fails, in :func:`evaluate`'s order."""
        given = tuple(c.reason for c in self.commits if c.reason is not None)
        return given + self.extra_reasons


# --- agent involvement ------------------------------------------------------------


def agent_involved(
    *,
    authors: TrustedAuthors,
    recorded_by: IdentityMode | None = None,
    actors: Iterable[trust.Identity] | None = None,
) -> bool:
    """Whether a change is agent-involved, so the rule of :func:`evaluate` applies.

    Give exactly one piece of evidence:

    - *recorded_by*, from a ready state (the finalize watch reads it from
      the relay ref): the identity mode that recorded the ready-state. It
      is agent-involved when that is an agent identity
      (:data:`AGENT_MODES`); nothing else is consulted.
    - *actors*, for an open or merged PR (the watch and the audit): the
      identities GitHub records for it, its author and every actor of an
      action on it. It is agent-involved when any of them is an agent login
      on the trusted authors' ``agents`` list, whatever its account type, or
      is a deleted account, whose identity cannot be ruled out.

    Raises :class:`GateError` unless exactly one is given.
    """
    if (recorded_by is None) == (actors is None):
        msg = "agent_involved takes exactly one of recorded_by and actors"
        raise GateError(msg)
    if recorded_by is not None:
        return recorded_by in AGENT_MODES
    agents = {trust.normalise_login(login) for login in authors.agents}
    for actor in actors or ():
        if actor.login is None:
            return True
        login = trust.normalise_login(actor.login)
        if login == trust.GHOST or login in agents:
            return True
    return False


# --- the evaluator ----------------------------------------------------------------


class _Lookups:
    """The comment reads of one evaluation, each made at most once per issue."""

    def __init__(self, authors: TrustedAuthors) -> None:
        self.authors = authors
        self._approvals: dict[str, list[Approval]] = {}
        self._records: dict[str, list[Record]] = {}

    def approvals(self, ref: str) -> list[Approval]:
        if ref not in self._approvals:
            self._approvals[ref] = verified_approvals(ref, authors=self.authors)
        return self._approvals[ref]

    def found(self, ref: str) -> list[Record]:
        if ref not in self._records:
            self._records[ref] = records(ref, authors=self.authors)
        return self._records[ref]

    def covers(self, ref: str, fingerprint: str) -> bool:
        """Whether a record or an exemption on *ref* covers *fingerprint*, whenever it was given."""
        if any(record.fingerprint == fingerprint for record in self.found(ref)):
            return True
        return any(approval.fingerprint == fingerprint for approval in self.approvals(ref))

    def entries(self, ref: str) -> list[tuple[str, str]]:
        """Each ``(fingerprint, at)`` a record or an approved exemption on *ref* holds.

        *at* is the time the record was posted, or the approving reply was: ISO 8601.
        """
        found = [(record.fingerprint, record.at) for record in self.found(ref)]
        approved = [(approval.fingerprint, approval.at) for approval in self.approvals(ref)]
        return found + approved


def _resolve_refs(issue_refs: list[str], change: str) -> tuple[list[str], list[str]]:
    """The distinct task issue refs, in order, and a reason for each that is unusable."""
    reasons: list[str] = []
    refs: list[str] = []
    for ref in issue_refs:
        try:
            comments.parse_issue_ref(ref)
        except RulingError as exc:
            reasons.append(f"task issue: {exc}")
            continue
        if ref not in refs:
            refs.append(ref)
    if not refs and not reasons:
        reasons.append(
            f"task issue: the change {change} resolves to no task issue;"
            " the gates never pass without one"
        )
    if len({comments.parse_issue_ref(ref)[0] for ref in refs}) > 1:
        reasons.append(f"task issue: the issues {', '.join(refs)} are not all in one repository")
    return refs, reasons


def _issue_commit(commits: list[tuple[str, str]], ref: str) -> tuple[str | None, str | None]:
    """The one commit of a batch branch for *ref*, or ``None`` and why there is none."""
    number = comments.parse_issue_ref(ref)[1]
    own = [
        sha
        for sha, subject in commits
        if subject_names(subject, number) and not subject.startswith(FIXUP_PREFIX)
    ]
    if len(own) != 1:
        return None, (
            f"merge review: the batch branch holds {len(own)} commits for {ref}"
            f" (subject ending in ' (#{number})'); it must hold exactly one"
        )
    return own[0], None


def _moment(value: str, ref: str) -> datetime:
    """The time *value* (ISO 8601) a record or approval on *ref* was posted, read strictly."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"the time {value!r} of a record or approval on {ref} is not ISO 8601"
        raise RecordError(msg) from exc
    if moment.tzinfo is None:
        msg = f"the time {value!r} of a record or approval on {ref} has no time zone"
        raise RecordError(msg)
    return moment


def _review_time(
    lookups: _Lookups, ref: str, fingerprint: str, after: datetime | None
) -> datetime | None:
    """The earliest time, at or after *after*, a record or approval on *ref* covers *fingerprint*.

    ``None`` when none does. Every time on *ref* is read, so a malformed one
    fails the lookup rather than being skipped.
    """
    timed = [(fp, _moment(at, ref)) for fp, at in lookups.entries(ref)]
    eligible = [
        moment for fp, moment in timed if fp == fingerprint and (after is None or moment >= after)
    ]
    return min(eligible, default=None)


def _uncovered_reason(
    lookups: _Lookups,
    ref: str,
    sha: str,
    subject: str,
    fingerprint: str,
    whole: str,
    anchor: str | None,
) -> str:
    """Why batch commit *sha* on *ref* is uncovered, given its bounding commit *anchor*.

    *anchor* is ``None`` only for the batch's first commit, which has no earlier
    review to be after. Past a chain break, *anchor* is the commit where the
    chain broke.
    """
    if anchor is None:
        return (
            f"merge review: no merge-review record or approved exemption on {ref}"
            f" covers its commit {sha[:12]} (fingerprint {fingerprint}),"
            f" and none on the batch covers the whole change ({whole})"
        )
    if lookups.covers(ref, fingerprint):
        return (
            f"merge review: commit {sha[:12]} ({subject}) is uncovered: its record on {ref}"
            f" (fingerprint {fingerprint}) predates the review of commit {anchor[:12]};"
            " it must be reviewed again"
        )
    return (
        f"merge review: no merge-review record or approved exemption on {ref} covers its"
        f" commit {sha[:12]} (fingerprint {fingerprint}) after the review of commit"
        f" {anchor[:12]}, and none on the batch covers the whole change ({whole})"
    )


def _head_commit(repo: Path, head: str) -> str:
    """The full sha of the commit *head* names, for a single-issue verdict."""
    try:
        return git.read_output("-C", str(repo), "rev-parse", "--verify", f"{head}^{{commit}}")
    except (subprocess.CalledProcessError, OSError) as exc:
        msg = f"cannot read the commit {head}: {exc}"
        raise RecordError(msg) from exc


def _covered_by_whole(
    repo: Path, base: str, head: str, refs: list[str], *, detail: bool
) -> tuple[list[CommitVerdict], list[str]]:
    """The verdicts when a record or exemption covers the whole change.

    Without *detail* nothing more is looked up, as :func:`evaluate` needs no
    more. With it, each issue gets its verdict: covered, with its own commit
    when the branch holds exactly one for it (``None`` when it does not, the
    whole change covering it).
    """
    if not detail:
        return [], []
    if len(refs) == 1:
        return [CommitVerdict(refs[0], _head_commit(repo, head), True, None)], []
    commits = branch_commits(repo, base, head)
    verdicts = []
    for ref in refs:
        sha, _problem = _issue_commit(commits, ref)
        verdicts.append(CommitVerdict(ref, sha, True, None))
    return verdicts, []


def _walk(
    repo: Path,
    base: str,
    head: str,
    refs: list[str],
    lookups: _Lookups,
    *,
    detail: bool,
) -> tuple[list[CommitVerdict], list[str]]:
    """Every issue's verdict in the batch's order, and the reasons no issue's commit holds.

    The single source of the rule: :func:`evaluate` reads the reasons of the
    verdicts and the extra reasons, in that order, and :func:`evaluate_commits`
    keeps the verdicts.
    """
    whole = change_fingerprint(repo, base, head)
    if any(lookups.covers(ref, whole) for ref in refs):
        return _covered_by_whole(repo, base, head, refs, detail=detail)
    if len(refs) == 1:
        reason = (
            f"merge review: no merge-review record or approved exemption"
            f" on {refs[0]} covers the change (fingerprint {whole}); run merge review"
        )
        sha = _head_commit(repo, head) if detail else None
        return [CommitVerdict(refs[0], sha, False, reason)], []
    commits = branch_commits(repo, base, head)
    subjects = dict(commits)
    verdicts: list[CommitVerdict] = []
    claimed: set[str] = set()
    # The chain (spec 5.4), walked in the batch's order. ``bound`` is c(k-1), the
    # earliest review of the previous commit, and ``anchor`` the commit it is of.
    # Once one commit is uncovered, ``broke`` names it and every later commit is
    # uncovered too.
    bound: datetime | None = None
    anchor: str | None = None
    broke: str | None = None
    for ref in refs:
        sha, problem = _issue_commit(commits, ref)
        if sha is None:
            verdicts.append(
                CommitVerdict(
                    ref,
                    None,
                    False,
                    f"{problem}, or a record must cover the whole change ({whole})",
                )
            )
            continue
        claimed.add(sha)
        fingerprint = commit_fingerprint(repo, sha)
        if broke is None:
            covered_at = _review_time(lookups, ref, fingerprint, bound)
            if covered_at is not None:
                bound, anchor = covered_at, sha
                verdicts.append(CommitVerdict(ref, sha, True, None))
                continue
            broke = sha
            reason_anchor = anchor
        else:
            reason_anchor = broke
        reason = _uncovered_reason(
            lookups, ref, sha, subjects[sha], fingerprint, whole, reason_anchor
        )
        verdicts.append(CommitVerdict(ref, sha, False, reason))
    extras = [
        f"merge review: commit {sha[:12]} ({subject}) is no issue's own commit, so only a"
        f" record or exemption covering the whole change ({whole}) covers it"
        for sha, subject in commits
        if sha not in claimed
    ]
    return verdicts, extras


def _judge(
    repo: Path,
    base: str,
    head: str,
    refs: list[str],
    authors: TrustedAuthors,
    *,
    detail: bool,
) -> CommitVerdicts:
    """:func:`_walk` as verdicts; a lookup failure fails closed, with no verdicts."""
    change = f"{base}...{head}"
    try:
        verdicts, extras = _walk(repo, base, head, refs, _Lookups(authors), detail=detail)
    except _LOOKUP_ERRORS as exc:
        failure = f"merge review: lookup failed for {change}: {exc}"
        return CommitVerdicts(commits=(), extra_reasons=(failure,), read_failure=True)
    return CommitVerdicts(commits=tuple(verdicts), extra_reasons=tuple(extras))


def evaluate(
    repo: Path, *, base: str, head: str, issue_refs: list[str], authors: TrustedAuthors
) -> GateResult:
    """Apply the merge-review rule to the change from the merge base of *base* and *head*.

    *issue_refs* are the change's task issues (``owner/repo#N``, one for a
    single-issue change, every issue of a batch in order), all in one
    repository. *base* is the freshly fetched ``origin/develop`` at submit
    and in the watch, the merge commit's first parent in the audit. *authors*
    is the trusted-authors configuration
    (:func:`~vergil_tooling.lib.trusted_authors.load_trusted_authors`).

    ``ok`` is true only when the rule passes; every reason it fails is given.
    A lookup failure fails it with the error as the reason, and a change with
    no usable task issue fails it, so nothing passes for want of an answer.
    """
    change = f"{base}...{head}"
    refs, reasons = _resolve_refs(issue_refs, change)
    if reasons:
        return GateResult(ok=False, reasons=tuple(reasons))
    judged = _judge(repo, base, head, refs, authors, detail=False)
    return GateResult(ok=judged.ok, reasons=judged.reasons, read_failure=judged.read_failure)


def evaluate_commits(
    repo: Path, *, base: str, head: str, issue_refs: list[str], authors: TrustedAuthors
) -> CommitVerdicts:
    """The rule of :func:`evaluate`, with a verdict for each issue's commit.

    Takes the same arguments and judges the same change by the same walk, so
    ``ok`` and ``reasons`` agree with :func:`evaluate`'s. The difference is
    that the change's commits are read even when a record covers the whole
    change: each issue's verdict then names its own commit and is covered, with
    no reason. A single-issue change has one verdict, for the head's commit.
    A task issue that cannot be used, or an unreadable ref, gives no verdicts
    and the reason in ``extra_reasons``.
    """
    change = f"{base}...{head}"
    refs, reasons = _resolve_refs(issue_refs, change)
    if reasons:
        return CommitVerdicts(commits=(), extra_reasons=tuple(reasons))
    return _judge(repo, base, head, refs, authors, detail=True)
