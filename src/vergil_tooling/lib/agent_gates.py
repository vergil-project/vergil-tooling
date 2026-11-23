"""The shared gate evaluator: protected-change approvals and merge-review coverage.

Submit, the finalize watch and the daily audit (epic
vergil-project/.github#1384, spec §4.6 and §4.8) all decide whether a change
may reach ``develop`` with the one function here, :func:`evaluate`. It applies
two rules to the change from the merge base of *base* and *head* to *head*:

- **Protected files.** Every protected path the change touches
  (:func:`~vergil_tooling.lib.protected_paths.changed_protected`, matched
  against the protected set as committed at *base*, never at *head*) must be
  covered, as the exact ``(path, blob)`` pair, by a verified, approving
  ``protected-change`` exchange
  (:func:`~vergil_tooling.lib.rulings.approvals.verified_approvals`) on any of
  the change's task issues. A blob approved for one path never covers
  another, and a deletion is covered only by an approval recording
  ``deleted`` for that path. A merge-review exemption or a human override
  never covers a protected file.
- **Merge review.** The change must be covered by fingerprint
  (:mod:`~vergil_tooling.lib.change_fingerprint`). A single-issue change
  needs, on its issue, a merge-review record
  (:func:`~vergil_tooling.lib.merge_review.record.records`; both outcomes,
  ``eligible`` and ``not-eligible (<reason>)``, pass), a verified, approving
  ``merge-review-exemption`` exchange, or a human override (:func:`overrides`)
  whose fingerprint equals the change's. A batch change is covered when one
  of those, on any of its issues, has the whole change's fingerprint (a fix
  pushed to an open batch is reviewed as a whole), or else when each issue's
  own commit, the one commit on the branch whose subject ends in
  `` (#<N>)``, is covered on that issue by its
  :func:`~vergil_tooling.lib.change_fingerprint.commit_fingerprint` and the
  branch holds no other commit (an unfolded ``fixup!`` or a later fix is
  covered only as part of the whole change).

**The human override** is a comment by the human login on the task issue
whose first line is exactly::

    Merge review override: fingerprint=<sha256> reason=<text>

It is keyed by the change fingerprint, not by a PR, since the human posts it
before the PR exists. It stands in for the merge-review record only.

**Fail closed.** Every lookup failure (an unreadable registry, ref, diff,
comment stream or configured login) gives ``ok=False`` with the error as
the reason, as does a change with no resolvable task issue. Nothing passes
for want of an answer.

**Agent involvement.** The rules apply to an agent-involved change only; a
change done entirely by the human carries the human's own decision.
:func:`agent_involved` is the one shared test of that, and :func:`evaluate`
does not apply it: the caller asks first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import trust
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
from vergil_tooling.lib.protected_paths import ProtectedPathsError, changed_protected
from vergil_tooling.lib.rulings import RulingError, comments
from vergil_tooling.lib.rulings.approvals import verified_approvals
from vergil_tooling.lib.rulings.identity import human_login
from vergil_tooling.lib.rulings.plain_text import check_plain_text

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from vergil_tooling.lib.rulings.approvals import Approval
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

__all__ = [
    "AGENT_MODES",
    "OVERRIDE_PREFIX",
    "GateError",
    "GateResult",
    "Override",
    "agent_involved",
    "evaluate",
    "overrides",
    "parse_override",
    "render_override",
]

OVERRIDE_PREFIX = "Merge review override:"

#: The identity modes that are agent identities: a ready-state one of them
#: recorded makes the change agent-involved.
AGENT_MODES = frozenset({IdentityMode.USER, IdentityMode.AUDIT})

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_OVERRIDE_RE = re.compile(
    re.escape(OVERRIDE_PREFIX) + r" fingerprint=(?P<fingerprint>[0-9a-f]{64}) reason=(?P<reason>.+)"
)

#: Every error a lookup raises; :func:`evaluate` turns each into a reason.
_LOOKUP_ERRORS = (
    trust.TrustReadError,
    RulingError,
    ProtectedPathsError,
    FingerprintError,
    RecordError,
)


class GateError(Exception):
    """A gate was asked a question it cannot answer: a programming error, never a pass."""


@dataclass(frozen=True)
class GateResult:
    """Whether a change passes both rules, and every reason it does not."""

    ok: bool
    reasons: tuple[str, ...]


# --- the human override --------------------------------------------------------


@dataclass(frozen=True)
class Override:
    """One human override: the change fingerprint it covers and the human's reason.

    ``comment_id`` and ``author`` are the comment's, set by :func:`overrides`;
    an override parsed from a bare body has ``0`` and ``""``.
    """

    fingerprint: str
    reason: str
    comment_id: int = 0
    author: str = ""


def render_override(fingerprint: str, reason: str) -> str:
    """The override line for the change with *fingerprint*, giving the human's *reason*.

    Refuses (:class:`GateError`) a fingerprint that is not a lowercase SHA-256
    hex digest and a reason that is not one non-empty line of plain text.
    """
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        msg = "the change fingerprint must be a lowercase SHA-256 hex digest"
        raise GateError(msg)
    if not reason.strip() or reason != reason.strip() or "\n" in reason:
        msg = "the override reason must be one non-empty line with no surrounding whitespace"
        raise GateError(msg)
    try:
        check_plain_text(reason, what="the override reason")
    except RulingError as exc:
        raise GateError(str(exc)) from exc
    return f"{OVERRIDE_PREFIX} fingerprint={fingerprint} reason={reason}"


def parse_override(body: str) -> Override | None:
    """The override a comment *body* carries on its first line, or ``None``.

    Only the first line counts, and only in exactly the form
    :func:`render_override` gives: a quoted or malformed override line is no
    override.
    """
    first = body.split("\n", 1)[0].rstrip("\r")
    match = _OVERRIDE_RE.fullmatch(first)
    if match is None:
        return None
    try:
        render_override(match["fingerprint"], match["reason"])
    except GateError:
        return None
    return Override(fingerprint=match["fingerprint"], reason=match["reason"])


def overrides(issue_ref: str, *, authors: TrustedAuthors) -> list[Override]:
    """Every human override on *issue_ref*, oldest first.

    Read from the trust layer's comment stream narrowed to the human role,
    and kept only when the comment's author is the human login
    (:func:`~vergil_tooling.lib.rulings.identity.human_login`) and it was
    never edited. A read failure raises
    (:class:`~vergil_tooling.lib.trust.TrustReadError`), as does a
    misconfigured login (:class:`~vergil_tooling.lib.rulings.RulingError`);
    it never returns ``[]``.
    """
    repo, issue = comments.parse_issue_ref(issue_ref)
    human = human_login(authors)
    stream = comments.read_stream(repo, issue, authors, roles=frozenset({trust.Role.HUMAN}))
    found: list[Override] = []
    for comment in stream.comments:
        if comment.author != human or comment.edited:
            continue
        override = parse_override(comment.body)
        if override is not None:
            found.append(
                Override(
                    fingerprint=override.fingerprint,
                    reason=override.reason,
                    comment_id=comment.id,
                    author=comment.author,
                )
            )
    return found


# --- agent involvement ------------------------------------------------------------


def agent_involved(
    *,
    authors: TrustedAuthors,
    recorded_by: IdentityMode | None = None,
    actors: Iterable[trust.Identity] | None = None,
) -> bool:
    """Whether a change is agent-involved, so both rules of :func:`evaluate` apply.

    Give exactly one piece of evidence:

    - *recorded_by*, at submit, where no PR exists yet: the identity mode
      that recorded the ready-state. It is agent-involved when that is an
      agent identity (:data:`AGENT_MODES`); nothing else is consulted.
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
        self._records: dict[str, frozenset[str]] = {}
        self._overrides: dict[str, frozenset[str]] = {}

    def approvals(self, ref: str) -> list[Approval]:
        if ref not in self._approvals:
            self._approvals[ref] = verified_approvals(ref, authors=self.authors)
        return self._approvals[ref]

    def covers(self, ref: str, fingerprint: str) -> bool:
        """Whether a record, an exemption or an override on *ref* covers *fingerprint*."""
        if ref not in self._records:
            found = records(ref, authors=self.authors)
            self._records[ref] = frozenset(record.fingerprint for record in found)
        if fingerprint in self._records[ref]:
            return True
        exempted = {
            a.fingerprint for a in self.approvals(ref) if a.kind == "merge-review-exemption"
        }
        if fingerprint in exempted:
            return True
        if ref not in self._overrides:
            self._overrides[ref] = frozenset(
                o.fingerprint for o in overrides(ref, authors=self.authors)
            )
        return fingerprint in self._overrides[ref]


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


