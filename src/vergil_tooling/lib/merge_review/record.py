"""The merge-review record: a machine-readable line in merge review's closing comment.

Merge review posts one closing comment on the task issue per run, from both
of its entry points (the review before the first submit, and the review of a
fix to an open PR). That comment, by the agent login, is the one
merge-review record (epic vergil-project/.github#1384, spec §4.6). Its record
line names the head the run judged, the change fingerprint
(:mod:`vergil_tooling.lib.change_fingerprint`) and the outcome::

    Merge review record: head=<sha> fingerprint=<hex> outcome=<eligible|not-eligible (<reason>)>

Submit, the watch and the audit read the records with :func:`records`, built
on the trust layer's comment stream narrowed to the agent role.

The closing comment's lines, in order, are the eligibility line, the
``Branch as reviewed:`` line (a fix to an open PR only), the record line, and
then the rest the skill writes (the closing state of every item).
:func:`parse_record` reads a record only in that place, under an eligibility
line with the same outcome, so a record line quoted anywhere else (in a step
comment's appended reviewer reply, say) is never a record.

The record covers:

- a single issue, or a fix to an open PR (single or batch): the change from
  the merge base with ``origin/develop`` to ``HEAD``
  (:func:`~vergil_tooling.lib.change_fingerprint.change_fingerprint`);
- one issue of a batch before handoff (it has a batch marker): that issue's
  own change, from the previous issue's commit to ``HEAD``. Folded, that is
  the issue's one commit, so the fingerprint equals the folded commit's
  :func:`~vergil_tooling.lib.change_fingerprint.commit_fingerprint`.

Every failure raises :class:`RecordError` (or the fingerprint's
:class:`~vergil_tooling.lib.change_fingerprint.FingerprintError`, or the trust
layer's :class:`~vergil_tooling.lib.trust.TrustReadError` on a read); nothing
yields a record, or an empty list of records, by default.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, trust
from vergil_tooling.lib.change_fingerprint import change_fingerprint, commit_fingerprint
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.rulings import comments

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

RECORD_PREFIX = "Merge review record:"

#: The reasons a ``not-eligible`` outcome may give, fixed by the merge-review
#: skill's Eligibility section: never free text, since report-ready's notes
#: are public.
NOT_ELIGIBLE_REASONS = (
    "human-ruling",
    "too-many-blockers",
    "check-failed",
    "result-malformed",
    "fix-base-rebased",
)

#: The base a single issue, or a fix to an open PR, is reviewed against.
DEFAULT_BASE = "origin/develop"

_OUTCOME = r"eligible|not-eligible \((?:" + "|".join(map(re.escape, NOT_ELIGIBLE_REASONS)) + r")\)"
_OUTCOME_RE = re.compile(_OUTCOME)
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_RECORD_RE = re.compile(
    re.escape(RECORD_PREFIX)
    + r" head=(?P<head>[0-9a-f]{40}) fingerprint=(?P<fingerprint>[0-9a-f]{64})"
    + r" outcome=(?P<outcome>"
    + _OUTCOME
    + r")"
)
#: The closing comment's first line, as the merge-review skill writes it.
_ELIGIBILITY_RE = re.compile(
    r"Merge review: done: (?P<outcome>" + _OUTCOME + r"); accepted-risk (?:0|[1-9][0-9]*)"
)
_BRANCH_RE = re.compile(r"Branch as reviewed: (?:yes|no)")

#: A batch commit's trailing issue ref, `` (#N)``.
_SUBJECT_REF_RE = re.compile(r" \(#([1-9][0-9]*)\)$")
FIXUP_PREFIX = "fixup! "

#: The largest comment body GitHub accepts.
MAX_BODY = 65536


class RecordError(Exception):
    """A record could not be rendered, a closing comment built, or a change found."""


@dataclass(frozen=True)
class Record:
    """One merge-review record: the head judged, its change fingerprint and the outcome.

    ``comment_id`` and ``author`` are the closing comment's, set by
    :func:`records`; a record parsed from a bare body has ``0`` and ``""``.
    """

    head: str
    fingerprint: str
    outcome: str
    comment_id: int = 0
    author: str = ""


def check_outcome(outcome: str) -> str:
    """*outcome* when it is a complete run's: ``eligible`` or ``not-eligible (<reason>)``.

    Stopped, waiting, fatal and error are not outcomes: such a run did not
    complete and has no record.
    """
    if _OUTCOME_RE.fullmatch(outcome) is None:
        reasons = ", ".join(NOT_ELIGIBLE_REASONS)
        msg = (
            f"the outcome must be eligible or not-eligible (<reason>), the reason one of"
            f" {reasons}; got {outcome!r}. A run that stopped, is waiting, or ended fatal"
            " or in error has no record"
        )
        raise RecordError(msg)
    return outcome


def render_record(head: str, fingerprint: str, outcome: str) -> str:
    """The record line for *head*, its change *fingerprint* and the run's *outcome*."""
    if _SHA_RE.fullmatch(head) is None:
        msg = f"the head must be a full 40-character commit SHA, got {head!r}"
        raise RecordError(msg)
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        msg = f"the fingerprint must be 64 lowercase hex characters, got {fingerprint!r}"
        raise RecordError(msg)
    check_outcome(outcome)
    return f"{RECORD_PREFIX} head={head} fingerprint={fingerprint} outcome={outcome}"


