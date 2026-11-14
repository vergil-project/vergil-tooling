"""Tests for vergil_tooling.lib.markdown_escape."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.markdown_escape import escape_inline


def test_escape_inline() -> None:
    assert escape_inline("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert escape_inline("[x](javascript:alert(1))") == "\\[x\\](javascript:alert(1))"
    assert escape_inline("a|b") == "a\\|b"
    assert escape_inline("a\nb") == "a b"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain title", "plain title"),
        ("a\r\nb\rc", "a b c"),
        ("AT&T", "AT&amp;T"),
        ("&lt;", "&amp;lt;"),
        ("back\\slash", "back\\\\slash"),
        ("`code`", "\\`code\\`"),
        ("*bold* _em_", "\\*bold\\* \\_em\\_"),
        ("~~gone~~", "\\~\\~gone\\~\\~"),
        ("# heading", "\\# heading"),
        ("issue #12", "issue #12"),
        ("\\|", "\\\\\\|"),
    ],
)
def test_escape_inline_cases(text: str, expected: str) -> None:
    assert escape_inline(text) == expected
