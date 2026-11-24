"""The hidden end-of-body marker that says what kind of comment a comment is.

A request ends with ``<!-- vrg-ruling kind=request -->`` and a response with
``<!-- vrg-ruling kind=response request=<id> reply=<id> -->``. A request of
the kind whose reply carries a structured decision (epic
vergil-project/.github#1384, spec §4.6) names its kind with a ``type``
attribute: ``<!-- vrg-ruling kind=request type=merge-review-exemption -->``. A
request with no ``type`` is an ordinary ruling request, as every earlier
request is.
A marker counts only when:

- the comment's author is the derived agent login (a marker anywhere else,
  including in a comment by the human, is ignored);
- it is the body's last line, after trailing whitespace is stripped;
- it is the only line in the body that starts with :data:`MARKER_PREFIX`;
- it matches one of the exact forms above.

A prefix line in any other position is plain text, never an error. An agent
comment whose sole prefix line is its last line but does not match an exact
form (an unknown ``type`` included) has a *malformed* marker:
:func:`parse_marker` returns ``None`` for it and :func:`malformed_marker`
reports it. Only the turn checks (``exchange``) turn that into a fatal check,
and only for the comments of the exchange in hand.

The marker never carries identity: the comment's author is the only source of
who wrote it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib.rulings import RulingError

if TYPE_CHECKING:
    from vergil_tooling.lib.rulings.comments import IssueComment

MARKER_PREFIX = "<!-- vrg-ruling"

#: The kind of a request whose reply carries a structured decision.
type RequestKind = Literal["merge-review-exemption"]

#: Every :data:`RequestKind`, by its text in the marker.
REQUEST_KINDS: dict[str, RequestKind] = {
    "merge-review-exemption": "merge-review-exemption",
}

_ID = "[1-9][0-9]*"
_REQUEST_RE = re.compile(r"<!-- vrg-ruling kind=request(?: type=([a-z-]+))? -->")
_RESPONSE_RE = re.compile(rf"<!-- vrg-ruling kind=response request=({_ID}) reply=({_ID}) -->")


@dataclass(frozen=True)
class Marker:
    """A parsed marker: a request, or a response naming its request and reply.

    ``type`` is set only on a request of a :data:`RequestKind`; an ordinary
    request and every response carry none.
    """

    kind: Literal["request", "response"]
    request_id: int | None
    reply_id: int | None
    type: RequestKind | None = None


def render_marker(marker: Marker) -> str:
    """The marker's one line, in its exact form.

    Refuses a request carrying comment IDs or a type that is not a
    :data:`RequestKind`, and a response carrying a type, missing either ID or
    naming one that is not a positive integer.
    """
    if marker.kind == "request":
        if marker.request_id is not None or marker.reply_id is not None:
            msg = f"a request marker carries no comment IDs, got {marker!r}"
            raise RulingError(msg)
        if marker.type is None:
            return "<!-- vrg-ruling kind=request -->"
        if marker.type not in REQUEST_KINDS:
            msg = (
                f"a request marker's type must be one of {', '.join(REQUEST_KINDS)}, got {marker!r}"
            )
            raise RulingError(msg)
        return f"<!-- vrg-ruling kind=request type={marker.type} -->"
    if marker.type is not None:
        msg = f"a response marker carries no type, got {marker!r}"
        raise RulingError(msg)
    request_id, reply_id = marker.request_id, marker.reply_id
    if request_id is None or reply_id is None or request_id < 1 or reply_id < 1:
        msg = f"a response marker needs positive request and reply IDs, got {marker!r}"
        raise RulingError(msg)
    return f"<!-- vrg-ruling kind=response request={request_id} reply={reply_id} -->"


def _marker_line(comment: IssueComment, agent: str) -> str | None:
    """The comment's marker line, well-formed or not, or ``None`` when it has none.

    A comment has a marker line only when it is by ``agent``, its last line
    starts with :data:`MARKER_PREFIX` and no other line does.
    """
    if not agent or comment.author != agent:
        return None
    lines = comment.body.rstrip().split("\n")
    if not lines[-1].startswith(MARKER_PREFIX):
        return None
    if sum(1 for line in lines if line.startswith(MARKER_PREFIX)) != 1:
        return None
    return lines[-1]


def _parse_line(line: str) -> Marker | None:
    match = _REQUEST_RE.fullmatch(line)
    if match is not None:
        text = match.group(1)
        if text is None:
            return Marker("request", None, None)
        kind = REQUEST_KINDS.get(text)
        return None if kind is None else Marker("request", None, None, kind)
    match = _RESPONSE_RE.fullmatch(line)
    if match is not None:
        return Marker("response", int(match.group(1)), int(match.group(2)))
    return None


def parse_marker(comment: IssueComment, agent: str) -> Marker | None:
    """The comment's well-formed marker, or ``None``. Never raises."""
    line = _marker_line(comment, agent)
    return None if line is None else _parse_line(line)


def malformed_marker(comment: IssueComment, agent: str) -> bool:
    """Whether the comment has a marker line that matches no exact form."""
    line = _marker_line(comment, agent)
    return line is not None and _parse_line(line) is None
