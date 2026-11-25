"""Security records: private draft advisories paired with neutral issues.

Every security finding gets one **pair** (epic vergil-project/.github#517):

- a **draft repository security advisory** in ``<org>/.github`` that holds the
  details, with one ``Tracking issue: <owner>/<repo>#<N>`` line per issue it
  tracks, and the affected repo as its ``other``-ecosystem package name;
- a neutral ``security``-labelled **issue** carrying the line
  ``Security advisory: <org>/.github <GHSA-id>``.

One issue may name several advisories, one line each (epic
vergil-project/.github#573); it keeps the ``security`` label while any line
remains.

This module owns the advisories REST API, the two reference lines, pair
classification, :func:`pair`, an advisory's first link, :func:`retarget`, the
one way a link moves (only from an issue the advisory tracks), and
:func:`close_record`, which closes an advisory with a public reason, and
:func:`note_record`, which appends a dated note to an advisory's description.

A retarget whose targets are all in another org **moves** the advisory there
(epic vergil-project/.github#1417, spec 4.6): GitHub cannot move one, so the
copy carries ``Moved from: <GHSA-id>`` and the closed original carries
``Moved to: <org>/.github <GHSA-id>``, a terminal state the sync recognises.

**Output rule.** An advisory's ``summary`` and ``description`` reach a caller
only through :func:`read_text`. :class:`Advisory` has no text field, the list
reads project the text away with ``--jq`` before it reaches stdout, and every
advisory helper re-raises a ``gh`` failure **without its stdout**, so no error
message can echo advisory text. Everything else names only GHSA IDs, issue
refs, labels and states.

**Trust rule** (epic vergil-project/.github#1467). An issue's ``Security
advisory:`` lines are read only from a body the trust layer trusts
(:mod:`~vergil_tooling.lib.trust`); the REST issue reads never take the body.
Where a body decides or receives a write (:func:`mark_issue`,
:func:`unmark_issue`, :func:`retarget`) an untrusted one is a stop before
any write; in the sync (:func:`records_pass`) it is reported, metadata only,
and never acted on. :func:`close_record` reads no body: its issues are the
advisory's own ``Tracking issue:`` lines.

**Known targets, no scan** (vergil-tooling#5138). A write whose issues are
known reads only those: :func:`close_record` the tracked issues, and
:func:`retarget` the tracked issues, the source and the targets, and
:func:`sync_issues` (the advisory sync ``vrg-finalize-pr`` runs on the issues
a merged PR closed, vergil-tooling#4583) those issues and the issues their
advisories track. Only the
sync, whose job is finding broken pairs, and a retarget re-run that can no
longer learn its dropped issues from the tracking lines, list the org's
``security`` issues (:func:`security_issues`), whose bodies are read in
batches, bodies only.

The advisory create is sent **exactly once** (:func:`github.post_json_once`):
drafts cannot be deleted, so a retried create could leave a second record.
"""

from __future__ import annotations

import contextlib
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from vergil_tooling.lib import epics, github, labels, trust, trusted_comments
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.shell_ref import quoted_slug
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from datetime import date

SECURITY_LABEL = "security"
DISCLOSED_LABEL = "security-disclosed"
SEVERITIES = ("critical", "high", "medium", "low")

_GHSA = r"GHSA(?:-[0-9a-z]{4}){3}"
_NAME = r"[A-Za-z0-9._-]+"

# Each reference line is exact and alone on its line; anything else (a quoted
# line, a mention mid-sentence, a repo other than .github) is not a reference.
_ADVISORY_RE = re.compile(rf"^Security advisory: ({_NAME})/\.github ({_GHSA})[ \t]*\r?$", re.M)
_TRACKING_RE = re.compile(rf"^Tracking issue: ({_NAME})/({_NAME})#([0-9]+)[ \t]*\r?$", re.M)
# The two lines of a cross-org move: the copy names its original and the
# original names its copy, each exact and alone on its line.
_MOVED_FROM_RE = re.compile(rf"^Moved from: ({_GHSA})[ \t]*\r?$", re.M)
_MOVED_TO_RE = re.compile(rf"^Moved to: ({_NAME})/\.github ({_GHSA})[ \t]*\r?$", re.M)

# The same tracking-line test, applied by gh's jq to each page so only the
# reference lines (never the rest of the description) reach stdout.
_JQ_TRACKING_TEST = r"^Tracking issue: [A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[0-9]+[ \t]*$"
_JQ_MOVED_FROM_TEST = r"^Moved from: GHSA(-[0-9a-z]{4}){3}[ \t]*$"
_JQ_MOVED_TO_TEST = r"^Moved to: [A-Za-z0-9._-]+/[.]github GHSA(-[0-9a-z]{4}){3}[ \t]*$"
_LIST_JQ = (
    '[.[] | [(.description // "") | split("\\n")[] | rtrimstr("\\r")] as $lines'
    " | {ghsa_id, state,"
    " affected: ((.vulnerabilities // []) | map(.package.name // empty) | first),"
    " submission: (.submission != null),"
    f' tracking: [$lines[] | select(test("{_JQ_TRACKING_TEST}"))],'
    f' moved_from: [$lines[] | select(test("{_JQ_MOVED_FROM_TEST}"))],'
    f' moved_to: [$lines[] | select(test("{_JQ_MOVED_TO_TEST}"))]}}]'
)
_DUPLICATE_JQ = (
    "[.[] | {ghsa_id, summary, names: [(.vulnerabilities // [])[] | .package.name // empty]}]"
)
# The REST issue reads take no body: the body, whose ``Security advisory:``
# lines drive the sync, retarget and close, is read only through the trust
# layer (:func:`_judged_body`, :func:`security_issues`).
_ISSUE_JQ = "{number, repository_url, state, labels: [.labels[].name]}"
_SECURITY_ISSUES_JQ = "[.[] | select(.pull_request == null) | {number, state, state_reason}]"
# The one close reason that lets the sync close an advisory (spec 6.2).
_COMPLETED = "completed"


class RetargetError(RuntimeError):
    """A :func:`retarget` step failed; the message names the GHSA ID and the step."""


class RetargetRefusedError(RetargetError):
    """:func:`retarget`, :func:`pair` or :func:`pair_check` refused before any write.

    The message names the advisory, the issues it tracks and, where one
    exists, the command that would succeed. A caller tells it apart from a
    failed step, after which a re-run with the same arguments finishes.
    """


class CloseError(RuntimeError):
    """A :func:`close_record` refusal or failed step; the message names IDs and refs only."""


@dataclass(frozen=True)
class Advisory:
    """One advisory as the list reads see it: IDs and states, never its text.

    It deliberately has **no** ``summary`` or ``description`` field, so a
    caller holding an ``Advisory`` cannot print the text by mistake.
    """

    ghsa_id: str
    state: str  # triage | draft | published | closed | withdrawn
    affected: str | None  # the first ``other``-ecosystem package name
    tracking: tuple[IssueRef, ...]
    external: bool  # came from private vulnerability reporting
    # The GHSA IDs its exact ``Moved from:`` lines name (a copy a move made).
    moved_from: tuple[str, ...] = ()
    # The ``(org, GHSA ID)`` its exact ``Moved to:`` lines name (a moved original).
    moved_to: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SecurityIssue:
    """One ``security``-labelled issue, open or closed, and every advisory it names.

    ``ghsa_ids`` is in body order and empty when the body has no line.
    ``state_reason`` is GitHub's close reason, lower-cased (``completed``,
    ``not_planned``, ``duplicate`` or any reason GitHub adds), None while the
    issue is open or when GitHub records none. ``skipped`` is the trust
    layer's record of an untrusted body (metadata only), None for a trusted
    one; an untrusted body's lines are never read, so its ``ghsa_ids`` is
    empty.
    """

    ref: IssueRef
    open: bool
    ghsa_ids: tuple[str, ...]
    state_reason: str | None
    skipped: trust.Skipped | None = None


@dataclass(frozen=True)
class GuardResult:
    """The sync close guard's verdict (:func:`guard_check`), by GHSA ID and issue ref only.

    ``held`` pairs each advisory the guard kept from ``closed`` with the
    tracked issues that fired it; ``errors`` names each advisory whose guard
    could not be decided, the issue whose read failed, and the scrubbed error.
    """

    held: list[tuple[str, tuple[IssueRef, ...]]]
    errors: list[tuple[str, IssueRef, str]]


@dataclass(frozen=True)
class Classification:
    """The pair check's verdict: consistent pairs, external reports, broken pairs, moves.

    ``broken`` holds one-line reasons naming only GHSA IDs and issue refs. An
    advisory with any broken tracking line is reported there and left out of
    ``paired``, so nothing acts on a pair whose links are wrong. ``moved``
    holds each advisory with exactly one ``Moved to:`` line: a terminal
    state, never synced and never broken for tracking nothing
    (:func:`moved_problems` checks the copy it names). ``untrusted`` holds
    each advisory that tracks a ``security`` issue whose body the trust layer
    does not trust: its pair cannot be checked, so it is reported and never
    synced.
    """

    paired: list[Advisory]
    external: list[Advisory]
    broken: list[str]
    moved: list[Advisory] = field(default_factory=list)
    untrusted: list[Advisory] = field(default_factory=list)


def _scrubbed(exc: github.GitHubAPIError) -> github.GitHubAPIError:
    """*exc* without its stdout: the exit status, the command and stderr only."""
    cls = (
        github.MissingGitHubTokenError
        if isinstance(exc, github.MissingGitHubTokenError)
        else github.GitHubAPIError
    )
    return cls(exc.returncode, exc.cmd, None, exc.stderr)


@contextlib.contextmanager
def _no_text() -> Generator[None]:
    """Re-raise any ``gh`` failure without its stdout, which may hold advisory text."""
    try:
        yield
    except github.GitHubAPIError as exc:
        raise _scrubbed(exc) from None


def _advisories(home: str) -> str:
    return f"repos/{home}/security-advisories"


def _issue_endpoint(ref: IssueRef) -> str:
    return f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"


def advisory_home(org: str) -> str:
    """Return ``"<org>/.github"``, the advisory home."""
    return f"{org}/.github"


def advisory_line(home: str, ghsa_id: str) -> str:
    """The issue-body line naming an advisory: ``Security advisory: <home> <GHSA>``."""
    return f"Security advisory: {home} {ghsa_id}"


def tracking_line(ref: IssueRef) -> str:
    """The advisory-description line naming a tracked issue."""
    return f"Tracking issue: {ref.slug}"


def moved_from_line(ghsa_id: str) -> str:
    """The line a moved advisory's copy carries: ``Moved from: <original GHSA>``."""
    return f"Moved from: {ghsa_id}"