def _record_index(lines: list[str]) -> int:
    """Where the record line goes: after the eligibility line and any branch line."""
    return 2 if len(lines) > 1 and _BRANCH_RE.fullmatch(lines[1]) else 1


def parse_record(body: str) -> Record | None:
    """The record a closing comment's *body* carries, or ``None`` when it carries none.

    A record counts only in its place: the body's first line is an
    eligibility line, the record line follows it (or follows the
    ``Branch as reviewed:`` line right below it), its outcome is the
    eligibility line's, and no other line of the body starts with the
    record prefix. Anything else, a malformed record line included, is no
    record.
    """
    lines = body.split("\n")
    eligibility = _ELIGIBILITY_RE.fullmatch(lines[0])
    if eligibility is None:
        return None
    index = _record_index(lines)
    if index >= len(lines):
        return None
    match = _RECORD_RE.fullmatch(lines[index])
    if match is None or match["outcome"] != eligibility["outcome"]:
        return None
    if sum(line.startswith(RECORD_PREFIX) for line in lines) != 1:
        return None
    return Record(head=match["head"], fingerprint=match["fingerprint"], outcome=match["outcome"])


def records(issue_ref: str, *, authors: TrustedAuthors) -> list[Record]:
    """Every merge-review record on *issue_ref*, oldest first.

    Read from the trust layer's comment stream narrowed to the agent role,
    so only a comment the agent login posted, with no edit by anyone, can be
    a record. A read failure raises
    (:class:`~vergil_tooling.lib.trust.TrustReadError`); it never returns
    ``[]``.
    """
    repo, issue = comments.parse_issue_ref(issue_ref)
    stream = comments.read_stream(repo, issue, authors, roles=frozenset({trust.Role.AGENTS}))
    found: list[Record] = []
    for comment in stream.comments:
        if comment.edited:
            continue
        record = parse_record(comment.body)
        if record is not None:
            found.append(
                Record(
                    head=record.head,
                    fingerprint=record.fingerprint,
                    outcome=record.outcome,
                    comment_id=comment.id,
                    author=comment.author,
                )
            )
    return found


def closing_body(body: str, outcome: str, record_line: str) -> str:
    """The closing comment *body* with *record_line* in its place.

    *body* is the closing comment as the merge-review skill writes it: the
    eligibility line for *outcome* first, then the ``Branch as reviewed:``
    line on a fix to an open PR, then the rest. Every line is kept, and the
    record line goes right below the first one or two. A body with no such
    eligibility line, an outcome other than *outcome*, or a record line of
    its own is refused.
    """
    check_outcome(outcome)
    lines = body.split("\n")
    eligibility = _ELIGIBILITY_RE.fullmatch(lines[0])
    if eligibility is None:
        msg = (
            "the body's first line must be the eligibility line,"
            " 'Merge review: done: <outcome>; accepted-risk <w>'"
        )
        raise RecordError(msg)
    if eligibility["outcome"] != outcome:
        msg = (
            f"the body's eligibility line gives the outcome {eligibility['outcome']!r},"
            f" not --outcome {outcome!r}"
        )
        raise RecordError(msg)
    if any(line.startswith(RECORD_PREFIX) for line in lines):
        msg = f"the body already has a line starting {RECORD_PREFIX!r}; the record line is computed"
        raise RecordError(msg)
    index = _record_index(lines)
    text = "\n".join([*lines[:index], record_line, *lines[index:]])
    if len(text.encode("utf-8")) > MAX_BODY:
        msg = f"the closing comment is over {MAX_BODY} bytes; nothing was posted"
        raise RecordError(msg)
    return text


@dataclass(frozen=True)
class Covered:
    """The change a record covers: the head judged and its fingerprint."""

    head: str
    fingerprint: str


