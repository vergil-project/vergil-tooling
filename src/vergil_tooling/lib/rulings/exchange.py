"""The turn checks over an issue's trusted comment stream.

Every ``stream`` here is the output of
:func:`vergil_tooling.lib.rulings.comments.trusted`: the comments by a trusted
login, oldest first. The issue is single-threaded. After a request, the next
trusted comment must be the human's reply; after the reply, the next must be
the agent's response. Anything else in either gap is a turn violation. An
edited request or reply is fatal, and so is a request or reply that is gone.
The checks cover only the exchange in hand: earlier rounds are never re-checked.

Every fatal check raises :class:`FatalCheck`, naming the check and the comment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.marker import Marker, malformed_marker, parse_marker
from vergil_tooling.lib.rulings.plain_text import check_plain_text

if TYPE_CHECKING:
    from vergil_tooling.lib.rulings.comments import IssueComment


class FatalCheck(RulingError):  # noqa: N818 - the name is the plan's interface
    """A failed turn check: the run stops, and the human triages the issue."""

    def __init__(self, check: str, comment_id: int | None, detail: str = "") -> None:
        self.check = check
        self.comment_id = comment_id
        where = "" if comment_id is None else f" on comment {comment_id}"
        message = f"fatal check '{check}'{where}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def _index(stream: list[IssueComment], comment_id: int) -> int | None:
    for index, comment in enumerate(stream):
        if comment.id == comment_id:
            return index
    return None


def read_reply(
    stream: list[IssueComment],
    *,
    request_id: int,
    agent: str,
    human: str,
    all_ids: frozenset[int],
) -> IssueComment | None:
    """The human's reply to request ``request_id``, or ``None`` when there is none yet.

    ``all_ids`` holds every comment ID on the issue, trusted or not, so a
    request that exists but is not by a trusted login is told apart from one
    that was deleted. Nothing may follow the reply except the agent's one
    well-formed response naming this request and reply.
    """
    index = _index(stream, request_id)
    if index is None:
        if request_id in all_ids:
            raise FatalCheck("request-author", request_id, "not posted by a trusted login")
        raise FatalCheck("deleted", request_id, "the request is not on the issue")
    request = stream[index]
    if malformed_marker(request, agent):
        raise FatalCheck("marker", request_id, "the request's marker is malformed")
    marker = parse_marker(request, agent)
    if request.author != agent or marker is None or marker.kind != "request":
        raise FatalCheck(
            "request-author", request_id, f"not a request posted by the agent login {agent}"
        )
    if request.edited:
        raise FatalCheck("edited", request_id, "the request has been edited")
    after = stream[index + 1 :]
    if not after:
        return None
    reply = after[0]
    if reply.author != human:
        raise FatalCheck(
            "turn", reply.id, f"the first trusted comment after the request is not by {human}"
        )
    if reply.edited:
        raise FatalCheck("edited", reply.id, "the reply has been edited")
    try:
        check_plain_text(reply.body, what=f"reply {reply.id}")
    except RulingError as exc:
        raise FatalCheck("reply-text", reply.id, str(exc)) from exc
    rest = after[1:]
    if not rest:
        return reply
    following = rest[0]
    if malformed_marker(following, agent):
        raise FatalCheck(
            "marker", following.id, "the comment after the reply has a malformed marker"
        )
    if parse_marker(following, agent) != Marker("response", request_id, reply.id):
        raise FatalCheck(
            "turn",
            following.id,
            f"the comment after the reply is not the response to request {request_id}"
            f" and reply {reply.id}",
        )
    if len(rest) > 1:
        raise FatalCheck("turn", rest[1].id, "a trusted comment follows the response")
    return reply


def check_respond(
    stream: list[IssueComment],
    *,
    request_id: int,
    reply_id: int,
    agent: str,
    human: str,
    all_ids: frozenset[int],
) -> int | None:
    """Whether the response to ``request_id`` and ``reply_id`` can be posted.

    Returns ``None`` when the reply is the latest trusted comment (ready to
    respond), or the ID of the agent's well-formed response that already
    landed after it. Runs every check :func:`read_reply` runs first.
    """
    reply = read_reply(stream, request_id=request_id, agent=agent, human=human, all_ids=all_ids)
    if reply is None or reply.id != reply_id:
        raise FatalCheck("deleted", reply_id, f"reply {reply_id} is not the reply to the request")
    # read_reply has already refused anything after the reply but the one
    # well-formed response naming this request and reply.
    after = stream[stream.index(reply) + 1 :]
    return after[0].id if after else None


def open_request(stream: list[IssueComment], agent: str) -> int | None:
    """The latest request by ``agent`` that no later response names, or ``None``.

    Malformed markers are ignored here: they belong to no exchange in hand.
    """
    answered: set[int] = set()
    for comment in reversed(stream):
        marker = parse_marker(comment, agent)
        if marker is None:
            continue
        if marker.kind == "response" and marker.request_id is not None:
            answered.add(marker.request_id)
        elif marker.kind == "request" and comment.id not in answered:
            return comment.id
    return None