def moved_to_line(home: str, ghsa_id: str) -> str:
    """The line a moved original carries: ``Moved to: <home> <copy GHSA>``."""
    return f"Moved to: {home} {ghsa_id}"


def parse_moved_from(description: str) -> tuple[str, ...]:
    """Every GHSA ID the exact ``Moved from:`` lines name, in order, without repeats."""
    return tuple(dict.fromkeys(m.group(1) for m in _MOVED_FROM_RE.finditer(description)))


def parse_moved_to(description: str) -> tuple[tuple[str, str], ...]:
    """Every ``(org, GHSA ID)`` the exact ``Moved to:`` lines name, in order, without repeats."""
    pairs = ((m.group(1), m.group(2)) for m in _MOVED_TO_RE.finditer(description))
    return tuple(dict.fromkeys(pairs))


def parse_advisory_refs(issue_body: str) -> tuple[str, ...]:
    """Every GHSA ID the exact ``Security advisory:`` lines name, in order, without repeats."""
    return tuple(dict.fromkeys(match.group(2) for match in _ADVISORY_RE.finditer(issue_body)))


def parse_advisory_lines(issue_body: str) -> tuple[tuple[str, str], ...]:
    """Every ``(org, GHSA ID)`` the exact ``Security advisory:`` lines name, in order, no repeats.

    :func:`parse_advisory_refs` keeps the GHSA IDs only; this keeps the org of
    each line's ``<org>/.github`` too, for a caller that checks it.
    """
    pairs = ((match.group(1), match.group(2)) for match in _ADVISORY_RE.finditer(issue_body))
    return tuple(dict.fromkeys(pairs))


def parse_tracking(description: str) -> tuple[IssueRef, ...]:
    """Every issue the exact ``Tracking issue:`` lines name, in order, without repeats."""
    refs = (
        IssueRef(match.group(1), match.group(2), int(match.group(3)))
        for match in _TRACKING_RE.finditer(description)
    )
    return tuple(dict.fromkeys(refs))


def create_advisory(
    home: str, *, summary: str, details: str, affected_repo: str, severity: str
) -> str:
    """Create a draft advisory in *home* and return its GHSA ID.

    The POST is sent exactly once. It never publishes, never requests a CVE
    and never asks for a private fork. On failure the caller decides (the
    duplicate guard, :func:`find_duplicate`, covers a create whose response
    was lost).
    """
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}: {severity!r}")
    body: dict[str, object] = {
        "summary": summary,
        "description": details,
        "severity": severity,
        "vulnerabilities": [{"package": {"ecosystem": "other", "name": _bare(affected_repo)}}],
    }
    with _no_text():
        payload = github.post_json_once(_advisories(home), body)
    ghsa_id = payload.get("ghsa_id")
    if not isinstance(ghsa_id, str) or not ghsa_id:
        raise RuntimeError(f"advisory create in {home} returned no ghsa_id")
    return ghsa_id


def _bare(repo: str) -> str:
    """The bare name of ``owner/name`` (a bare name is returned unchanged)."""
    return repo.rsplit("/", 1)[-1]


def find_duplicate(home: str, *, summary: str, affected_repo: str) -> str | None:
    """The GHSA ID of a draft in *home* with this *summary* and affected repo, or None.

    The summaries are compared here and never returned or printed.
    """
    with _no_text():
        rows = github.read_json_paginated(f"{_advisories(home)}?state=draft", jq=_DUPLICATE_JQ)
    bare = _bare(affected_repo)
    for row in rows:
        item: Any = row
        if item.get("summary") == summary and bare in (item.get("names") or []):
            return str(item["ghsa_id"])
    return None


def _advisory_from_row(row: Any) -> Advisory:
    ghsa_id, state = row.get("ghsa_id"), row.get("state")
    if not isinstance(ghsa_id, str) or not isinstance(state, str):
        raise ValueError("advisory list row lacks a ghsa_id or state")
    affected = row.get("affected")

    def joined(key: str) -> str:
        return "\n".join(str(line) for line in row.get(key) or [])

    return Advisory(
        ghsa_id=ghsa_id,
        state=state,
        affected=affected if isinstance(affected, str) else None,
        tracking=parse_tracking(joined("tracking")),
        external=state == "triage" or bool(row.get("submission")),
        moved_from=parse_moved_from(joined("moved_from")),
        moved_to=parse_moved_to(joined("moved_to")),
    )


def list_advisories(home: str) -> list[Advisory]:
    """Every advisory in *home*, in every state, across every page.

    The fetch projects each page to the GHSA ID, state, package name, whether
    a ``submission`` exists and the ``Tracking issue:``, ``Moved from:`` and
    ``Moved to:`` lines, so no other summary or
    description text reaches stdout.
    """
    with _no_text():
        rows = github.read_json_paginated(_advisories(home), jq=_LIST_JQ)
    return [_advisory_from_row(row) for row in rows]


def read_text(home: str, ghsa_id: str) -> tuple[str, str]:
    """Return an advisory's ``(summary, description)``.

    The only path to an advisory's text; for ``show``, ``list --details``,
    :func:`retarget`'s rewrite and :func:`note_record`'s append, never for
    anything that prints to a log.
    """
    with _no_text():
        raw: Any = github.read_json(
            "api", f"{_advisories(home)}/{ghsa_id}", "--jq", "{summary, description}"
        )
    summary, description = raw.get("summary"), raw.get("description") or ""
    if not isinstance(summary, str) or not isinstance(description, str):
        raise ValueError(f"advisory {ghsa_id} read returned no summary or description")
    return summary, description


def _write_description(home: str, ghsa_id: str, description: str) -> None:
    with _no_text():
        github.write_json("PATCH", f"{_advisories(home)}/{ghsa_id}", {"description": description})


def set_state(home: str, ghsa_id: str, state: Literal["draft", "closed"]) -> None:
    """Set an advisory's state to ``draft`` or ``closed`` (never ``published``)."""
    if state not in ("draft", "closed"):
        raise ValueError(f"advisory state must be draft or closed: {state!r}")
    with _no_text():
        github.write_json("PATCH", f"{_advisories(home)}/{ghsa_id}", {"state": state})


def _issue_state(ref: IssueRef) -> tuple[IssueRef, set[str], str]:
    """*ref*'s current ref, labels and trusted body.

    The current ref differs from *ref* only when the issue was transferred:
    the read at its old number follows GitHub's redirect to the moved issue,
    while writes and sub-resource reads at the old number fail with a 404.
    The body is read at the current ref through the trust layer
    (:func:`_judged_body`): every caller writes from it or decides a write
    by it, so an untrusted body is a stop.
    """
    raw = _read_issue(ref)
    labels_ = {str(name) for name in raw.get("labels") or []}
    current = _current_ref(raw)
    return current, labels_, _judged_body(current)


def _judged_body(ref: IssueRef) -> str:
    """*ref*'s body, read through the trust layer; an untrusted body is a stop.

    Raises :class:`~vergil_tooling.lib.trust.UntrustedContentError` (metadata
    only) when its author or an editor holds no role, and
    :class:`~vergil_tooling.lib.trust.TrustReadError` when who wrote it cannot
    be read in full. Nothing falls back to an untrusted copy.
    """
    content = trust.read_issue(f"{ref.owner}/{ref.repo}", ref.number, require_body=True)
    return cast("str", content.body)


def _read_issue(ref: IssueRef) -> Any:
    """*ref*'s REST number, repo, state and labels (``_ISSUE_JQ``); it follows a transfer.

    It never reads the body: :func:`_issue_state` reads that through the trust layer.
    """
    return github.read_json("api", _issue_endpoint(ref), "--jq", _ISSUE_JQ)


def _current_ref(raw: Any) -> IssueRef:
    """The ref an issue read (:func:`_read_issue`) actually returned."""
    owner, repo = str(raw["repository_url"]).rstrip("/").split("/")[-2:]
    return IssueRef(owner=owner, repo=repo, number=int(raw["number"]))


def _issue_labels_body(ref: IssueRef) -> tuple[set[str], str]:
    _current, present, body = _issue_state(ref)
    return present, body


def _append_line(text: str, line: str) -> str:
    stripped = text.rstrip()
    return f"{stripped}\n\n{line}" if stripped else line


def mark_issue(ref: IssueRef, home: str, ghsa_id: str) -> None:
    """Give *ref* the ``security`` label and the advisory line. Idempotent.

    The body it appends the line to, and writes back, is the trusted body
    (:func:`_issue_state`), read before any write: an untrusted one raises
    :class:`~vergil_tooling.lib.trust.UntrustedContentError` and nothing is
    written, so automation never re-writes text it does not trust.
    """
    present, body = _issue_labels_body(ref)
    labels.ensure_labels(f"{ref.owner}/{ref.repo}", [SECURITY_LABEL])
    if SECURITY_LABEL not in present:
        github.write_json("POST", f"{_issue_endpoint(ref)}/labels", {"labels": [SECURITY_LABEL]})
    line = advisory_line(home, ghsa_id)
    if line not in (candidate.rstrip() for candidate in body.splitlines()):
        github.write_json("PATCH", _issue_endpoint(ref), {"body": _append_line(body, line)})


def _moved_comment(moved_to: Sequence[IssueRef]) -> str:
    return f"Security record moved to {', '.join(ref.slug for ref in moved_to)}"


def unmark_issue(ref: IssueRef, ghsa_id: str, moved_to: Sequence[IssueRef]) -> str | None:
    """Drop *ref*'s line naming *ghsa_id*; say where it moved.

    An issue may name several advisories (:func:`parse_advisory_refs`). If no
    line of *ref*'s body names *ghsa_id* — it names only others, or none — the
    issue is left untouched: no body edit, no label change, no comment.
    Otherwise only the line naming *ghsa_id* is removed, and the ``security``
    label goes too only when no advisory line is left after the removal.

    The comment goes first and the line and label then go in **one** PATCH, so
    an interrupted unmark never leaves the issue with only one of the two: it
    either still carries both (and :func:`security_issues` still finds it
    naming this advisory, so a re-run of :func:`retarget` finishes it) or
    neither. That makes it idempotent: once the PATCH has landed the line is
    gone and a re-run does nothing, so the comment is never posted twice. The
    comment counts as posted only when a trusted author's copy of it is on the
    issue (:func:`trusted_comments.bodies`): an outsider's never stops it.
    The body it reads the line from, and writes back without it, is the
    trusted body (:func:`_issue_state`): an untrusted one raises
    :class:`~vergil_tooling.lib.trust.UntrustedContentError` before any write.

    A *ref* that was **transferred** (its read leads to an issue under another
    ref) is never written at its old number. If the issue it moved to is in
    *moved_to*, nothing is left to unmark: that issue is a target itself, and
    unmarking it would undo the retarget. Otherwise the unmark follows the
    transfer and acts on the issue's current ref. Either way it returns a
    one-line note, naming only refs, that says which it did; it returns
    ``None`` for an issue that was not transferred.
    """
    if not moved_to:
        raise ValueError(f"unmark {ref.slug}: no issue the record moved to")
    current, present, body = _issue_state(ref)
    if current == ref:
        _unmark_current(ref, present, body, ghsa_id, moved_to)
        return None
    if current in moved_to:
        return (
            f"{ref.slug} was transferred to {current.slug}, a target: "
            f"nothing left to unmark at {ref.slug}"
        )
    if _unmark_current(current, present, body, ghsa_id, moved_to):
        outcome = f"unmarked {current.slug}"
    else:
        outcome = f"{current.slug} has no line for this advisory, nothing to unmark"
    return f"{ref.slug} was transferred to {current.slug}: followed the transfer and {outcome}"


