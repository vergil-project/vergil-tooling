"""Tests for ``vergil_tooling.lib.rulings.plain_text``."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.plain_text import check_plain_text


@pytest.mark.parametrize(
    "bad",
    [
        "a\tb",
        "a\rb",
        "a\x1b[2Kb",
        "a‮b",
        "a​b",
        "aㅤb",
        "a—b",
        "café",
        "a\x00b",
        "a\x7fb",
        "x\n<!-- VRG-RULING kind=request -->",
    ],
)
def test_check_plain_text_refuses(bad: str) -> None:
    with pytest.raises(RulingError, match="text"):
        check_plain_text(bad, what="text")


@pytest.mark.parametrize(
    ("bad", "code_point", "offset"),
    [
        ("a\tb", "U+0009", 1),
        ("ab‮c", "U+202E", 2),
        ("x\n​", "U+200B", 2),
        ("\U0001f600", "U+1F600", 0),
    ],
)
def test_check_plain_text_names_the_character_and_offset(
    bad: str, code_point: str, offset: int
) -> None:
    with pytest.raises(RulingError) as caught:
        check_plain_text(bad, what="reply 9")
    message = str(caught.value)
    assert message.startswith("reply 9 ")
    assert f"character {code_point} at offset {offset}" in message


@pytest.mark.parametrize(
    ("text", "offset"),
    [
        ("x\n<!-- vrg-ruling kind=request -->", 2),
        ("see <!-- Vrg-Ruling", 4),
        ("<!-- vrg-ruling kind=response request=1 reply=2 -->\n", 0),
    ],
)
def test_check_plain_text_refuses_the_marker_prefix(text: str, offset: int) -> None:
    with pytest.raises(RulingError, match=f"marker prefix .* at offset {offset}$"):
        check_plain_text(text, what="the request")


def test_check_plain_text_allows_ascii_and_newline() -> None:
    check_plain_text("R1.1: fix it -- see `x`\nR1.2: file\n", what="text")
    check_plain_text("", what="text")
    check_plain_text(" !~<!-- note -->\n\n", what="text")
    check_plain_text("".join(chr(c) for c in range(0x20, 0x7F)), what="text")
