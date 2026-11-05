"""Tests for ``vergil_tooling.lib.rulings.marker``."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.marker import (
    MARKER_PREFIX,
    Marker,
    malformed_marker,
    parse_marker,
    render_marker,
)

AGENT = "h-vergil-user"


def _agent(body: str) -> IssueComment:
    return IssueComment(1, AGENT, body, False)


def test_render_marker_exact_forms() -> None:
    assert render_marker(Marker("request", None, None)) == "<!-- vrg-ruling kind=request -->"
    assert (
        render_marker(Marker("response", 4300000001, 12))
        == "<!-- vrg-ruling kind=response request=4300000001 reply=12 -->"
    )
    assert render_marker(Marker("request", None, None)).startswith(MARKER_PREFIX)


@pytest.mark.parametrize(
    "marker",
    [
        Marker("request", 1, None),
        Marker("request", None, 2),
        Marker("response", None, 2),
        Marker("response", 1, None),
        Marker("response", 0, 2),
        Marker("response", 1, -2),
    ],
)
def test_render_marker_refuses_inconsistent_ids(marker: Marker) -> None:
    with pytest.raises(RulingError, match="marker"):
        render_marker(marker)


def test_marker_round_trip() -> None:
    for m in (Marker("response", 11, 12), Marker("request", None, None)):
        c = _agent("text\n" + render_marker(m))
        assert parse_marker(c, AGENT) == m
        assert malformed_marker(c, AGENT) is False


def test_marker_alone_and_with_trailing_whitespace() -> None:
    line = render_marker(Marker("request", None, None))
    assert parse_marker(_agent(line), AGENT) == Marker("request", None, None)
    assert parse_marker(_agent(f"x\r\n{line}\r\n \n"), AGENT) == Marker("request", None, None)


def test_quoted_marker_is_plain_text() -> None:
    quoted = render_marker(Marker("request", None, None))
    mid = IssueComment(1, AGENT, f"findings\n{quoted}\nmore", False)
    fenced = IssueComment(2, AGENT, f"```\n{quoted}\n```", False)
    two = IssueComment(3, AGENT, f"{quoted}\n{quoted}", False)
    for c in (mid, fenced, two):
        assert parse_marker(c, AGENT) is None
        assert malformed_marker(c, AGENT) is False


def test_malformed_marker_is_not_parsed() -> None:
    c = _agent("x\n<!-- vrg-ruling kind=bogus -->")
    assert parse_marker(c, AGENT) is None
    assert malformed_marker(c, AGENT) is True


@pytest.mark.parametrize(
    "line",
    [
        "<!-- vrg-ruling kind=request-->",
        "<!-- vrg-ruling  kind=request -->",
        "<!-- vrg-ruling kind=request --> x",
        "<!-- vrg-ruling kind=response -->",
        "<!-- vrg-ruling kind=response request=1 -->",
        "<!-- vrg-ruling kind=response request=01 reply=2 -->",
        "<!-- vrg-ruling kind=response request=0 reply=2 -->",
        "<!-- vrg-ruling kind=response request=1 reply=2 extra=3 -->",
        "<!-- vrg-ruling kind=response reply=2 request=1 -->",
        "<!-- vrg-ruling kind=response request=١ reply=2 -->",
        "<!-- vrg-ruling kind=request request=1 reply=2 -->",
        "<!-- vrg-ruling",
    ],
)
def test_near_miss_markers_are_malformed(line: str) -> None:
    c = _agent(f"x\n{line}")
    assert parse_marker(c, AGENT) is None
    assert malformed_marker(c, AGENT) is True


def test_parse_marker_never_raises_on_odd_bodies() -> None:
    for body in ("", "\n\n", "   ", "‮<!-- vrg-ruling kind=request -->", "\x00"):
        c = _agent(body)
        assert parse_marker(c, AGENT) is None
        assert malformed_marker(c, AGENT) is False


def test_marker_ignored_unless_by_agent() -> None:
    body = render_marker(Marker("request", None, None))
    assert parse_marker(IssueComment(1, "h", body, False), AGENT) is None
    assert malformed_marker(IssueComment(1, "h", "<!-- vrg-ruling bogus", False), AGENT) is False
    assert parse_marker(IssueComment(1, "", body, False), "") is None
    assert parse_marker(IssueComment(1, "h-vergil-audit", body, False), AGENT) is None