def _unmark_current(
    ref: IssueRef, present: set[str], body: str, ghsa_id: str, moved_to: Sequence[IssueRef]
) -> bool:
    """:func:`unmark_issue` at the issue's current *ref*; ``True`` if it named *ghsa_id*."""
    if ghsa_id not in parse_advisory_refs(body):
        return False
    endpoint = _issue_endpoint(ref)
    comment = _moved_comment(moved_to)
    existing = trusted_comments.bodies(ref)
    if comment not in existing:
        github.write_json("POST", f"{endpoint}/comments", {"body": comment})
    this_line = re.compile(
        rf"^Security advisory: {_NAME}/\.github {re.escape(ghsa_id)}[ \t]*\r?$", re.M
    )
    remaining = re.sub(r"\n{3,}", "\n\n", this_line.sub("", body)).rstrip()
    update: dict[str, object] = {"body": remaining}
    if SECURITY_LABEL in present and not parse_advisory_refs(remaining):
        update["labels"] = sorted(present - {SECURITY_LABEL})
    github.write_json("PATCH", endpoint, update)
    return True


def _without_tracking(description: str) -> str:
    """*description* with every ``Tracking issue:`` line and the gap it leaves removed."""
    return re.sub(r"\n{3,}", "\n\n", _TRACKING_RE.sub("", description)).rstrip()


def _with_tracking(description: str, targets: Sequence[IssueRef]) -> str:
    lines = "\n".join(tracking_line(ref) for ref in targets)
    return _append_line(_without_tracking(description), lines)


def _before_tracking(description: str, block: str) -> str:
    """*description* with *block* appended, its ``Tracking issue:`` lines kept last in order."""
    text = _append_line(_without_tracking(description), block)
    tracking = parse_tracking(description)
    if tracking:
        text += "\n\n" + "\n".join(tracking_line(ref) for ref in tracking)
    return text


@contextlib.contextmanager
def _step(ghsa_id: str, step: str, error: type[RuntimeError] = RetargetError) -> Generator[None]:
    """Turn any failure inside one step into *error*, a :class:`RetargetError` by default.

    The message names the advisory and the step; a ``gh`` failure is scrubbed
    of its stdout first, both in the message and in the chained cause.
    """
    try:
        yield
    except github.GitHubAPIError as exc:
        clean = _scrubbed(exc)
        raise error(f"advisory {ghsa_id}: {step} failed: {clean}") from clean
    except Exception as exc:
        raise error(f"advisory {ghsa_id}: {step} failed: {exc}") from exc


def retarget_command(
    ghsa_id: str,
    source: IssueRef,
    targets: Sequence[IssueRef],
    drop: Sequence[IssueRef] = (),
) -> str:
    """The paste-ready ``vrg-security-record retarget`` command for this move.

    *drop* is only ever the ``--drop`` set the human already gave: no tool
    output suggests leaving an open issue untracked.
    """
    words = [f"vrg-security-record retarget {ghsa_id} --from {quoted_slug(source)}"]
    words += [f"--to {quoted_slug(ref)}" for ref in targets]
    words += [f"--drop {quoted_slug(ref)}" for ref in drop]
    return " ".join(words)


def _slugs(refs: Sequence[IssueRef]) -> str:
    return ", ".join(ref.slug for ref in refs) or "no issue"


def _moved_targets(comments: Sequence[str]) -> list[set[str]]:
    """The target sets named by every :func:`_moved_comment` among *comments*."""
    prefix = "Security record moved to "
    return [
        {slug.strip() for slug in text[len(prefix) :].split(",")}
        for text in (comment.strip() for comment in comments)
        if text.startswith(prefix)
    ]


def _source_tied_to_move(source: IssueRef, ghsa_id: str, wanted: Sequence[IssueRef]) -> bool:
    """Whether *source* is the source of a move to *wanted* that already rewrote the lines.

    Either its body still names *ghsa_id* (a run interrupted before its
    unmark finished) or it carries the comment :func:`unmark_issue` posts,
    naming exactly *wanted* (a run that finished), from a trusted author
    (:func:`trusted_comments.bodies`): an outsider's copy of that comment
    never ties a source to a move. It is read at its current ref, so a
    transferred source is judged where it now is, and its body only through
    the trust layer (:func:`_issue_state`): an untrusted body is a stop,
    since a tie lets the move write to the source.
    """
    current, _present, body = _issue_state(source)
    if ghsa_id in parse_advisory_refs(body):
        return True
    names = {ref.slug for ref in wanted}
    return names in _moved_targets(trusted_comments.bodies(current))


def _check_source(
    ghsa_id: str,
    source: IssueRef,
    previous: Sequence[IssueRef],
    wanted: Sequence[IssueRef],
) -> None:
    """Refuse a *source* the advisory does not track and that is not tied to this move."""
    if source in previous:
        return
    if previous and set(previous) == set(wanted):
        with _step(ghsa_id, f"reading {source.slug}"):
            tied = _source_tied_to_move(source, ghsa_id, wanted)
        if tied:
            return
    if not previous:
        raise RetargetRefusedError(
            f"advisory {ghsa_id}: --from {source.slug} is not an issue it tracks, and it "
            "tracks no issue, so there is nothing to move; make its first pair with "
            "vrg-security-record create --resume"
        )
    raise RetargetRefusedError(
        f"advisory {ghsa_id}: --from {source.slug} is not an issue it tracks (it tracks "
        f"{_slugs(previous)}); run: {retarget_command(ghsa_id, previous[0], wanted)}"
    )


def _open_issues(
    ghsa_id: str, refs: Sequence[IssueRef], listed: dict[IssueRef, SecurityIssue]
) -> list[IssueRef]:
    """Every issue of *refs* that is open; one missing from *listed* is read directly."""
    still_open: list[IssueRef] = []
    for ref in refs:
        issue = listed.get(ref)
        if issue is not None:
            is_open = issue.open
        else:
            with _step(ghsa_id, f"reading the state of {ref.slug}"):
                is_open = str(_read_issue(ref).get("state") or "").lower() != "closed"
        if is_open:
            still_open.append(ref)
    return still_open


def _check_drops(  # noqa: PLR0913 - each input is one part of the one check
    ghsa_id: str,
    source: IssueRef,
    wanted: Sequence[IssueRef],
    others: Sequence[IssueRef],
    drop: Sequence[IssueRef],
    listed: dict[IssueRef, SecurityIssue],
) -> None:
    """Refuse to stop tracking an open issue of *others* that *drop* does not name.

    *others* is the dropped set without the source. The refusal's command
    keeps every open dropped issue and never names the flag that leaves one:
    leaving an open issue untracked is a human decision, so no tool output
    suggests it.
    """
    still_open = _open_issues(ghsa_id, others, listed)
    refused = [ref for ref in still_open if ref not in drop]
    if not refused:
        return
    noun = "issue" if len(refused) == 1 else "issues"
    if wanted[0].owner != source.owner:
        # A move closes this advisory, so it cannot keep tracking an issue
        # in its own org: the command that succeeds is the same one, re-run
        # once the open issue no longer needs it.
        raise RetargetRefusedError(
            f"advisory {ghsa_id}: this would stop tracking the open {noun} {_slugs(refused)}; "
            f"a move to {wanted[0].owner}/.github closes this advisory, so it cannot keep "
            f"tracking an issue in {source.owner}. Close or retarget that work first, then "
            f"run: {retarget_command(ghsa_id, source, wanted, drop)}. "
            "Leaving an open issue untracked is the human's decision"
        )
    keep = tuple(dict.fromkeys((*still_open, *wanted)))
    raise RetargetRefusedError(
        f"advisory {ghsa_id}: this would stop tracking the open {noun} {_slugs(refused)}; "
        f"to keep tracking every open issue, run: {retarget_command(ghsa_id, source, keep)}. "
        "Leaving an open issue untracked is the human's decision"
    )


