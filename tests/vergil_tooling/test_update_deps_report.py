"""Tests for vergil_tooling.lib.update_deps.report (the held-back report)."""

from __future__ import annotations

import json

import pytest

from vergil_tooling.lib.update_deps import report
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult

_E = HeldBack(
    source="python",
    name="example-pkg",
    current="1.9.4",
    latest="2.1.0",
    note="pyproject: example-pkg>=1.0,<2",
)


def test_to_line_round_trips() -> None:
    line = report.to_line(_E)
    assert json.loads(line) == {
        "source": "python",
        "name": "example-pkg",
        "current": "1.9.4",
        "latest": "2.1.0",
        "note": "pyproject: example-pkg>=1.0,<2",
    }
    assert report.parse_report_lines(line + "\n", origin="t") == [_E]


def test_to_line_omits_absent_note() -> None:
    entry = HeldBack(source="s", name="n", current="1", latest="2")
    assert "note" not in json.loads(report.to_line(entry))


def test_parse_report_lines_accepts_blank_lines() -> None:
    text = "\n" + report.to_line(_E) + "\n\n"
    assert report.parse_report_lines(text, origin="t") == [_E]


def test_parse_report_lines_empty_text() -> None:
    assert report.parse_report_lines("", origin="t") == []


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("{not json", "not valid JSON"),
        ("[1, 2]", "not a JSON object"),
        ('{"source": "s", "name": "n", "current": "1"}', "missing required key 'latest'"),
        (
            '{"source": "s", "name": "n", "current": 1, "latest": "2"}',
            "'current' must be a string",
        ),
        (
            '{"source": "s", "name": "n", "current": "1", "latest": "2", "note": 3}',
            "'note' must be a string",
        ),
        (
            '{"source": "s", "name": "n", "current": "1", "latest": "2", "extra": "x"}',
            "unknown key 'extra'",
        ),
    ],
)
def test_parse_report_lines_rejects_malformed(line: str, why: str) -> None:
    text = report.to_line(_E) + "\n" + line + "\n"
    with pytest.raises(UpdateDepsError, match=why) as info:
        report.parse_report_lines(text, origin="scripts/dev/update-deps.sh")
    assert "line 2" in str(info.value)
    assert "scripts/dev/update-deps.sh" in str(info.value)
    assert info.value.phase == "update"
    assert info.value.command == "scripts/dev/update-deps.sh"


def test_collect_flattens_in_result_order() -> None:
    a = HeldBack(source="a", name="a", current="1", latest="2")
    results = [
        UpdateResult(updater="x", changed=False, summary="", commit_message="", held_back=[a]),
        UpdateResult(updater="y", changed=False, summary="", commit_message=""),
        UpdateResult(updater="z", changed=True, summary="", commit_message="", held_back=[_E]),
    ]
    assert report.collect(results) == [a, _E]


def test_update_result_held_back_defaults_empty() -> None:
    result = UpdateResult(updater="x", changed=False, summary="", commit_message="")
    assert result.held_back == []


def test_render_markdown_empty_says_so() -> None:
    assert report.render_markdown([]) == report.EMPTY_REPORT
    assert "No dependencies are held back" in report.EMPTY_REPORT


def test_render_markdown_table_escapes_pipes() -> None:
    entry = HeldBack(source="python", name="p", current="1", latest="2", note="a|b")
    text = report.render_markdown([entry])
    assert text.splitlines()[0] == "| Source | Dependency | Current | Latest | Note |"
    assert "| python | p | 1 | 2 | a\\|b |" in text


def test_render_markdown_absent_note_is_empty_cell() -> None:
    entry = HeldBack(source="s", name="n", current="1", latest="2")
    assert "| s | n | 1 | 2 |  |" in report.render_markdown([entry])


def test_render_markdown_exactly_max_rows_has_no_overflow_line() -> None:
    entries = [HeldBack(source="s", name=f"n{i}", current="1", latest="2") for i in range(3)]
    text = report.render_markdown(entries, max_rows=3)
    assert text.count("\n| s | n") == 3
    assert "more" not in text


def test_render_markdown_caps_rows() -> None:
    entries = [HeldBack(source="s", name=f"n{i}", current="1", latest="2") for i in range(2000)]
    text = report.render_markdown(entries)
    assert text.count("\n| s | n") == 200
    assert "…and 1800 more" in text
    assert len(text) < 20_000
