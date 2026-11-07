"""The scope change comment: its rendered body and how it is recognised."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.scope import SCOPE_HEADER, is_scope_change, render_scope_change

HUMAN = "h"
AGENT = "h-vergil-user"


def test_render_adds_the_header() -> None:
    assert render_scope_change("Also cover the docs.") == "Scope change\n\nAlso cover the docs.\n"


def test_render_drops_leading_and_trailing_blank_lines() -> None:
    body = render_scope_change("\n\nAdd x.\n\nDrop y.\n\n")
    assert body == "Scope change\n\nAdd x.\n\nDrop y.\n"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("café", "not plain text: character U\\+00E9 at offset 3"),
        ("a\rb", "U\\+000D"),
        ("x\n<!-- VRG-RULING kind=request -->", "marker prefix"),
        ("", "blank"),
        ("\n  \n", "blank"),
        ("Scope change\n\nx", "header is added for you"),
        ("Scope change  \nx", "header is added for you"),
    ],
)
def test_render_refuses(text: str, match: str) -> None:
    with pytest.raises(RulingError, match=match):
        render_scope_change(text)


def test_render_keeps_a_header_later_in_the_text() -> None:
    assert render_scope_change("x\nScope change") == "Scope change\n\nx\nScope change\n"


def _comment(author: str, body: str, *, edited: bool = False) -> IssueComment:
    return IssueComment(1, author, body, edited)


def test_is_scope_change_recognises_the_rendered_body() -> None:
    assert is_scope_change(_comment(HUMAN, render_scope_change("Add x.")), HUMAN)


@pytest.mark.parametrize(
    "body",
    ["Scope change\r\n\r\nAdd x.\r\n", "Scope change", "Scope change   \n\nAdd x."],
)
def test_is_scope_change_tolerates_line_endings_and_trailing_space(body: str) -> None:
    assert is_scope_change(_comment(HUMAN, body), HUMAN)


def test_is_scope_change_leaves_edits_to_the_caller() -> None:
    assert is_scope_change(_comment(HUMAN, "Scope change\n\nx\n", edited=True), HUMAN)


@pytest.mark.parametrize(
    ("author", "body"),
    [
        (AGENT, "Scope change\n\nAdd x.\n"),
        ("stranger", "Scope change\n\nAdd x.\n"),
        ("", "Scope change\n\nAdd x.\n"),
        (HUMAN, "R1.1: fix\n"),
        (HUMAN, "scope change\n\nAdd x.\n"),
        (HUMAN, "Scope change: add x\n"),
        (HUMAN, "\nScope change\n\nAdd x.\n"),
        (HUMAN, ""),
    ],
)
def test_is_scope_change_refuses(author: str, body: str) -> None:
    assert not is_scope_change(_comment(author, body), HUMAN)


def test_is_scope_change_needs_a_human_login() -> None:
    assert not is_scope_change(_comment("", "Scope change\n"), "")


def test_header_constant() -> None:
    assert SCOPE_HEADER == "Scope change"