def retarget(  # noqa: PLR0913 - the move, its source and its two exception lists
    home: str,
    ghsa_id: str,
    targets: Sequence[IssueRef],
    *,
    source: IssueRef,
    drop: Sequence[IssueRef] = (),
    renamed: Sequence[IssueRef] = (),
) -> tuple[str, ...]:
    """Move advisory *ghsa_id*'s tracking from *source* to *targets*.

    Every target must be in one org (:func:`fix_org`). When that org is not
    *home*'s, the advisory itself **moves** to the targets' org
    (:func:`_move`), and the return value ends with the original's
    :func:`moved_to_line`, which names the copy's GHSA ID. Otherwise:

    It first reads the tracking lines and runs two checks. Either raises
    :class:`RetargetRefusedError` before anything is written, naming the
    issues the advisory tracks and the command that would succeed:

    - **Source check.** *source* must be an issue a ``Tracking issue:`` line
      names. A **re-run** of a move whose rewrite already landed is accepted
      too: when the tracking lines are already exactly *targets* (as a set)
      and *source* is tied to that move, either because its body still names
      *ghsa_id* (the run stopped before its unmark finished) or because it
      carries the comment :func:`unmark_issue` posts naming exactly *targets*
      (the run finished). Such a re-run moves nothing: it only finishes the
      unmarks. Its listing also finds any other issue whose body still names
      *ghsa_id* though no tracking line names it (the other half of a broken
      pair, which a first run leaves alone): the re-run unmarks it when it is
      closed and refuses on it when it is open, so a re-run writes nothing
      only when no such issue exists. Any other source is refused.
    - **Drop check.** A **dropped** issue is one that is not a target and not
      in *renamed*, and is named by the current tracking lines. A dropped
      issue that is still open, other than *source* (or the issue *source*
      was transferred to), is refused unless *drop* names it. Its state is
      read directly; a failed read fails the run.

    The issues a first run touches are known from the tracking lines and the
    arguments, so it reads only those, and no org-wide listing: an issue
    whose body names *ghsa_id* though the advisory does not track it (the
    other half of a broken pair) is ``sync``'s to report, not this run's to
    find (vergil-tooling#5138). A **re-run** after the rewrite landed (the
    tracking lines already name exactly *targets*, as a set) can no longer
    learn the dropped issues from the tracking lines, so it alone lists the
    org's ``security`` issues (:func:`_write_listing`) and also drops each
    listed issue whose advisory line names *ghsa_id*.

    Every issue body it reads is read through the trust layer, and each one
    that decides a write or is written back must be trusted: on a re-run, a
    listed ``security`` issue with an untrusted body (:func:`_write_listing`),
    and on any run an untrusted body on a target or an issue to unmark
    (:func:`_check_bodies`), raises :class:`RetargetRefusedError` before
    anything is written.

    Then, in order: mark every target, replace every ``Tracking issue:`` line
    with one per target (skipped when the description would not change), then
    unmark every dropped issue, *source* last. Each step only starts when the
    one before succeeded, so a failure never leaves an advisory tracking an
    unmarked issue, and a re-run with the same arguments finishes the move.
    Any failure raises :class:`RetargetError` naming the advisory and the
    step; the caller must not close or move the old issue until this returns.

    A dropped issue whose body has no line naming *ghsa_id* (it names only
    other advisories, or none) is never touched (:func:`unmark_issue`); one
    that also names other advisories loses only this line and keeps its label.

    *renamed* names old numbers of issues that are themselves a target (a
    transferred issue): their tracking line is replaced but they are never
    unmarked, since the old number now leads to the moved issue itself.

    A dropped issue that was transferred but is not named in *renamed* is
    never written at its old number (:func:`unmark_issue`). The return value
    holds one note per such issue, saying what the unmark did.
    """
    wanted = tuple(dict.fromkeys(targets))
    if not wanted:
        raise RetargetError(f"advisory {ghsa_id}: no target issues given")
    if fix_org(ghsa_id, wanted) != home.split("/", 1)[0]:
        return _move(home, ghsa_id, wanted, source=source, drop=drop, renamed=renamed)
    with _step(ghsa_id, "reading the tracking lines"):
        _summary, description = read_text(home, ghsa_id)
    previous = parse_tracking(description)
    _check_source(ghsa_id, source, previous, wanted)
    listed: dict[IssueRef, SecurityIssue] = {}
    if set(previous) == set(wanted):
        # The tracking lines already name exactly the targets: a re-run of a
        # move whose rewrite landed, so they no longer name what it drops.
        listed = {issue.ref: issue for issue in _write_listing(ghsa_id, home.split("/", 1)[0])}
    unmarks = _checked_drops(ghsa_id, source, previous, wanted, drop, renamed, listed)
    _check_bodies(ghsa_id, (*wanted, *unmarks))
    _hand_to(home, ghsa_id, wanted)
    with _step(ghsa_id, "rewriting the tracking lines"):
        _set_tracking(home, ghsa_id, description, wanted)
    return _unmark_all(ghsa_id, unmarks, wanted)


def _checked_drops(  # noqa: PLR0913 - the drop check's inputs, as retarget has them
    ghsa_id: str,
    source: IssueRef,
    previous: Sequence[IssueRef],
    wanted: Sequence[IssueRef],
    drop: Sequence[IssueRef],
    renamed: Sequence[IssueRef],
    listed: dict[IssueRef, SecurityIssue],
) -> list[IssueRef]:
    """Run the drop check (:func:`_check_drops`); return the issues to unmark, in order.

    The **dropped** issues are those *previous* tracks or a listed
    ``security`` issue names, less the targets and *renamed*; *listed* is
    empty unless the run is a re-run that had to list them (:func:`retarget`).
    *source* (and
    the issue it was transferred to) goes last, so a run interrupted before
    then leaves it marked and a re-run's source check still finds it tied
    to the move.
    """
    marked = [ref for ref, issue in listed.items() if ghsa_id in issue.ghsa_ids]
    dropped = [
        ref
        for ref in dict.fromkeys((*previous, *marked))
        if ref not in wanted and ref not in renamed
    ]
    with _step(ghsa_id, f"reading {source.slug}"):
        source_now = _current_ref(_read_issue(source))
    others = [ref for ref in dropped if ref not in (source, source_now)]
    _check_drops(ghsa_id, source, wanted, others, drop, listed)
    last = [ref for ref in (source, source_now) if ref in dropped]
    return list(dict.fromkeys((*others, *last)))


def _write_listing(ghsa_id: str, org: str) -> list[SecurityIssue]:
    """*org*'s ``security`` issues (:func:`security_issues`), for a retarget re-run.

    The re-run unmarks every listed issue whose advisory line names
    *ghsa_id*. An untrusted body's lines are never read, so which issues
    those are cannot be known: any untrusted body among them is a stop
    before any write, raised as :class:`RetargetRefusedError` and naming
    metadata only. A failed read raises :class:`RetargetError`.
    """
    with _step(ghsa_id, "listing the security issues"):
        listed = security_issues(org)
    for issue in listed:
        if issue.skipped is not None:
            stop = trust.UntrustedContentError(
                issue.skipped,
                "a security issue's body is untrusted, so the issues naming this advisory "
                "cannot be read",
            )
            raise RetargetRefusedError(f"advisory {ghsa_id}: {stop}")
    return listed


def _check_bodies(ghsa_id: str, refs: Sequence[IssueRef]) -> None:
    """Read the trusted body of each of *refs*, every issue a retarget writes, before any write.

    Each is read as its write will read it (:func:`_issue_state`), so an
    untrusted body refuses the run with :class:`RetargetRefusedError` before
    the first target is marked, never part-way through; a failed read raises
    :class:`RetargetError`.
    """
    for ref in dict.fromkeys(refs):
        stop: trust.UntrustedContentError | None = None
        with _step(ghsa_id, f"reading {ref.slug}"):
            try:
                _issue_state(ref)
            except trust.UntrustedContentError as exc:
                stop = exc
        if stop is not None:
            raise RetargetRefusedError(f"advisory {ghsa_id}: {stop}") from stop


def _hand_to(home: str, ghsa_id: str, wanted: Sequence[IssueRef]) -> None:
    """Mark every target with advisory *ghsa_id* of *home*, each a step of its own."""
    for ref in wanted:
        with _step(ghsa_id, f"marking {ref.slug}"):
            mark_issue(ref, home, ghsa_id)


def _set_tracking(home: str, ghsa_id: str, description: str, wanted: Sequence[IssueRef]) -> None:
    """Write *description* with one tracking line per target, unless that changes nothing."""
    rewritten = _with_tracking(description, wanted)
    if rewritten != description:
        _write_description(home, ghsa_id, rewritten)


def _unmark_all(
    ghsa_id: str, refs: Sequence[IssueRef], wanted: Sequence[IssueRef]
) -> tuple[str, ...]:
    """Unmark each of *refs* in order (:func:`unmark_issue`); return the transfer notes."""
    notes: list[str] = []
    for ref in refs:
        with _step(ghsa_id, f"unmarking {ref.slug}"):
            note = unmark_issue(ref, ghsa_id, wanted)
        if note is not None:
            notes.append(note)
    return tuple(notes)


# --- the cross-org move -----------------------------------------------------------


def fix_org(ghsa_id: str, targets: Sequence[IssueRef]) -> str:
    """The one org every target is in; :class:`RetargetRefusedError` when they span orgs.

    Advisories are homed and synced per org, so one advisory's fix tasks are
    all in one org. The refusal names the two ways out.
    """
    orgs = sorted({ref.owner for ref in targets})
    if len(orgs) != 1:
        raise RetargetRefusedError(
            f"advisory {ghsa_id}: the --to issues span orgs ({', '.join(orgs) or 'none'}), "
            "and every fix of one advisory must be in one org. Either duplicate the "
            "advisory into each org, each copy tracking that org's own fix tasks, or "
            "break the finding up into separate advisories"
        )
    return orgs[0]


def _read_move_fields(home: str, ghsa_id: str) -> tuple[str, str, object]:
    """Advisory *ghsa_id*'s ``(summary, description, severity)``, for its copy only."""
    with _no_text():
        raw: Any = github.read_json(
            "api", f"{_advisories(home)}/{ghsa_id}", "--jq", "{summary, description, severity}"
        )
    summary, description = raw.get("summary"), raw.get("description") or ""
    if not isinstance(summary, str) or not isinstance(description, str):
        raise ValueError(f"advisory {ghsa_id} read returned no summary or description")
    return summary, description, raw.get("severity")


def _movable(home: str, ghsa_id: str, advisories: Sequence[Advisory]) -> Advisory:
    """Advisory *ghsa_id* of *home*, refused unless it is a draft record, or a moved one.

    A copy an earlier move made (one with a ``Moved from:`` line) never
    moves again: its original's ``Moved to:`` line would then name an
    advisory that tracks nothing, and ``sync`` would report that original
    broken with no way to clear it. The refusal names the way out.
    """
    advisory = {a.ghsa_id: a for a in advisories}.get(ghsa_id)
    if advisory is None:
        raise RetargetRefusedError(f"advisory {ghsa_id} does not exist in {home}")
    if advisory.moved_from:
        raise RetargetRefusedError(
            f"advisory {ghsa_id} is itself the copy of a move (it has a Moved from: "
            f"{advisory.moved_from[0]} line), and a copy never moves again. Keep it "
            "tracking the fix tasks of its own org and create a separate advisory in the "
            "new org for the fix work there: one copy of the finding per org"
        )
    if len(advisory.moved_to) > 1:
        raise RetargetRefusedError(
            f"advisory {ghsa_id} has more than one Moved to: line, so which copy holds it "
            "is a human's decision"
        )
    resumable = advisory.state == "closed" and bool(advisory.moved_to)
    if advisory.external or not (advisory.state == "draft" or resumable):
        kind = "an external report" if advisory.external else f"in state {advisory.state}"
        raise RetargetRefusedError(
            f"advisory {ghsa_id} is {kind}; a move hands on a draft security record only, "
            "and any other state is a human's decision"
        )
    return advisory


