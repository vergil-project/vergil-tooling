"""Tests for vergil_tooling.lib.text_table."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import text_table


def test_render_aligns_columns_and_strips_trailing_space() -> None:
    out = text_table.render(("PR", "TITLE"), [("#1", "a"), ("#123", "")])
    assert out == "PR    TITLE\n#1    a\n#123"


def test_render_indents_every_line() -> None:
    out = text_table.render(("A", "B"), [("x", "y")], indent="  ")
    assert out == "  A  B\n  x  y"


def test_render_with_no_rows_is_the_header() -> None:
    assert text_table.render(("A", "BB"), []) == "A  BB"


def test_render_refuses_a_short_row() -> None:
    with pytest.raises(ValueError, match="row has 1 cells for 2 columns"):
        text_table.render(("A", "B"), [("x",)])


def test_truncate_keeps_short_text() -> None:
    assert text_table.truncate("short", 10) == "short"
    assert text_table.truncate("x" * 10, 10) == "x" * 10


def test_truncate_cuts_with_an_ellipsis() -> None:
    cut = text_table.truncate("x" * 100)
    assert len(cut) == text_table.TITLE_WIDTH
    assert cut.endswith("…")
