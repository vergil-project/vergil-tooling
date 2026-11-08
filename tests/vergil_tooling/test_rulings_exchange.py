"""Tests for ``vergil_tooling.lib.rulings.exchange``."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.exchange import (
    FatalCheck,
    check_respond,
    open_request,
    read_reply,
)
from vergil_tooling.lib.rulings.marker import Marker, render_marker

AGENT = "h-vergil-user"
HUMAN = "h"
REQUEST = render_marker(Marker("request", None, None))


def request(cid: int, *, author: str = AGENT, edited: bool = False) -> IssueComment:
    return IssueComment(cid, author, f"R1: fix or file?\n{REQUEST}", edited)


def reply(cid: int, body: str = "R1: fix", *, edited: bool = False) -> IssueComment:
    return IssueComment(cid, HUMAN, body, edited)


def response(cid: int, request_id: int, reply_id: int) -> IssueComment:
    marker = render_marker(Marker("response", request_id, reply_id))
    return IssueComment(cid, AGENT, f"R1: fixed\n{marker}", False)


def agent_note(cid: int, body: str = "findings") -> IssueComment:
    return IssueComment(cid, AGENT, body, False)


def _read(
    stream: list[IssueComment], request_id: int = 1, extra: tuple[int, ...] = ()
) -> IssueComment | None:
    ids = frozenset(c.id for c in stream) | frozenset(extra)
    return read_reply(stream, request_id=request_id, agent=AGENT, human=HUMAN, all_ids=ids)


def _fatal(
    stream: list[IssueComment],
    check: str,
    comment_id: int,
    *,
    extra: tuple[int, ...] = (),
) -> None:
    with pytest.raises(FatalCheck) as caught:
        _read(stream, extra=extra)
    assert (caught.value.check, caught.value.comment_id) == (check, comment_id)
    assert f"fatal check '{check}' on comment {comment_id}" in str(caught.value)


# --- FatalCheck --------------------------------------------------------------


def test_fatal_check_is_a_ruling_error() -> None:
    err = FatalCheck("turn", None)
    assert isinstance(err, RulingError)
    assert str(err) == "fatal check 'turn'"
    assert str(FatalCheck("edited", 4, "why")) == "fatal check 'edited' on comment 4: why"


# --- read_reply --------------------------------------------------------------


def test_read_reply_no_reply_yet() -> None:
    assert _read([request(1)]) is None
    assert _read([agent_note(0), request(1)]) is None


def test_read_reply_returns_the_reply() -> None:
    assert _read([request(1), reply(2)]) == reply(2)


def test_read_reply_after_a_well_formed_response() -> None:
    assert _read([request(1), reply(2), response(3, 1, 2)]) == reply(2)


def test_read_reply_ignores_earlier_rounds() -> None:
    earlier = [request(1), reply(2), response(3, 1, 2), agent_note(4, "x\n<!-- vrg-ruling bad")]
    assert _read([*earlier, request(5), reply(6)], request_id=5) == reply(6)


def test_read_reply_edited_request() -> None:
    _fatal([request(1, edited=True)], "edited", 1)
    _fatal([request(1, edited=True), reply(2)], "edited", 1)


def test_read_reply_edited_reply() -> None:
    _fatal([request(1), reply(2, edited=True)], "edited", 2)


def test_read_reply_agent_comment_before_reply() -> None:
    _fatal([request(1), agent_note(2), reply(3)], "turn", 2)
    _fatal([request(1), response(2, 1, 1)], "turn", 2)


def test_read_reply_deleted_request() -> None:
    _fatal([reply(2)], "deleted", 1)
    _fatal([], "deleted", 1)


def test_read_reply_request_by_the_human() -> None:
    # A human comment carrying a marker is never a request (Review Focus 4).
    _fatal([request(1, author=HUMAN)], "request-author", 1)
    _fatal([request(1, author=HUMAN), reply(2)], "request-author", 1)


def test_read_reply_request_by_an_untrusted_author() -> None:
    # In all_ids but not in the trusted stream.
    _fatal([reply(2)], "request-author", 1, extra=(1,))


def test_read_reply_deleted_untrusted_comment_is_not_fatal() -> None:
    # Epic 889 spec §12: the untrusted comment 2, posted after the request and
    # then deleted, is in neither the trusted stream nor all_ids.
    stream = [request(1), reply(3)]
    assert _read(stream) == reply(3)
    ids = frozenset(c.id for c in stream)
    assert (
        check_respond(stream, request_id=1, reply_id=3, agent=AGENT, human=HUMAN, all_ids=ids)
        is None
    )


def test_read_reply_request_without_a_request_marker() -> None:
    _fatal([agent_note(1), reply(2)], "request-author", 1)
    _fatal([response(1, 7, 8), reply(2)], "request-author", 1)
    _fatal([request(1, author="h-vergil-audit"), reply(2)], "request-author", 1)


def test_read_reply_request_with_a_malformed_marker() -> None:
    _fatal([agent_note(1, "q\n<!-- vrg-ruling kind=requests -->"), reply(2)], "marker", 1)


def test_read_reply_second_human_comment() -> None:
    _fatal([request(1), reply(2), reply(3, "R1: file")], "turn", 3)


def test_read_reply_agent_comment_without_a_response_marker() -> None:
    _fatal([request(1), reply(2), agent_note(3)], "turn", 3)


def test_read_reply_malformed_marker_after_the_reply() -> None:
    _fatal([request(1), reply(2), agent_note(3, "done\n<!-- vrg-ruling kind=x -->")], "marker", 3)


def test_read_reply_response_naming_another_exchange() -> None:
    _fatal([request(1), reply(2), response(3, 1, 99)], "turn", 3)
    _fatal([request(1), reply(2), response(3, 99, 2)], "turn", 3)


def test_read_reply_comment_after_the_response() -> None:
    _fatal([request(1), reply(2), response(3, 1, 2), agent_note(4)], "turn", 4)
    _fatal([request(1), reply(2), response(3, 1, 2), reply(4)], "turn", 4)


@pytest.mark.parametrize(
    "body",
    ["fix‮", "x\n<!-- vrg-ruling kind=request -->", "R1:\tfix", "R1: fix\r\n", "café"],
)
def test_read_reply_reply_not_plain_text(body: str) -> None:
    with pytest.raises(FatalCheck) as caught:
        _read([request(1), reply(2, body)])
    assert (caught.value.check, caught.value.comment_id) == ("reply-text", 2)
    assert "not plain text" in str(caught.value) or "marker prefix" in str(caught.value)


def test_read_reply_human_marker_in_reply_is_reply_text() -> None:
    marker = render_marker(Marker("response", 1, 2))
    _fatal([request(1), reply(2, f"ok\n{marker}")], "reply-text", 2)


# --- check_respond -----------------------------------------------------------


def _respond(stream: list[IssueComment], reply_id: int = 2) -> int | None:
    ids = frozenset(c.id for c in stream)
    return check_respond(
        stream, request_id=1, reply_id=reply_id, agent=AGENT, human=HUMAN, all_ids=ids
    )


def _respond_fatal(
    stream: list[IssueComment], check: str, comment_id: int, reply_id: int = 2
) -> None:
    with pytest.raises(FatalCheck) as caught:
        _respond(stream, reply_id)
    assert (caught.value.check, caught.value.comment_id) == (check, comment_id)


def test_check_respond_ready() -> None:
    assert _respond([request(1), reply(2)]) is None


def test_check_respond_already_responded() -> None:
    assert _respond([request(1), reply(2), response(3, 1, 2)]) == 3


def test_check_respond_second_human_comment() -> None:
    _respond_fatal([request(1), reply(2), reply(3)], "turn", 3)


def test_check_respond_reply_deleted() -> None:
    _respond_fatal([request(1)], "deleted", 2)


def test_check_respond_reply_is_another_comment() -> None:
    _respond_fatal([request(1), reply(5)], "deleted", 2)


def test_check_respond_response_naming_another_reply() -> None:
    _respond_fatal([request(1), reply(2), response(3, 1, 9)], "turn", 3)


def test_check_respond_runs_the_read_checks() -> None:
    _respond_fatal([request(1, edited=True), reply(2)], "edited", 1)
    _respond_fatal([request(1), reply(2, edited=True)], "edited", 2)
    _respond_fatal([reply(2)], "deleted", 1)
    _respond_fatal([request(1), reply(2, "fix​")], "reply-text", 2)


# --- open_request ------------------------------------------------------------


def test_open_request() -> None:
    assert open_request([], AGENT) is None
    assert open_request([request(1)], AGENT) == 1
    assert open_request([request(1), reply(2)], AGENT) == 1
    assert open_request([request(1), reply(2), response(3, 1, 2)], AGENT) is None


def test_open_request_ignores_an_earlier_malformed_marker() -> None:
    stream = [agent_note(1, "old\n<!-- vrg-ruling kind=nope -->"), request(5)]
    assert open_request(stream, AGENT) == 5


def test_open_request_finds_the_latest_unanswered() -> None:
    done = [request(1), reply(2), response(3, 1, 2)]
    assert open_request([*done, agent_note(4), request(5)], AGENT) == 5
    assert open_request([*done, request(5), reply(6), response(7, 5, 6)], AGENT) is None


def test_open_request_ignores_markers_not_by_the_agent() -> None:
    # A human comment with a request marker, even first on the issue, is no
    # request (Review Focus 4).
    assert open_request([request(1, author=HUMAN)], AGENT) is None
    assert open_request([request(1), response(2, 1, 9)], AGENT) is None
    human_response = IssueComment(3, HUMAN, render_marker(Marker("response", 1, 2)), False)
    assert open_request([request(1), reply(2), human_response], AGENT) == 1