def _reusable_copy(
    ghsa_id: str,
    to_home: str,
    candidates: Sequence[Advisory],
    moved_to: Sequence[tuple[str, str]],
    wanted: Sequence[IssueRef],
) -> Advisory | None:
    """The copy an earlier run of this move made, or None; refuse any other match.

    *candidates* are the advisories of *to_home* with a ``Moved from:`` line
    naming *ghsa_id*. One is reused only when it is the only one, a
    ``draft``, and not an external report: a private vulnerability report's
    submitter writes its description, so a line there proves nothing. Any
    other match refuses the move, naming each candidate. A copy must also
    agree with the original's ``Moved to:`` line, when it has one, and
    track nothing or exactly *wanted*.
    """
    if len(candidates) > 1 or any(c.external or c.state != "draft" for c in candidates):
        names = ", ".join(
            f"{c.ghsa_id} ({'an external report' if c.external else c.state})" for c in candidates
        )
        raise RetargetRefusedError(
            f"advisory {ghsa_id}: {to_home} has advisories with a Moved from: {ghsa_id} "
            f"line ({names}); a move reuses only one draft record it made, so it reuses "
            "none of these and creates no copy. A human must check them"
        )
    copy = candidates[0] if candidates else None
    if moved_to:
        org, copy_id = moved_to[0]
        if copy is None or (org, copy_id) != (to_home.split("/", 1)[0], copy.ghsa_id):
            found = f"; {to_home} holds {copy.ghsa_id}" if copy else ""
            raise RetargetRefusedError(
                f"advisory {ghsa_id} has already moved to {org}/.github {copy_id}, which is "
                f"not a copy of it in {to_home}{found}. A human must decide where it lives"
            )
    if copy is not None and copy.tracking and set(copy.tracking) != set(wanted):
        raise RetargetRefusedError(
            f"advisory {ghsa_id}: its copy {copy.ghsa_id} in {to_home} already tracks "
            f"{_slugs(copy.tracking)}; retarget the copy in its own org instead"
        )
    return copy


def _move(  # noqa: PLR0913 - retarget's inputs, passed on unchanged
    home: str,
    ghsa_id: str,
    wanted: Sequence[IssueRef],
    *,
    source: IssueRef,
    drop: Sequence[IssueRef],
    renamed: Sequence[IssueRef],
) -> tuple[str, ...]:
    """Move advisory *ghsa_id* from *home* to the advisory home of *wanted*'s org.

    GitHub cannot move an advisory, so the move copies it. Every check runs
    before the first write, and each raises :class:`RetargetRefusedError`:

    - the original must be a ``draft``, not an external report, with at most
      one ``Moved to:`` line (a ``closed`` one carrying the line is a
      finished move, re-run), and must not itself be the copy an earlier
      move made, one with a ``Moved from:`` line (:func:`_movable`);
    - the **source check** and **drop check** of :func:`retarget`, so a move
      never closes an advisory that still tracks open work no ``--drop``
      names; once the original's tracking lines are gone, *source* must be
      tied to this move (:func:`_source_tied_to_move`);
    - the trust checks of :func:`retarget`: no untrusted body among the
      issues to unmark and the targets. A move lists no ``security`` issues:
      the original keeps its tracking lines until every unmark has run, so
      they name every issue a re-run still has to unmark;
    - the copy to reuse (:func:`_reusable_copy`), and a severity the copy
      can take when there is none.

    Then, in order, each step only once the one before succeeded:

    1. **Reuse** the copy an earlier run made, found by its ``Moved from:``
       line; otherwise
    2. **create** it in the fix org's home with the original's summary,
       details (less its tracking lines) and severity plus
       :func:`moved_from_line`, the affected repo being the first target's;
       then mark every target with it and give it one tracking line each;
    3. write :func:`moved_to_line` on the original, unmark every dropped
       issue (*source* last), remove the original's tracking lines and set
       it ``closed``.

    Each org's calls run under :func:`github.target_org` of that org. A
    re-run of the identical command finishes a move that failed partway,
    and writes nothing once it is done. Returns the transfer notes, then
    the original's ``Moved to:`` line.
    """
    org, to_org = home.split("/", 1)[0], wanted[0].owner
    with github.target_org(org):
        with _step(ghsa_id, "reading the advisories"):
            advisories = list_advisories(home)
        original = _movable(home, ghsa_id, advisories)
        with _step(ghsa_id, "reading the advisory"):
            summary, description, severity = _read_move_fields(home, ghsa_id)
        previous = parse_tracking(description)
        moved_to = parse_moved_to(description)
        dropping = previous
        if moved_to and not previous:
            with _step(ghsa_id, f"reading {source.slug}"):
                tied = _source_tied_to_move(source, ghsa_id, wanted)
            if not tied:
                raise RetargetRefusedError(
                    f"advisory {ghsa_id} has moved and tracks no issue, and --from "
                    f"{source.slug} is not the source of a move to {_slugs(wanted)}"
                )
            # The unmarks all ran before the tracking lines went, so only the
            # source can be left to finish; an unmark of it is a no-op when it
            # no longer names the advisory.
            dropping = (source,)
        else:
            _check_source(ghsa_id, source, previous, wanted)
        unmarks = _checked_drops(ghsa_id, source, dropping, wanted, drop, renamed, {})
        _check_bodies(ghsa_id, unmarks)
    with github.target_org(to_org):
        _check_bodies(ghsa_id, wanted)
        to_home = advisory_home(to_org)
        with _step(ghsa_id, f"listing the advisories in {to_home}"):
            candidates = [a for a in list_advisories(to_home) if ghsa_id in a.moved_from]
    copy = _reusable_copy(ghsa_id, to_home, candidates, moved_to, wanted)
    if copy is None and severity not in SEVERITIES:
        raise RetargetRefusedError(
            f"advisory {ghsa_id} has no {', '.join(SEVERITIES)} severity for its copy "
            "to take; set its severity first"
        )
    with github.target_org(to_org):
        if copy is None:
            with _step(ghsa_id, f"creating its copy in {to_home}"):
                copy_id = create_advisory(
                    to_home,
                    summary=summary,
                    details=_append_line(_without_tracking(description), moved_from_line(ghsa_id)),
                    affected_repo=f"{wanted[0].owner}/{wanted[0].repo}",
                    severity=str(severity),
                )
        else:
            copy_id = copy.ghsa_id
        _hand_to(to_home, copy_id, wanted)
        with _step(copy_id, "writing the tracking lines"):
            _summary, copy_text = read_text(to_home, copy_id)
            _set_tracking(to_home, copy_id, copy_text, wanted)
    line = moved_to_line(to_home, copy_id)
    with github.target_org(org):
        if not moved_to:
            description = _before_tracking(description, line)
            with _step(ghsa_id, "writing the Moved to: line"):
                _write_description(home, ghsa_id, description)
        notes = _unmark_all(ghsa_id, unmarks, wanted)
        if previous:
            with _step(ghsa_id, "removing the tracking lines"):
                _write_description(home, ghsa_id, _without_tracking(description))
        if original.state != "closed":
            with _step(ghsa_id, "setting the state to closed"):
                set_state(home, ghsa_id, "closed")
    return (*notes, line)


def tracked_issues(home: str, ghsa_id: str) -> tuple[IssueRef, ...]:
    """The issues advisory *ghsa_id*'s ``Tracking issue:`` lines name, in order.

    The description is read through :func:`read_text` and only the refs leave
    this function. A failed read raises :class:`RetargetError`.
    """
    with _step(ghsa_id, "reading the tracking lines"):
        _summary, description = read_text(home, ghsa_id)
    return parse_tracking(description)


def pair_check(home: str, ghsa_id: str, issue: IssueRef | None) -> None:
    """Refuse a first pairing of *ghsa_id* when it already tracks another issue. Read-only.

    An advisory with no ``Tracking issue:`` line passes, and so does one
    whose only line names *issue* (a resumed ``create``). With *issue* None
    (not yet known) any tracking line refuses. The refusal is a
    :class:`RetargetRefusedError` naming the tracked issues; moving an
    advisory that is already paired is :func:`retarget`'s job.
    """
    tracked = tracked_issues(home, ghsa_id)
    if not tracked or (issue is not None and tracked == (issue,)):
        return
    if issue is not None:
        hint = f"to move it, run: {retarget_command(ghsa_id, tracked[0], (issue,))}"
    else:
        hint = "to move it, run vrg-security-record retarget with --from an issue it tracks"
    raise RetargetRefusedError(
        f"advisory {ghsa_id} already tracks {_slugs(tracked)}, so its first pair is made; {hint}"
    )


def pair(home: str, ghsa_id: str, issue: IssueRef) -> None:
    """Make advisory *ghsa_id*'s first pairing: mark *issue*, then write its tracking line.

    :func:`pair_check` runs first, so an advisory already tracking another
    issue is refused before any write. One already tracking *issue* alone (a
    resumed ``create``) only has its mark finished. The mark comes before the
    line, so the advisory never tracks an unmarked issue.
    """
    pair_check(home, ghsa_id, issue)
    with _step(ghsa_id, f"marking {issue.slug}"):
        mark_issue(issue, home, ghsa_id)
    with _step(ghsa_id, "reading the tracking lines"):
        _summary, description = read_text(home, ghsa_id)
    rewritten = _with_tracking(description, (issue,))
    if rewritten != description:
        with _step(ghsa_id, "writing the tracking line"):
            _write_description(home, ghsa_id, rewritten)


def security_issues(org: str) -> list[SecurityIssue]:
    """Every ``security``-labelled issue in *org*'s managed repos, open or closed.

    The same repo enumeration as :func:`epic_audit.open_issues` (non-archived
    repos with a ``vergil.toml``), read per repo through the REST label filter,
    never through search, whose index can lag. The listing gives each issue's
    number and state only; its body, whose advisory lines are ``ghsa_ids``, is
    read through the trust layer's body-only batched read
    (:func:`trust.read_bodies`): one query per repo for up to 50 issues, and
    no comment or comment history, since nothing here is taken from a
    comment (vergil-tooling#5138). An untrusted body is listed with its
    ``skipped`` record and no ``ghsa_ids``, never with the lines it carries;
    each caller decides what that means. A trust read failure raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`.
    """
    found: list[SecurityIssue] = []
    authors: TrustedAuthors | None = None
    for bare in github.list_org_repos(org):
        repo = f"{org}/{bare}"
        if not epics.is_managed_repo(repo):
            continue
        rows: list[Any] = github.read_json_paginated(
            f"repos/{repo}/issues?state=all&labels={SECURITY_LABEL}", jq=_SECURITY_ISSUES_JQ
        )
        if not rows:
            continue
        if authors is None:
            authors = load_trusted_authors()
        bodies = trust.read_bodies(repo, [int(row["number"]) for row in rows], authors=authors)
        for item in rows:
            number = int(item["number"])
            content = bodies[number]
            is_open = str(item["state"]).lower() == "open"
            reason = item.get("state_reason")
            found.append(
                SecurityIssue(
                    ref=IssueRef(org, bare, number),
                    open=is_open,
                    ghsa_ids=() if content.body is None else parse_advisory_refs(content.body),
                    state_reason=None if is_open or reason is None else str(reason).lower(),
                    skipped=content.skipped,
                )
            )
    return found


