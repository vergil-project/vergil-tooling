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
text (:mod:`.plain_text`). The verifier of a past exchange (Task 6b) reads
these lines back.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib.protected_paths import DELETED
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.marker import REQUEST_KINDS, Marker, RequestKind, render_marker
from vergil_tooling.lib.rulings.plain_text import check_plain_text

if TYPE_CHECKING:
    from vergil_tooling.lib.protected_paths import ProtectedChange

__all__ = [
    "DECISION_LINES",
    "REQUEST_KINDS",
    "Decision",
    "RequestKind",
    "parse_decision",
    "render_exemption_request",
    "render_protected_change_request",
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
        lines.append(f"Protected: {change.blob} {path}")
    lines.append(f"Diff: {url}")
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
        f"Reason: {text}",
        f"Fingerprint: {fingerprint}",
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