def _protected_reasons(
    repo: Path, base: str, head: str, refs: list[str], lookups: _Lookups
) -> list[str]:
    changes = changed_protected(repo, base, head, base)
    if not changes:
        return []
    approved = {
        (path, blob)
        for ref in refs
        for approval in lookups.approvals(ref)
        if approval.kind == "protected-change"
        for path, blob in approval.blobs.items()
    }
    on = ", ".join(refs)
    return [
        f"protected file {change.path} (blob {change.blob}) has no approved"
        f" protected-change approval on {on}; ask for one with"
        " vrg-ruling request --kind protected-change"
        for change in changes
        if (change.path, change.blob) not in approved
    ]


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


def _merge_review_reasons(
    repo: Path, base: str, head: str, refs: list[str], lookups: _Lookups
) -> list[str]:
    whole = change_fingerprint(repo, base, head)
    if any(lookups.covers(ref, whole) for ref in refs):
        return []
    if len(refs) == 1:
        return [
            f"merge review: no merge-review record, approved exemption or human override"
            f" on {refs[0]} covers the change (fingerprint {whole}); run merge review"
        ]
    commits = branch_commits(repo, base, head)
    reasons: list[str] = []
    claimed: set[str] = set()
    for ref in refs:
        sha, problem = _issue_commit(commits, ref)
        if sha is None:
            reasons.append(f"{problem}, or a record must cover the whole change ({whole})")
            continue
        claimed.add(sha)
        fingerprint = commit_fingerprint(repo, sha)
        if not lookups.covers(ref, fingerprint):
            reasons.append(
                f"merge review: no merge-review record, approved exemption or human override"
                f" on {ref} covers its commit {sha[:12]} (fingerprint {fingerprint}),"
                f" and none on the batch covers the whole change ({whole})"
            )
    reasons += [
        f"merge review: commit {sha[:12]} ({subject}) is no issue's own commit, so only a"
        f" record, exemption or override covering the whole change ({whole}) covers it"
        for sha, subject in commits
        if sha not in claimed
    ]
    return reasons