def _tracking_problems(advisory: Advisory, by_ref: dict[IssueRef, SecurityIssue]) -> list[str]:
    if not advisory.tracking:
        return [f"{advisory.ghsa_id}: no Tracking issue: lines"]
    problems: list[str] = []
    for ref in advisory.tracking:
        issue = by_ref.get(ref)
        if issue is None:
            problems.append(
                f"{advisory.ghsa_id}: tracks {ref.slug}, which is not a security-labelled issue"
            )
        elif advisory.ghsa_id not in issue.ghsa_ids:
            problems.append(f"{advisory.ghsa_id}: tracks {ref.slug}, which does not point back")
    return problems


def _issue_problems(issue: SecurityIssue, by_id: dict[str, Advisory]) -> list[str]:
    """One broken line per wrong advisory line of *issue* (or one for having none)."""
    if not issue.ghsa_ids:
        return [f"{issue.ref.slug}: security label but no Security advisory: line"]
    problems: list[str] = []
    for ghsa_id in issue.ghsa_ids:
        advisory = by_id.get(ghsa_id)
        if advisory is None:
            problems.append(f"{issue.ref.slug}: names {ghsa_id}, which does not exist")
        elif issue.ref not in advisory.tracking:
            problems.append(f"{issue.ref.slug}: names {ghsa_id}, which does not track it")
    return problems


def classify(advisories: Sequence[Advisory], issues: Sequence[SecurityIssue]) -> Classification:
    """Classify every advisory and check every pair's links both ways (spec 4.3).

    - An advisory some ``security`` issue names is **paired** when every one of
      its ``Tracking issue:`` lines names a ``security`` issue that names it
      back; otherwise each wrong or missing line is **broken**.
    - An advisory no issue names is an **external** report when it came from
      private vulnerability reporting (``triage`` state or a submission), and
      **broken** otherwise.
    - A ``security`` issue is **broken** when it has no advisory line; each of
      its lines naming an advisory that does not exist or does not track it is
      a broken line of its own.

    - An advisory that is not an external report and carries one ``Moved
      to:`` line is **moved** (a terminal state, whatever issues name it or
      it tracks); one with more than one such line is **broken**. An external
      report's description is its submitter's, so a line there moves nothing.

    An issue :func:`retarget` unmarked has no ``security`` label, so it is not
    among *issues* and is never reported.

    - A ``security`` issue whose body the trust layer does not trust
      (``skipped``) has no lines that can be read, so it is never broken on
      its own; its record is the caller's to report. An advisory (not moved)
      that tracks one is **untrusted**: its pair cannot be checked, so it is
      neither paired nor broken, and nothing acts on it.
    """
    by_id = {advisory.ghsa_id: advisory for advisory in advisories}
    by_ref = {issue.ref: issue for issue in issues}
    named = {ghsa_id for issue in issues for ghsa_id in issue.ghsa_ids}
    unreadable = {issue.ref for issue in issues if issue.skipped is not None}
    paired: list[Advisory] = []
    external: list[Advisory] = []
    broken: list[str] = []
    moved: list[Advisory] = []
    untrusted: list[Advisory] = []
    for advisory in sorted(advisories, key=lambda a: a.ghsa_id):
        if advisory.moved_to and not advisory.external:
            if len(advisory.moved_to) == 1:
                moved.append(advisory)
            else:
                broken.append(f"{advisory.ghsa_id}: more than one Moved to: line")
            continue
        if unreadable.intersection(advisory.tracking):
            untrusted.append(advisory)
            continue
        if advisory.ghsa_id not in named:
            if advisory.external:
                external.append(advisory)
            else:
                broken.append(f"{advisory.ghsa_id}: no security issue names this advisory")
            continue
        problems = _tracking_problems(advisory, by_ref)
        if problems:
            broken += problems
        else:
            paired.append(advisory)
    for issue in sorted(issues, key=lambda i: i.ref.slug):
        if issue.skipped is None:
            broken += _issue_problems(issue, by_id)
    return Classification(
        paired=paired, external=external, broken=broken, moved=moved, untrusted=untrusted
    )


def moved_problems(moved: Sequence[Advisory]) -> list[str]:
    """One broken line per moved advisory whose copy does not hold it.

    The copy its ``Moved to:`` line names must exist in that org's advisory
    home, carry a ``Moved from:`` line naming the original, and track at
    least one issue (its own org's sync checks those links). Each home is
    listed once, under :func:`github.target_org` of its org; a failed read
    propagates, so the pass fails rather than vouch for a pair it never saw.
    """
    homes: dict[str, dict[str, Advisory]] = {}
    problems: list[str] = []
    for advisory in moved:
        org, copy_id = advisory.moved_to[0]
        if org not in homes:
            with github.target_org(org):
                listed = list_advisories(advisory_home(org))
            homes[org] = {a.ghsa_id: a for a in listed}
        copy = homes[org].get(copy_id)
        where = f"{advisory.ghsa_id}: moved to {org}/.github {copy_id}"
        if copy is None:
            problems.append(f"{where}, which does not exist")
        elif advisory.ghsa_id not in copy.moved_from:
            problems.append(f"{where}, which has no Moved from: {advisory.ghsa_id} line")
        elif not copy.tracking:
            problems.append(f"{where}, which tracks no issue")
    return problems


# The states the sync keeps in step with the issues. Any other state (a
# published or withdrawn advisory, or a triage-state report a pair adopted) is
# a human's decision, so the sync never moves an advisory out of it.
_SYNCED_STATES = ("draft", "closed")


def _would_close(advisory: Advisory, by_ref: dict[IssueRef, SecurityIssue]) -> bool:
    """True iff the issue states alone would move *advisory* from ``draft`` to ``closed``."""
    # classify() pairs an advisory only when every tracked issue is listed.
    return (
        not advisory.external
        and advisory.state == "draft"
        and not any(by_ref[ref].open for ref in advisory.tracking)
    )


def _parent_queue_kind(ref: IssueRef) -> str | None:
    """The queue kind of *ref*'s native parent (live queue or queue archive), else None."""
    parent = epics.native_parent_of(ref)
    return None if parent is None else epics.queue_or_archive_kind(parent)


def guard_check(classification: Classification, issues: Sequence[SecurityIssue]) -> GuardResult:
    """The sync close guard (spec 6.2): which advisories the sync must not close.

    Only a **paired** ``draft`` advisory whose tracked issues are all closed
    is checked, since only that one would move to ``closed``. The guard fires
    when any of its tracked issues was closed with a reason other than
    ``completed`` (a closed issue with no recorded reason included), or is a
    queue item: its native parent is a live intake, planning or deferred epic,
    or a queue archive epic of one (:func:`epics.queue_or_archive_kind`). The
    parent is read only for a tracked issue closed as completed, once per
    issue. An advisory already ``closed`` is never checked, so a human's
    ``vrg-security-record close`` stands and is not reported as held.

    A failed parent read is an error for each advisory that needed it, never
    a close and never a hold; the other advisories are still decided. A
    missing token is not one advisory's failure and propagates.
    """
    by_ref = {issue.ref: issue for issue in issues}
    kinds: dict[IssueRef, str | None | Exception] = {}
    held: list[tuple[str, tuple[IssueRef, ...]]] = []
    errors: list[tuple[str, IssueRef, str]] = []
    for advisory in classification.paired:
        if not _would_close(advisory, by_ref):
            continue
        firing: list[IssueRef] = []
        failed: tuple[IssueRef, str] | None = None
        for ref in advisory.tracking:
            if by_ref[ref].state_reason != _COMPLETED:
                firing.append(ref)
                continue
            if ref not in kinds:
                try:
                    kinds[ref] = _parent_queue_kind(ref)
                except github.MissingGitHubTokenError:
                    raise
                except github.GitHubAPIError as exc:
                    kinds[ref] = _scrubbed(exc)
                except RuntimeError as exc:  # a GraphQL error payload
                    kinds[ref] = exc
            kind = kinds[ref]
            if isinstance(kind, Exception):
                failed = failed or (ref, " ".join(str(kind).split()))
            elif kind is not None:
                firing.append(ref)
        if failed is not None:
            errors.append((advisory.ghsa_id, *failed))
        elif firing:
            held.append((advisory.ghsa_id, tuple(firing)))
    return GuardResult(held=held, errors=errors)


def pending_syncs(
    classification: Classification, issues: Sequence[SecurityIssue], guard: GuardResult
) -> list[tuple[str, Literal["draft", "closed"]]]:
    """The ``(GHSA ID, target state)`` changes the issue-driven sync would make.

    For each **paired** advisory the target is ``closed`` when every issue it
    tracks is closed, and ``draft`` while any is open (spec 4.3: the issue
    always wins). Only a mismatch is listed. External reports are never
    touched, and neither is an advisory outside ``draft``/``closed``. An
    advisory *guard* (:func:`guard_check`) holds or could not decide gets no
    change: it stays ``draft``.
    """
    stopped = {ghsa_id for ghsa_id, _ in guard.held} | {e[0] for e in guard.errors}
    open_by_ref = {issue.ref: issue.open for issue in issues}
    changes: list[tuple[str, Literal["draft", "closed"]]] = []
    for advisory in classification.paired:
        if advisory.external or advisory.state not in _SYNCED_STATES:
            continue
        if advisory.ghsa_id in stopped:
            continue
        # classify() pairs an advisory only when every tracked issue is listed.
        any_open = any(open_by_ref[ref] for ref in advisory.tracking)
        target: Literal["draft", "closed"] = "draft" if any_open else "closed"
        if advisory.state != target:
            changes.append((advisory.ghsa_id, target))
    return changes


def sync_states(
    home: str,
    classification: Classification,
    issues: Sequence[SecurityIssue],
    guard: GuardResult,
) -> int:
    """Apply :func:`pending_syncs` to *home* and return the number of advisories changed.

    Stops at the first failed write, which propagates scrubbed of its stdout;
    the next sync retries whatever is still out of step.
    """
    changes = pending_syncs(classification, issues, guard)
    for ghsa_id, state in changes:
        set_state(home, ghsa_id, state)
    return len(changes)


