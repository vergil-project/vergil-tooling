"""The two request kinds whose reply carries a structured decision (epic .github#1384).

A **protected-change approval** (spec §4.5) and a **merge-review exemption**
(spec §4.6) are rulings in the same authenticated exchange as every other:
the agent posts the request, the human replies with ``vrg-ruling-reply`` as
the human, and the agent closes the turn with ``vrg-ruling respond``. They
differ from an ordinary ruling in two ways:

- the request's marker names its kind
  (``<!-- vrg-ruling kind=request type=<kind> -->``, see :mod:`.marker`), and
  its body records exactly what an approval covers, one fact per line, so a
  later reader can take it from the request alone;
- the human's reply carries exactly one decision line, ``Decision: approve``
  or ``Decision: refuse`` (:data:`DECISION_LINES`), which
  :func:`parse_decision` reads strictly. Anything else is not a decision.

**The protected-change request** lists every protected path the change
touches, one ``Protected: <blob> <path>`` line each, the blob being the
file's git object ID at the head (``deleted`` for a deletion), then one
``Diff: <url>`` line linking the change. **The exemption request** carries
one ``Reason: <text>`` line and one ``Fingerprint: <sha256>`` line, the
change fingerprint of :mod:`vergil_tooling.lib.change_fingerprint`. The blob
comes first on its line so a path, which may hold spaces, is the rest of the
line.

Every value is checked before anything is rendered, and every line is plain
text (:mod:`.plain_text`).

**Verifying a past exchange** (spec §4.5). The turn checks of
:mod:`.exchange` judge the exchange in hand, and treat any trusted comment
after its response as fatal. A task issue always gains later comments, so
submit, the watch and the audit read an approval already given with
:func:`verified_approvals` instead: it reads each request's lines back,
checks the request and the first trusted comment after it, and ignores
everything after that reply.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib.protected_paths import DELETED, ProtectedChange
from vergil_tooling.lib.rulings import RulingError, comments
from vergil_tooling.lib.rulings.identity import expected_agent_login, human_login
from vergil_tooling.lib.rulings.marker import (
    REQUEST_KINDS,
    Marker,
    RequestKind,
    parse_marker,
    render_marker,
)
from vergil_tooling.lib.rulings.plain_text import check_plain_text

if TYPE_CHECKING:
    from vergil_tooling.lib.rulings.comments import IssueComment
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

__all__ = [
    "DECISION_LINES",
    "REQUEST_KINDS",
    "Approval",
    "Decision",
    "RequestKind",
    "parse_decision",
    "render_exemption_request",
    "render_protected_change_request",
    "verified_approvals",
]

type Decision = Literal["approve", "refuse"]

#: The two decision lines a reply to either kind may carry, by their decision.
DECISION_LINES: dict[str, Decision] = {
    "Decision: approve": "approve",
    "Decision: refuse": "refuse",
}

#: What every request of either kind tells the human to reply.
REPLY_INSTRUCTION = "Reply with exactly one decision line: Decision: approve, or Decision: refuse."

_DECISION_PREFIX = "decision:"
_PROTECTED_PREFIX = "Protected: "
_DIFF_PREFIX = "Diff: "
_REASON_PREFIX = "Reason: "
_FINGERPRINT_PREFIX = "Fingerprint: "
_BLOB_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


def _one_line(value: str, *, what: str) -> str:
    """A non-empty single line of plain text with no surrounding whitespace."""
    if not value.strip():
        msg = f"{what} is empty"
        raise RulingError(msg)
    check_plain_text(value, what=what)
    if "\n" in value:
        msg = f"{what} must be one line"
        raise RulingError(msg)
    if value != value.strip():
        msg = f"{what} has leading or trailing whitespace"
        raise RulingError(msg)
    return value


def _body(lines: list[str], kind: RequestKind) -> str:
    """The lines, the reply instruction, a blank line, then the typed request marker."""
    return "\n".join(
        [*lines, REPLY_INSTRUCTION, "", render_marker(Marker("request", None, None, kind))]
    )


def render_protected_change_request(changes: list[ProtectedChange], diff_url: str) -> str:
    """The protected-change request: each protected path with its head blob, and the diff.

    Refuses an empty list (there is nothing to approve), a path listed twice,
    a blob that is neither a git object ID nor ``deleted``, and a path or URL
    that is not one line of plain text.
    """
    if not changes:
        msg = "the change touches no protected path; there is nothing to approve"
        raise RulingError(msg)
    url = _one_line(diff_url, what="the diff link")
    if not url.startswith("https://"):
        msg = "the diff link must be an https:// URL"
        raise RulingError(msg)
    lines = [
        "Protected-change approval requested. This change edits protected files;"
        " an approve covers exactly these files at exactly these blobs:"
    ]
    seen: set[str] = set()
    for change in changes:
        path = _one_line(change.path, what="a protected path")
        if path in seen:
            msg = f"the protected path {path} is listed twice"
            raise RulingError(msg)
        seen.add(path)
        if change.blob != DELETED and _BLOB_RE.fullmatch(change.blob) is None:
            msg = f"the blob of {path} is neither a git object ID nor {DELETED!r}"
            raise RulingError(msg)
        lines.append(f"{_PROTECTED_PREFIX}{change.blob} {path}")
    lines.append(f"{_DIFF_PREFIX}{url}")
    return _body(lines, "protected-change")


def render_exemption_request(reason: str, fingerprint: str) -> str:
    """The merge-review exemption request: the reason, and the fingerprint it covers.

    Refuses a reason that is not one line of plain text, and a fingerprint
    that is not a lowercase SHA-256 hex digest.
    """
    text = _one_line(reason, what="the exemption reason")
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        msg = "the change fingerprint must be a lowercase SHA-256 hex digest"
        raise RulingError(msg)
    lines = [
        "Merge-review exemption requested. An approve stands in for merge review's"
        " closing comment for the change with this fingerprint only; it never waives"
        " a protected-change approval.",
        f"{_REASON_PREFIX}{text}",
        f"{_FINGERPRINT_PREFIX}{fingerprint}",
    ]
    return _body(lines, "merge-review-exemption")


def parse_decision(body: str) -> Decision:
    """The decision a reply carries: its one exact ``Decision:`` line.

    Every line that starts with ``decision:`` in any letter case, after
    leading whitespace, is a decision line. Refuses with
    :class:`RulingError` unless there is exactly one, and it is exactly a
    line of :data:`DECISION_LINES` (no other word, case or spacing).
    """
    candidates = [
        line for line in body.split("\n") if line.lstrip().lower().startswith(_DECISION_PREFIX)
    ]
    if not candidates:
        msg = "the reply has no decision line: Decision: approve, or Decision: refuse"
        raise RulingError(msg)
    if len(candidates) > 1:
        msg = f"the reply has {len(candidates)} decision lines; it must have exactly one"
        raise RulingError(msg)
    decision = DECISION_LINES.get(candidates[0])
    if decision is None:
        msg = "the reply's decision line is not exactly 'Decision: approve' or 'Decision: refuse'"
        raise RulingError(msg)
    return decision


# --- verifying a past exchange (Task 6b) -----------------------------------------


@dataclass(frozen=True)
class Approval:
    """One verified, approving exchange: what the request recorded, by its comment ID.

    ``blobs`` maps each protected path a ``protected-change`` request listed
    to its blob (``deleted`` for a deletion), and is empty for an exemption.
    ``fingerprint`` is the change fingerprint an exemption covers, and
    ``None`` for a protected-change approval. ``decision`` is always
    ``approve``: :func:`verified_approvals` returns no other.
    """

    kind: RequestKind
    request_id: int
    decision: str
    blobs: dict[str, str]
    fingerprint: str | None


def _values(lines: list[str], prefix: str) -> list[str]:
    return [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]


def _only(values: list[str], what: str) -> str:
    if len(values) != 1:
        msg = f"the request has {len(values)} {what} lines; it must have exactly one"
        raise RulingError(msg)
    return values[0]


def _request_facts(body: str, kind: RequestKind) -> tuple[dict[str, str], str | None]:
    """The blobs and fingerprint a request of *kind* records, read back from its *body*.

    The body must be exactly what :func:`render_protected_change_request` or
    :func:`render_exemption_request` renders from the values its lines carry
    (trailing whitespace aside), so a request in any other form records
    nothing. Refuses with :class:`RulingError`.
    """
    text = body.rstrip()
    lines = text.split("\n")
    if kind == "protected-change":
        changes: list[ProtectedChange] = []
        for value in _values(lines, _PROTECTED_PREFIX):
            blob, sep, path = value.partition(" ")
            if not sep:
                msg = f"the protected line {value!r} has no path"
                raise RulingError(msg)
            changes.append(ProtectedChange(path, blob))
        rendered = render_protected_change_request(
            changes, _only(_values(lines, _DIFF_PREFIX), "Diff:")
        )
        blobs = {change.path: change.blob for change in changes}
        fingerprint = None
    else:
        reason = _only(_values(lines, _REASON_PREFIX), "Reason:")
        fingerprint = _only(_values(lines, _FINGERPRINT_PREFIX), "Fingerprint:")
        rendered = render_exemption_request(reason, fingerprint)
        blobs = {}
    if rendered != text:
        msg = f"the request is not in the {kind} request's form"
        raise RulingError(msg)
    return blobs, fingerprint


def _judge(
    stream: list[IssueComment], index: int, kind: RequestKind, human: str
) -> Approval | None:
    """The approval the request at ``stream[index]`` holds.

    ``None`` when it has no reply yet or the human refused it. Refuses with
    :class:`RulingError`, naming why, on anything else that holds no
    approval: an edited request, a body not in its kind's form, a first
    trusted comment after it that is not the human's unedited plain-text
    reply, or a reply without one exact decision line.
    """
    request = stream[index]
    if request.edited:
        msg = "the request has been edited"
        raise RulingError(msg)
    blobs, fingerprint = _request_facts(request.body, kind)
    after = stream[index + 1 :]
    if not after:
        return None
    reply = after[0]
    if reply.author != human:
        msg = f"the first trusted comment after it, {reply.id}, is not by {human}"
        raise RulingError(msg)
    if reply.edited:
        msg = f"the reply {reply.id} has been edited"
        raise RulingError(msg)
    check_plain_text(reply.body, what=f"the reply {reply.id}")
    decision = parse_decision(reply.body)
    if decision != "approve":
        return None
    return Approval(kind, request.id, decision, blobs, fingerprint)


def verified_approvals(issue_ref: str, *, authors: TrustedAuthors) -> list[Approval]:
    """Every verified, approving exchange of either kind on *issue_ref*, oldest first.

    Built on the trust layer's comment stream
    (:func:`~vergil_tooling.lib.rulings.comments.read_stream`, the human and
    the agents). A request of either kind counts only when the agent login
    (:func:`~vergil_tooling.lib.rulings.identity.expected_agent_login`)
    posted it, with its typed marker, its body is exactly its kind's form,
    and the first trusted comment after it is the human login's reply,
    plain text with one ``Decision: approve`` line (:func:`parse_decision`).
    Neither may be edited; a deleted one is not on the stream. A comment the
    stream leaves out (the automation App's, an outsider's, or one edited by
    either) is never the reply, and comments after the reply are ignored, so
    a later trusted comment never voids or makes an approval.

    Each request that holds no approval, other than one with no reply yet or
    one the human refused, is reported on stderr with the reason. A read
    failure raises (:class:`~vergil_tooling.lib.trust.TrustReadError`), as
    does a misconfigured login (:class:`RulingError`); it never returns
    ``[]``.
    """
    repo, issue = comments.parse_issue_ref(issue_ref)
    agent = expected_agent_login(authors)
    human = human_login(authors)
    stream = comments.read_stream(repo, issue, authors).comments
    found: list[Approval] = []
    for index, comment in enumerate(stream):
        marker = parse_marker(comment, agent)
        if marker is None or marker.kind != "request" or marker.type is None:
            continue
        try:
            approval = _judge(stream, index, marker.type, human)
        except RulingError as exc:
            print(
                f"{issue_ref}: {marker.type} request {comment.id} is not approved: {exc}",
                file=sys.stderr,
            )
            continue
        if approval is not None:
            found.append(approval)
    return found