def branch_commits(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    """``(sha, subject)`` of each commit of ``base..head``, oldest first; refuse a merge."""
    try:
        raw = git.read_output(
            "-C",
            str(repo),
            "rev-list",
            "--reverse",
            "--no-commit-header",
            "--format=%H%x1f%P%x1f%s",
            f"{base}..{head}",
            "--",
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        msg = f"cannot read the commits {base[:12]}..{head[:12]}: {exc}"
        raise RecordError(msg) from exc
    found: list[tuple[str, str]] = []
    for line in raw.splitlines():
        fields = line.split("\x1f", 2)
        if len(fields) != 3:  # noqa: PLR2004 - sha, parents, subject
            msg = f"cannot parse the git rev-list line {line!r}"
            raise RecordError(msg)
        sha, parents, subject = fields
        if len(parents.split()) > 1:
            msg = f"commit {sha[:12]} is a merge commit: a batch branch is linear"
            raise RecordError(msg)
        found.append((sha, subject))
    return found


def subject_names(subject: str, number: int) -> bool:
    """Whether a batch commit *subject* ends in the issue ref `` (#<number>)``."""
    match = _SUBJECT_REF_RE.search(subject)
    return match is not None and int(match.group(1)) == number


def _previous_commit(repo: Path, marker: batch_marker.Marker, issue: str, head: str) -> str:
    """The batch issue *issue*'s fold base: ``base_sha``, or the previous issue's one commit."""
    position = marker.issues.index(issue)
    if position == 0:
        return marker.base_sha
    previous = int(marker.issues[position - 1].removeprefix("#"))
    matches = [
        sha
        for sha, subject in branch_commits(repo, marker.base_sha, head)
        if subject_names(subject, previous) and not subject.startswith(FIXUP_PREFIX)
    ]
    if len(matches) != 1:
        msg = (
            f"the batch branch must hold exactly one commit for the previous issue"
            f" #{previous} since {marker.base_sha[:12]}; found {len(matches)}"
        )
        raise RecordError(msg)
    return matches[0]


def batch_member_change(repo: Path, marker: batch_marker.Marker, issue: str, head: str) -> Covered:
    """What a record covers for batch issue *issue* (``#N``) at *head*, before handoff.

    The issue's commits are those of ``<previous issue's commit>..<head>``:
    its first commit, whose subject ends in `` (#N)``, and only
    ``fixup!`` commits of it after. Any other commit there (a later issue's,
    a merge) is refused. The fingerprint is the change since the previous
    issue's commit, which is the folded commit's ``commit_fingerprint``.
    """
    number = int(issue.removeprefix("#"))
    previous = _previous_commit(repo, marker, issue, head)
    own = branch_commits(repo, previous, head)
    if not own:
        msg = f"the batch branch has no commit for {issue} after {previous[:12]}"
        raise RecordError(msg)
    first_sha, first_subject = own[0]
    if first_subject.startswith(FIXUP_PREFIX) or not subject_names(first_subject, number):
        msg = (
            f"the first commit after {previous[:12]}, {first_sha[:12]} ({first_subject}),"
            f" is not {issue}'s first commit: its subject must end in ' ({issue})'"
        )
        raise RecordError(msg)
    for sha, subject in own[1:]:
        if subject != f"{FIXUP_PREFIX}{first_subject}":
            msg = (
                f"commit {sha[:12]} ({subject}) is not a fixup of {issue}'s first commit;"
                f" {issue} must be the last issue on the branch"
            )
            raise RecordError(msg)
    if len(own) == 1:
        return Covered(head=head, fingerprint=commit_fingerprint(repo, head))
    return Covered(head=head, fingerprint=change_fingerprint(repo, previous, head))


def covered_change(repo: Path, issue_number: int, *, main_root: Path) -> Covered:
    """What a record posted now from the worktree *repo* covers, for task *issue_number*.

    A batch member (its batch marker, under *main_root*, exists) gets
    :func:`batch_member_change`; any other task, or a fix to an open PR
    (report-ready has removed the markers), the change from the merge base
    with ``origin/develop`` to ``HEAD``.
    """
    try:
        head = git.read_output("-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}")
    except subprocess.CalledProcessError as exc:
        msg = f"cannot read HEAD in {repo}"
        raise RecordError(msg) from exc
    issue = f"#{issue_number}"
    try:
        marker = batch_marker.read_marker(main_root, issue)
    except batch_marker.BatchMarkerError as exc:
        raise RecordError(str(exc)) from exc
    if marker is not None:
        return batch_member_change(repo, marker, issue, head)
    return Covered(head=head, fingerprint=change_fingerprint(repo, DEFAULT_BASE, head))