def held_line(ghsa_id: str, refs: Sequence[IssueRef]) -> str:
    """The ``- held:`` line for *ghsa_id*: the issues that fired the guard, and the next step."""
    slugs = ", ".join(ref.slug for ref in refs)
    return (
        f"- held: {ghsa_id} ({slugs}): retarget it to a fix task, "
        "or close it with vrg-security-record close"
    )


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def unlabelled_problem(ref: IssueRef) -> str:
    """The broken line for *ref*: a ``Security advisory:`` line but no ``security`` label.

    It names the issue alone, never the GHSA IDs its lines carry (spec 6.3 of
    epic vergil-project/.github#1331).
    """
    return f"{ref.slug}: Security advisory: line but no security label"


def records_pass(org: str, *, apply: bool, prog: str, unlabelled: Sequence[IssueRef] = ()) -> int:
    """The security-records pass over *org*; *apply* False previews it without writing.

    Lists the advisories in the org's advisory home, syncs each paired
    advisory's ``draft``/``closed`` state to its issues (:func:`sync_states`),
    and reports broken pairs and unpaired external reports. A **moved**
    advisory is never synced; its copy is checked in the org it moved to
    (:func:`moved_problems`), and a copy that does not hold it is a broken
    pair. It needs **Repository security advisories: read** on
    ``<org>/.github`` (and on the ``.github`` of each org an advisory moved
    to), and with *apply* also **write**. *prog* prefixes the failure line on
    stderr.

    Its output names only GHSA IDs, issue refs and states, never advisory
    text: every advisory read it makes projects the text away, and
    :class:`Advisory` has no text field. The advisory home is resolved only
    when the org has a ``security`` issue or a ``.github`` repo; with neither
    there is nothing to check.

    The sync close guard (:func:`guard_check`) keeps an advisory ``draft``
    while a queue item or a non-completed close is all that ended it. Each
    advisory it holds is reported on a ``- held:`` line with the next step,
    and counted apart from broken pairs; a hold waits on a human and does not
    fail the pass. An advisory whose guard read failed is reported on a
    ``- error:`` line and left unchanged, and the rest are still synced.

    Each issue's body is read through the trust layer (:func:`security_issues`).
    An untrusted body is reported, never acted on: its record goes to stderr,
    metadata only (:func:`trust.report_skipped`, counts by cause under GitHub
    Actions), it is not reported broken, and each advisory tracking it is left
    unchanged and named on a ``- untrusted:`` line by GHSA ID alone.

    *unlabelled* are the issues the caller found with a ``Security advisory:``
    line in a trusted body but no ``security`` label (spec 6.3 of epic
    vergil-project/.github#1331): each is a broken pair, named by issue ref
    alone (:func:`unlabelled_problem`), whether or not an advisory tracks it.

    Exit 1 when the pass fails, when any issue body is untrusted (the preview
    included: a human decides on it), or, with *apply*, when any pair is
    broken or any guard read failed (after syncing the rest). The preview
    reports broken pairs and guard errors without failing.
    """
    mode = "" if apply else " (preview)"
    try:
        issues = security_issues(org)
        if not issues and not unlabelled and not github.repo_exists(org, ".github"):
            print(f"Security records{mode}: none.")
            return 0
        home = advisory_home(org)
        result = classify(list_advisories(home), issues)
        guard = guard_check(result, issues)
        broken = [
            *result.broken,
            *moved_problems(result.moved),
            *(unlabelled_problem(ref) for ref in sorted(unlabelled, key=lambda r: r.slug)),
        ]
        synced = (
            sync_states(home, result, issues, guard)
            if apply
            else len(pending_syncs(result, issues, guard))
        )
    except github.MissingGitHubTokenError:
        raise
    except (github.GitHubAPIError, trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
        print(f"{prog}: security records pass failed: {exc}", file=sys.stderr)
        return 1
    skipped = [issue.skipped for issue in issues if issue.skipped is not None]
    verb = "synced" if apply else "would sync"
    print(
        f"Security records{mode}: {_count(synced, 'advisory state', 'advisory states')} {verb}, "
        f"{_count(len(broken), 'broken pair', 'broken pairs')}, "
        f"{len(guard.held)} held, {_count(len(guard.errors), 'error', 'errors')}."
    )
    for line in broken:
        print(f"- broken: {line}")
    for ghsa_id, refs in guard.held:
        print(held_line(ghsa_id, refs))
    for ghsa_id, ref, error in guard.errors:
        print(
            f"- error: {ghsa_id} ({ref.slug}): reading its parent failed, left unchanged: {error}"
        )
    for advisory in result.untrusted:
        print(
            f"- untrusted: {advisory.ghsa_id}: an issue it tracks has an untrusted body, "
            "left unchanged"
        )
    for advisory in result.external:
        print(f"- unpaired external report (left untouched): {advisory.ghsa_id}")
    trust.report_skipped(skipped, stream=sys.stderr)
    return 1 if skipped or (apply and (broken or guard.errors)) else 0


# --- sync of known issues ---------------------------------------------------

_KNOWN_ISSUE_JQ = "{number, repository_url, state, state_reason, labels: [.labels[].name]}"


@dataclass
class IssueSyncResult:
    """What :func:`sync_issues` did, by GHSA ID and issue ref only.

    ``synced`` holds each ``(GHSA ID, new state)`` it set; ``held`` each
    advisory the sync close guard kept ``draft`` (:func:`guard_check`), with
    the tracked issues that fired it, which waits on a human; ``problems``
    one line per reason an advisory could not be synced (a broken pair, an
    untrusted body, a guard read that failed), which the caller must report
    as a failure.
    """

    synced: list[tuple[str, Literal["draft", "closed"]]] = field(default_factory=list)
    held: list[tuple[str, tuple[IssueRef, ...]]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _known_security_issues(
    refs: Sequence[IssueRef], authors: TrustedAuthors
) -> list[SecurityIssue]:
    """The ``security``-labelled issues among *refs*, each at its current ref.

    Each issue is read once over REST (its state, close reason and labels; a
    transfer is followed to the issue's current ref), then the bodies of the
    labelled ones through the trust layer's body-only batched read, one query
    per repo, as :func:`security_issues` reads them. An issue without the
    label is left out, and its body is never read. A failed read propagates.
    """
    labelled: list[tuple[IssueRef, Any]] = []
    for ref in dict.fromkeys(refs):
        raw: Any = github.read_json("api", _issue_endpoint(ref), "--jq", _KNOWN_ISSUE_JQ)
        if SECURITY_LABEL in {str(name) for name in raw.get("labels") or []}:
            labelled.append((_current_ref(raw), raw))
    by_repo: dict[str, list[int]] = {}
    for current, _raw in labelled:
        by_repo.setdefault(f"{current.owner}/{current.repo}", []).append(current.number)
    bodies = {
        (repo, number): content
        for repo, numbers in by_repo.items()
        for number, content in trust.read_bodies(repo, numbers, authors=authors).items()
    }
    found: list[SecurityIssue] = []
    for current, raw in labelled:
        content = bodies[(f"{current.owner}/{current.repo}", current.number)]
        is_open = str(raw["state"]).lower() == "open"
        reason = raw.get("state_reason")
        found.append(
            SecurityIssue(
                ref=current,
                open=is_open,
                ghsa_ids=() if content.body is None else parse_advisory_refs(content.body),
                state_reason=None if is_open or reason is None else str(reason).lower(),
                skipped=content.skipped,
            )
        )
    return found


def _sync_org(org: str, refs: Sequence[IssueRef], authors: TrustedAuthors) -> IssueSyncResult:
    """:func:`sync_issues` for the issues *refs*, all in *org*."""
    result = IssueSyncResult()
    issues = _known_security_issues(refs, authors)
    result.problems += [
        f"{issue.ref.slug}: its body is untrusted, so the advisories it names were not synced"
        for issue in issues
        if issue.skipped is not None
    ]
    trusted = [issue for issue in issues if issue.skipped is None]
    # A security issue with no advisory line is a broken pair on its own,
    # whatever else the call holds, and needs no advisories call to say so.
    result.problems += [
        line for issue in trusted if not issue.ghsa_ids for line in _issue_problems(issue, {})
    ]
    named = [issue for issue in trusted if issue.ghsa_ids]
    ghsa_ids = tuple(dict.fromkeys(ghsa_id for issue in named for ghsa_id in issue.ghsa_ids))
    if not ghsa_ids:
        return result
    home = advisory_home(org)
    by_id = {advisory.ghsa_id: advisory for advisory in list_advisories(home)}
    for issue in named:
        result.problems += _issue_problems(issue, by_id)
    # A moved advisory is terminal and an external report is never synced.
    affected = [
        by_id[ghsa_id]
        for ghsa_id in ghsa_ids
        if ghsa_id in by_id and not by_id[ghsa_id].moved_to and not by_id[ghsa_id].external
    ]
    read = {issue.ref for issue in issues}
    others = [ref for advisory in affected for ref in advisory.tracking if ref not in read]
    every = [*issues, *_known_security_issues(others, authors)]
    by_ref = {issue.ref: issue for issue in every}
    unreadable = {issue.ref for issue in every if issue.skipped is not None}
    paired: list[Advisory] = []
    for advisory in affected:
        if unreadable.intersection(advisory.tracking):
            result.problems.append(
                f"{advisory.ghsa_id}: an issue it tracks has an untrusted body, left unchanged"
            )
            continue
        problems = _tracking_problems(advisory, by_ref)
        if problems:
            result.problems += problems
        else:
            paired.append(advisory)
    classification = Classification(paired=paired, external=[], broken=[])
    guard = guard_check(classification, every)
    result.held.extend(guard.held)
    result.problems += [
        f"{ghsa_id} ({ref.slug}): reading its parent failed, left unchanged: {error}"
        for ghsa_id, ref, error in guard.errors
    ]
    for ghsa_id, state in pending_syncs(classification, every, guard):
        set_state(home, ghsa_id, state)
        result.synced.append((ghsa_id, state))
    return result


def sync_issues(refs: Sequence[IssueRef]) -> IssueSyncResult:
    """Sync the advisories the ``security`` issues among *refs* name (vergil-tooling#4583).

    The event-driven half of the sync: a caller that has just changed some
    issues' state (``vrg-finalize-pr``, once a merged PR has closed them)
    syncs only the advisories those issues name, reading only the issues
    those advisories track, with no scan of the org. An issue without the
    ``security`` label is read over REST only, so a call whose issues name
    no advisory makes no advisories call and needs no advisories access.

    Each advisory named is decided as :func:`records_pass` decides it: only a
    **paired** advisory (every issue it tracks is a ``security`` issue that
    names it back) in ``draft`` or ``closed`` is synced, the sync close guard
    (:func:`guard_check`) holds one that a queue item or a close other than
    ``completed`` ended, and a moved advisory or an external report is left
    alone. A broken pair, an untrusted body (on one of *refs* or on an issue
    an advisory tracks) and a guard read that failed are ``problems``, each
    left unchanged; the rest are still synced. The issues are grouped by org,
    each org's run under :func:`github.target_org`, its advisory home that
    org's ``.github``.

    Whenever an issue names an advisory it needs **Repository security
    advisories: read and write** on that org's ``.github``. A failed read or
    write propagates (a ``gh`` failure scrubbed of its stdout, a
    :class:`~vergil_tooling.lib.trust.TrustReadError`), after any state
    already set: the caller must fail loudly, and ``vrg-security-record
    sync`` finishes the rest.
    """
    total = IssueSyncResult()
    by_org: dict[str, list[IssueRef]] = {}
    for ref in refs:
        by_org.setdefault(ref.owner, []).append(ref)
    if not by_org:
        return total
    authors = load_trusted_authors()
    for org, org_refs in by_org.items():
        with github.target_org(org):
            part = _sync_org(org, org_refs, authors)
        total.synced.extend(part.synced)
        total.held.extend(part.held)
        total.problems.extend(part.problems)
    return total


# --- close ------------------------------------------------------------------

CLOSE_REASON_MAX = 500


def close_comment(ghsa_id: str, reason: str) -> str:
    """The public comment a close posts: the GHSA ID and the reason, nothing else."""
    return f"Security record {ghsa_id} closed: {reason}"


def check_close_reason(reason: str) -> str:
    """*reason* stripped; :class:`CloseError` when it is empty, multi-line or too long."""
    text = reason.strip()
    if not text:
        raise CloseError("the close reason is empty")
    if "\n" in text or "\r" in text:
        raise CloseError("the close reason must be one line")
    if len(text) > CLOSE_REASON_MAX:
        raise CloseError(f"the close reason is longer than {CLOSE_REASON_MAX} characters")
    return text


@dataclass(frozen=True)
class CloseResult:
    """What :func:`close_record` did, by refs only.

    ``issues`` carry the close comment, at their current refs; ``notes`` say
    which tracked issue was transferred where; ``closed`` is False when the
    advisory was already closed, so this run only completed the comments.
    """

    issues: tuple[IssueRef, ...]
    notes: tuple[str, ...]
    closed: bool


def close_record(home: str, ghsa_id: str, reason: str) -> CloseResult:
    """Close advisory *ghsa_id* in *home*, leaving *reason* on every issue it tracks.

    The issues are those its ``Tracking issue:`` lines name, and only those:
    the close reads no other issue and lists none of the org's ``security``
    issues (vergil-tooling#5138). An issue whose body names the advisory
    though the advisory does not track it (the other half of a broken pair)
    gets no comment; finding and reporting it is ``sync``'s job. Each
    tracked issue is read at its current ref, following a transfer. The
    steps run in order, each only once the one before it succeeded:

    1. **Check.** Refuse, changing nothing, when the advisory does not exist,
       is an external report or is in a state other than ``draft`` or
       ``closed`` (a human's decision, which ``sync`` leaves alone too),
       tracks no issue to carry the comment, or has any tracked issue still
       open: ``sync`` sets an advisory back to draft while any issue it
       tracks is open, so the close would not hold. No issue body is read:
       the tracking lines, in the advisory's own description, pick the
       issues.
    2. **Comment.** Post :func:`close_comment` on each issue, skipping one
       that already carries that exact comment from a trusted author
       (:func:`trusted_comments.bodies`): an outsider's copy of it is no
       audit trail, so it never stops the post.
    3. **Close.** Set the advisory's state to ``closed``, unless it already is.

    The comments go first, so a closed advisory always has its audit trail,
    and a re-run with the same reason after a failure finishes the job
    without posting twice. Any failure raises :class:`CloseError` naming the
    advisory and the step, scrubbed of ``gh`` stdout. *reason* is public
    text; keeping it neutral is the caller's duty. No advisory text is read.
    """
    text = check_close_reason(reason)
    with _step(ghsa_id, "reading the advisories", CloseError):
        advisory = {a.ghsa_id: a for a in list_advisories(home)}.get(ghsa_id)
    if advisory is None:
        raise CloseError(f"advisory {ghsa_id} does not exist in {home}")
    if advisory.external or advisory.state not in _SYNCED_STATES:
        kind = "an external report" if advisory.external else f"in state {advisory.state}"
        raise CloseError(
            f"advisory {ghsa_id} is {kind}; close only moves a draft security record "
            "to closed, and any other state is a human's decision"
        )
    refs = advisory.tracking
    if not refs:
        raise CloseError(
            f"advisory {ghsa_id} tracks no issue, so no issue can carry the close comment"
        )
    current: dict[IssueRef, None] = {}
    notes: list[str] = []
    still_open: list[IssueRef] = []
    for ref in refs:
        with _step(ghsa_id, f"reading {ref.slug}", CloseError):
            raw = _read_issue(ref)
            now = _current_ref(raw)
        if now != ref:
            notes.append(f"{ref.slug} was transferred to {now.slug}: commented there")
        if now not in current and str(raw.get("state") or "").lower() != "closed":
            still_open.append(now)
        current[now] = None
    if still_open:
        names = ", ".join(ref.slug for ref in still_open)
        noun = "issue" if len(still_open) == 1 else "issues"
        raise CloseError(
            f"advisory {ghsa_id}: open {noun} {names}; close or retarget first, since "
            "sync sets an advisory back to draft while any issue it tracks is open"
        )
    comment = close_comment(ghsa_id, text)
    with _step(ghsa_id, "reading the trusted authors", CloseError):
        authors = load_trusted_authors()
    for ref in current:
        endpoint = _issue_endpoint(ref)
        with _step(ghsa_id, f"commenting on {ref.slug}", CloseError):
            existing = trusted_comments.bodies(ref, authors)
            if comment not in existing:
                github.write_json("POST", f"{endpoint}/comments", {"body": comment})
    closing = advisory.state != "closed"
    if closing:
        with _step(ghsa_id, "setting the state to closed", CloseError):
            set_state(home, ghsa_id, "closed")
    return CloseResult(issues=tuple(current), notes=tuple(notes), closed=closing)


# --- note -------------------------------------------------------------------


class NoteError(RuntimeError):
    """A :func:`note_record` refusal or failed step; the message names IDs only."""


def note_header(day: date) -> str:
    """The line a note starts with: ``Note (<YYYY-MM-DD>):``."""
    return f"Note ({day.isoformat()}):"


def check_note(note: str) -> str:
    """*note* with its outer blank lines and every line's trailing spaces dropped.

    :class:`NoteError` when it is empty, or when any of its lines is an exact
    ``Tracking issue:``, ``Moved from:`` or ``Moved to:`` line: the note
    would then change what the record tracks or where it lives, which only
    :func:`retarget` may do. The message never quotes the note.
    """
    text = "\n".join(line.rstrip() for line in note.splitlines()).strip("\n")
    if not text.strip():
        raise NoteError("the note is empty")
    if _TRACKING_RE.search(text):
        raise NoteError(
            "the note has a 'Tracking issue:' line, which would change what the record "
            "tracks; use retarget to move a record's tracking"
        )
    refuse_move_lines(text, "the note", NoteError)
    return text


def refuse_move_lines(text: str, what: str, error: type[Exception]) -> None:
    """Raise *error* when any line of *text* is an exact ``Moved from:`` or ``Moved to:`` line.

    Only a cross-org :func:`retarget` writes those lines. Caller text holding
    one would change where the record lives, or pass for the copy an earlier
    move made. Every writer of advisory text from caller input runs this
    check: :func:`check_note`, and ``vrg-security-record create`` on its
    details file, summary and issue body (with ``--resume`` too). *what*
    names the text in the message, which never quotes it.
    """
    if _MOVED_FROM_RE.search(text) or _MOVED_TO_RE.search(text):
        raise error(
            f"{what} has a 'Moved from:' or 'Moved to:' line, which would change where "
            "the record lives; only retarget writes those lines, when it moves a record "
            "to another org"
        )


def with_note(description: str, note: str, day: date) -> str:
    """*description* with *note* appended under :func:`note_header`.

    The ``Tracking issue:`` lines are kept, in their order, as the last lines,
    so :func:`retarget`, :func:`list_advisories` and the sync read them as
    before.
    """
    return _before_tracking(description, f"{note_header(day)}\n{note}")


def _ends_with_note(description: str, note: str, day: date) -> bool:
    """Whether *description*'s last note, before its tracking lines, is *note* dated *day*.

    Only the last note counts, and only as a whole block, so a new note that
    is merely a prefix of an earlier one, or repeats an older one, is added.
    """
    kept = _without_tracking(description)
    block = f"{note_header(day)}\n{note}"
    return kept == block or kept.endswith(f"\n\n{block}")


def note_record(home: str, ghsa_id: str, note: str, day: date) -> bool:
    """Append *note* to advisory *ghsa_id*'s description in *home*, dated *day*.

    Refuses, changing nothing, when the note is empty or holds a
    ``Tracking issue:`` line (:func:`check_note`), or when the advisory does
    not exist, is an external report or is in a state other than ``draft`` or
    ``closed``. Returns ``False``, writing nothing, when the description's
    last note is already this exact dated note (:func:`_ends_with_note`), so
    a re-run after a lost response never adds it twice; ``True`` once it is
    written.

    Any failure raises :class:`NoteError` naming the advisory and the step,
    scrubbed of ``gh`` stdout. Neither the note nor the description ever
    reaches a message.
    """
    text = check_note(note)
    with _step(ghsa_id, "reading the advisories", NoteError):
        advisory = {a.ghsa_id: a for a in list_advisories(home)}.get(ghsa_id)
    if advisory is None:
        raise NoteError(f"advisory {ghsa_id} does not exist in {home}")
    if advisory.external or advisory.state not in _SYNCED_STATES:
        kind = "an external report" if advisory.external else f"in state {advisory.state}"
        raise NoteError(
            f"advisory {ghsa_id} is {kind}; note only annotates a draft or closed security record"
        )
    with _step(ghsa_id, "reading the description", NoteError):
        _summary, description = read_text(home, ghsa_id)
    if _ends_with_note(description, text, day):
        return False
    with _step(ghsa_id, "writing the note", NoteError):
        _write_description(home, ghsa_id, with_note(description, text, day))
    return True
