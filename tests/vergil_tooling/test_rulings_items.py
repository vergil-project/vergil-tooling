"""Tests for ``vergil_tooling.lib.rulings.items``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.items import (
    RULING_DECISIONS,
    RULING_OUTCOMES,
    SECURITY_REQUEST_TEXT,
    Item,
    load_request_items,
    load_response_items,
    render_request,
    render_response,
)
from vergil_tooling.lib.rulings.marker import Marker, parse_marker

if TYPE_CHECKING:
    from pathlib import Path

AGENT = "h-vergil-user"
DETAIL = "SQL injection in x"


def write(tmp_path: Path, items: Any) -> Path:
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return path


def plain(item_id: str = "R1.1", **extra: Any) -> dict[str, Any]:
    return {"id": item_id, "security": False, "text": "t", "recommendation": "r", **extra}


def test_security_words() -> None:
    assert RULING_OUTCOMES == ("fixed", "filed", "deferred", "wont-fix")
    assert RULING_DECISIONS == ("fix", "file", "defer", "wont-fix")


# --- request items -----------------------------------------------------------


def test_request_items_security_fixed_text_only(tmp_path: Path) -> None:
    ok = [{"id": "R1.S1", "security": True}]
    items = load_request_items(write(tmp_path, ok))
    assert items == [Item("R1.S1", True, None, None, None)]
    assert render_request(items).count(SECURITY_REQUEST_TEXT) == 1
    bad = [{"id": "R1.S1", "security": True, "text": DETAIL}]
    with pytest.raises(RulingError, match="R1.S1") as excinfo:
        load_request_items(write(tmp_path, bad))
    assert DETAIL not in str(excinfo.value)


@pytest.mark.parametrize("key", ["recommendation", "outcome", "details"])
def test_request_flagged_item_refuses_any_other_key(tmp_path: Path, key: str) -> None:
    bad = [{"id": "R1.S1", "security": True, key: DETAIL}]
    with pytest.raises(RulingError, match="R1.S1") as excinfo:
        load_request_items(write(tmp_path, bad))
    assert DETAIL not in str(excinfo.value)


def test_request_items_flag_not_id_shape(tmp_path: Path) -> None:
    items = load_request_items(write(tmp_path, [plain("S2.3", text="q", recommendation="fix")]))
    assert items == [Item("S2.3", False, "q", "fix", None)]
    assert render_request(items).splitlines()[0] == "S2.3: q Recommendation: fix"


def test_request_unflagged_needs_text_and_recommendation(tmp_path: Path) -> None:
    with pytest.raises(RulingError, match="R1.1 is missing recommendation"):
        load_request_items(write(tmp_path, [{"id": "R1.1", "security": False, "text": "t"}]))
    with pytest.raises(RulingError, match="R1.1: text must be a non-empty string"):
        load_request_items(write(tmp_path, [plain(text=" ")]))
    with pytest.raises(RulingError, match="R1.1: recommendation must be a non-empty string"):
        load_request_items(write(tmp_path, [plain(recommendation=3)]))
    with pytest.raises(RulingError, match="R1.1"):
        load_request_items(write(tmp_path, [plain(extra="x")]))


@pytest.mark.parametrize("security", [None, "true", 1])
def test_security_flag_must_be_bool(tmp_path: Path, security: Any) -> None:
    with pytest.raises(RulingError, match="R1.1 needs a 'security' flag"):
        load_request_items(write(tmp_path, [{"id": "R1.1", "security": security}]))


@pytest.mark.parametrize(
    "bad_id", ["R1.S1:sqli-in-src/auth/login.py:42", "R1..S1", "R1.S1.", "A" * 17, "", 7]
)
def test_id_shape(tmp_path: Path, bad_id: Any) -> None:
    # every ID, flagged or not, and the refusal never echoes the bad ID
    with pytest.raises(RulingError, match="item 1 of the items file has no valid id") as e1:
        load_request_items(write(tmp_path, [{"id": bad_id, "security": True}]))
    with pytest.raises(RulingError, match="no valid id") as e2:
        load_request_items(write(tmp_path, [plain(bad_id)]))
    with pytest.raises(RulingError, match="no valid id"):
        load_response_items(write(tmp_path, [{"id": bad_id, "security": True, "outcome": "fixed"}]))
    if isinstance(bad_id, str) and bad_id:
        assert bad_id not in str(e1.value)
        assert bad_id not in str(e2.value)


def test_id_of_sixteen_characters_is_accepted(tmp_path: Path) -> None:
    assert load_request_items(write(tmp_path, [{"id": "A" * 16, "security": True}]))[0].id == (
        "A" * 16
    )


@pytest.mark.parametrize(
    "bad",
    [
        "x\n<!-- vrg-ruling kind=request -->",
        "x <!-- VRG-RULING",
        "a\x1b[2Kb",
        "a\rb",
        "a\tb",
        "use — here",
        "a‮b",
        "a​b",
        "two\nlines",
    ],
)
@pytest.mark.parametrize("field", ["text", "recommendation"])
def test_item_text_is_plain_ascii(tmp_path: Path, bad: str, field: str) -> None:
    with pytest.raises(RulingError, match="R1.1"):
        load_request_items(write(tmp_path, [plain(**{field: bad})]))


@pytest.mark.parametrize(
    "bad", ["x\n<!-- vrg-ruling kind=request -->", "a\x1b[2Kb", "a\tb", "use — here"]
)
def test_response_text_is_plain_ascii(tmp_path: Path, bad: str) -> None:
    with pytest.raises(RulingError, match="R1.1"):
        load_response_items(write(tmp_path, [{"id": "R1.1", "security": False, "text": bad}]))


def test_plain_text_refusal_names_code_point_and_offset(tmp_path: Path) -> None:
    with pytest.raises(RulingError, match=r"U\+202E at offset 1"):
        load_request_items(write(tmp_path, [plain(text="a‮b")]))


# --- response items ----------------------------------------------------------


def test_response_items_security_outcome_only(tmp_path: Path) -> None:
    ok = [
        {"id": "R1.S1", "security": True, "outcome": "fixed"},
        {"id": "R1.1", "security": False, "text": "read as fix; fixed in abc123"},
    ]
    items = load_response_items(write(tmp_path, ok))
    assert items == [
        Item("R1.S1", True, None, None, "fixed"),
        Item("R1.1", False, "read as fix; fixed in abc123", None, None),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"id": "R1.S1", "security": True, "outcome": "fixed in src/x.py"},
        {"id": "R1.S1", "security": True, "outcome": "auth-bypass-fixed"},
        {"id": "R1.S1", "security": True, "outcome": "fix"},
        {"id": "R1.S1", "security": True, "outcome": ["fixed"]},
        {"id": "R1.S1", "security": True},
        {"id": "R1.S1", "security": True, "outcome": "fixed", "text": DETAIL},
        {"id": "R1.S1", "security": True, "outcome": "fixed", "note": DETAIL},
    ],
)
def test_response_flagged_item_refusals(tmp_path: Path, bad: dict[str, Any]) -> None:
    with pytest.raises(RulingError, match="R1.S1") as excinfo:
        load_response_items(write(tmp_path, [bad]))
    message = str(excinfo.value)
    assert DETAIL not in message
    assert "src/x.py" not in message
    assert "auth-bypass" not in message


def test_response_unflagged_item_has_only_text(tmp_path: Path) -> None:
    with pytest.raises(RulingError, match="R1.1"):
        load_response_items(
            write(tmp_path, [{"id": "R1.1", "security": False, "text": "t", "outcome": "fixed"}])
        )
    with pytest.raises(RulingError, match="R1.1 is missing text"):
        load_response_items(write(tmp_path, [{"id": "R1.1", "security": False}]))


# --- file shape ------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("not json", "not valid JSON"),
        ('{"id": "R1"}', "JSON array"),
        ("[]", "holds no items"),
        ('["R1"]', "item 1 of the items file is not a JSON object"),
    ],
)
def test_items_file_shape(tmp_path: Path, content: str, match: str) -> None:
    path = tmp_path / "items.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RulingError, match=match):
        load_request_items(path)


def test_items_file_missing(tmp_path: Path) -> None:
    with pytest.raises(RulingError, match="cannot read the items file"):
        load_response_items(tmp_path / "absent.json")


def test_duplicate_ids_refused(tmp_path: Path) -> None:
    with pytest.raises(RulingError, match="R1.1 appears more than once"):
        load_request_items(write(tmp_path, [plain(), plain()]))
    dup = [{"id": "R1.S1", "security": True, "outcome": "fixed"}] * 2
    with pytest.raises(RulingError, match="R1.S1 appears more than once"):
        load_response_items(write(tmp_path, dup))


# --- rendering -------------------------------------------------------------


def test_render_request_lines_and_marker() -> None:
    body = render_request(
        [Item("R1.1", False, "fix or file?", "fix", None), Item("R1.S1", True, None, None, None)]
    )
    assert body == (
        "R1.1: fix or file? Recommendation: fix\n"
        f"R1.S1: {SECURITY_REQUEST_TEXT}\n"
        "\n"
        "<!-- vrg-ruling kind=request -->"
    )
    comment = IssueComment(1, AGENT, body, edited=False)
    assert parse_marker(comment, AGENT) == Marker("request", None, None)


def test_render_response_lines_and_marker() -> None:
    body = render_response(
        [
            Item("R1.1", False, "read as fix; fixed", None, None),
            Item("R1.S1", True, None, None, "filed"),
        ],
        request_id=41,
        reply_id=42,
    )
    assert body.splitlines()[:2] == ["R1.1: read as fix; fixed", "R1.S1: filed"]
    comment = IssueComment(1, AGENT, body, edited=False)
    assert parse_marker(comment, AGENT) == Marker("response", 41, 42)