def evaluate(
    repo: Path, *, base: str, head: str, issue_refs: list[str], authors: TrustedAuthors
) -> GateResult:
    """Apply both rules to the change from the merge base of *base* and *head* to *head*.

    *issue_refs* are the change's task issues (``owner/repo#N``, one for a
    single-issue change, every issue of a batch in order), all in one
    repository. *base* is also the ref the protected set is read at: the
    freshly fetched ``origin/develop`` at submit and in the watch, the merge
    commit's first parent in the audit. *authors* is the trusted-authors
    configuration (:func:`~vergil_tooling.lib.trusted_authors.load_trusted_authors`).

    ``ok`` is true only when both rules pass; every reason either fails is
    given. A lookup failure fails its rule with the error as the reason, and
    a change with no usable task issue fails both, so nothing passes for
    want of an answer.
    """
    change = f"{base}...{head}"
    refs, reasons = _resolve_refs(issue_refs, change)
    if reasons:
        return GateResult(ok=False, reasons=tuple(reasons))
    lookups = _Lookups(authors)
    for rule, check in (
        ("protected files", _protected_reasons),
        ("merge review", _merge_review_reasons),
    ):
        try:
            reasons += check(repo, base, head, refs, lookups)
        except _LOOKUP_ERRORS as exc:
            reasons.append(f"{rule}: lookup failed for {change}: {exc}")
    return GateResult(ok=not reasons, reasons=tuple(reasons))
