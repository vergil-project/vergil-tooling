"""Unit tests for the coverage-equivalence proof tool (epic #333, Task 2).

The pure logic under the coverage gate is :func:`diff_reports`, which compares
the missing-line/branch set of two ``coverage.xml`` reports and returns their
symmetric difference (empty means the two reports measure the identical set of
misses). The run-and-diff driver is a thin ``scripts/`` layer exercised by
actually running the harness, not by unit tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.perf.coverage_equivalence import diff_reports

if TYPE_CHECKING:
    from pathlib import Path


def test_identical_reports_diff_empty(tmp_path: Path) -> None:
    xml = (
        '<coverage><packages><package><classes><class filename="a.py">'
        '<lines><line number="1" hits="1"/></lines></class></classes>'
        "</package></packages></coverage>"
    )
    (tmp_path / "a.xml").write_text(xml)
    (tmp_path / "b.xml").write_text(xml)
    assert diff_reports(tmp_path / "a.xml", tmp_path / "b.xml") == []


def test_differing_miss_is_reported(tmp_path: Path) -> None:
    hit = (
        '<coverage><packages><package><classes><class filename="a.py">'
        '<lines><line number="1" hits="1"/></lines></class></classes>'
        "</package></packages></coverage>"
    )
    miss = hit.replace('hits="1"', 'hits="0"')
    (tmp_path / "a.xml").write_text(hit)
    (tmp_path / "b.xml").write_text(miss)
    assert diff_reports(tmp_path / "a.xml", tmp_path / "b.xml") != []
