"""Tests for vergil_tooling.lib.rust_coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vergil_tooling.lib.rust_coverage import (
    CoverageExportError,
    Region,
    format_regions,
    missed_regions,
    uncovered_regions,
)

_LIB = "src/lib.rs"


def _region(
    line: int, col: int, count: int, *, file_id: int = 0, expanded: int = 0, kind: int = 0
) -> list[int]:
    return [line, col, line, col + 9, count, file_id, expanded, kind]


def _function(
    regions: list[list[int]], *, name: str = "f", filenames: list[str] | None = None
) -> dict[str, Any]:
    return {"name": name, "count": 1, "regions": regions, "filenames": filenames or [_LIB]}


def _export(*functions: dict[str, Any], files: list[str] | None = None) -> dict[str, Any]:
    """An llvm-cov export whose region totals are llvm-cov's count of *functions*.

    The totals are computed by hand here, as llvm-cov does: per source
    function (main file and first region), the best-covered instantiation.
    """
    groups: dict[tuple[str, int, int], tuple[int, int]] = {}
    for function in functions:
        regions = function["regions"]
        if not regions:
            continue
        code = [r for r in regions if r[7] == 0]
        first = regions[0]
        key = (function["filenames"][first[5]], first[0], first[1])
        covered = sum(1 for r in code if r[4])
        best = groups.get(key, (0, 0))
        groups[key] = (max(best[0], len(code)), max(best[1], covered))
    count = sum(n for n, _ in groups.values())
    covered = sum(c for _, c in groups.values())
    return {
        "type": "llvm.coverage.json.export",
        "version": "2.0.1",
        "data": [
            {
                "files": [{"filename": name} for name in (files or [_LIB])],
                "functions": list(functions),
                "totals": {
                    "regions": {"count": count, "covered": covered, "notcovered": count - covered}
                },
            }
        ],
    }


def test_uncovered_regions_on_a_covered_line() -> None:
    # Line 12 ran (its first region has a count), but the region at column 21,
    # an untested ``?`` error path, did not.
    export = _export(
        _function([_region(10, 1, 4), _region(12, 5, 4), [12, 21, 12, 30, 0, 0, 0, 0]])
    )
    assert uncovered_regions(export) == [Region(_LIB, 12, 21)]


def test_uncovered_regions_ignores_non_code_kinds() -> None:
    export = _export(
        _function(
            [
                _region(10, 1, 4),
                _region(11, 5, 0, kind=1, expanded=1),
                _region(12, 5, 0, kind=2),
                _region(13, 5, 0, kind=3),
                _region(14, 5, 0, kind=4),
            ],
            filenames=[_LIB, "src/macros.rs"],
        )
    )
    assert uncovered_regions(export) == []


def test_uncovered_regions_dedupes_instantiations() -> None:
    # Two instantiations of one generic function, the same region unrun in both.
    first = _function([_region(10, 1, 2), _region(12, 21, 0)], name="f::<u8>")
    second = _function([_region(10, 1, 3), _region(12, 21, 0)], name="f::<u16>")
    assert uncovered_regions(_export(first, second)) == [Region(_LIB, 12, 21)]


def test_uncovered_regions_listed_from_the_best_covered_instantiation() -> None:
    # llvm-cov counts the best-covered instantiation alone (two of three
    # regions in ``f::<u16>``), so the region at 12:21 is missed although
    # ``f::<u8>`` ran it.
    first = _function([_region(10, 1, 2), _region(12, 21, 3), _region(14, 5, 0)], name="f::<u8>")
    second = _function([_region(10, 1, 2), _region(12, 21, 0), _region(14, 5, 1)], name="f::<u16>")
    third = _function([_region(10, 1, 1), _region(12, 21, 0), _region(14, 5, 0)], name="f::<u32>")
    export = _export(first, second, third)
    assert missed_regions(export) == 1
    # A tie (``f::<u8>`` and ``f::<u16>`` both cover two) goes to the first.
    assert uncovered_regions(export) == [Region(_LIB, 14, 5)]
    assert uncovered_regions(_export(second, first, third)) == [Region(_LIB, 12, 21)]


def test_uncovered_regions_prefers_the_strictly_better_instantiation() -> None:
    worse = _function([_region(10, 1, 1), _region(12, 21, 0), _region(14, 5, 0)], name="f::<u8>")
    better = _function([_region(10, 1, 1), _region(12, 21, 0), _region(14, 5, 1)], name="f::<u16>")
    assert uncovered_regions(_export(worse, better)) == [Region(_LIB, 12, 21)]


def test_uncovered_regions_agree_with_the_region_totals() -> None:
    # Below 100% regions, the listing is non-empty and names exactly the
    # regions the totals count as missed.
    export = _export(
        _function([_region(1, 1, 1), _region(2, 5, 0), _region(3, 5, 0)], name="a"),
        _function([_region(10, 1, 1), _region(12, 21, 0)], name="g::<u8>"),
        _function([_region(10, 1, 0), _region(12, 21, 0)], name="g::<u16>"),
        _function([_region(20, 1, 0)], name="never_called"),
        _function([_region(30, 1, 5)], name="covered"),
        _function([_region(40, 1, 0)], name="m", filenames=["src/m.rs"]),
        files=[_LIB, "src/m.rs"],
    )
    total = export["data"][0]["totals"]["regions"]
    assert total["covered"] < total["count"]
    listed = uncovered_regions(export)
    assert listed
    assert len(listed) == missed_regions(export) == total["notcovered"] == 5


def test_uncovered_regions_none() -> None:
    export = _export(_function([_region(10, 1, 1), _region(12, 21, 3)]))
    assert uncovered_regions(export) == []
    assert missed_regions(export) == 0


def test_uncovered_regions_sorted_across_files() -> None:
    export = _export(
        _function([_region(9, 1, 0)], name="b", filenames=["src/b.rs"]),
        _function([_region(3, 1, 0), _region(2, 1, 0)], name="a", filenames=["src/a.rs"]),
        files=["src/a.rs", "src/b.rs"],
    )
    assert uncovered_regions(export) == [
        Region("src/a.rs", 2, 1),
        Region("src/a.rs", 3, 1),
        Region("src/b.rs", 9, 1),
    ]


def test_uncovered_regions_in_a_macro_expansion() -> None:
    # The body is in src/lib.rs (file 0); an expansion region pulls in file 1,
    # whose unrun code region is named by its own file.
    function = _function(
        [_region(10, 1, 1), _region(11, 5, 1, kind=1, expanded=1), _region(3, 9, 0, file_id=1)],
        filenames=[_LIB, "src/macros.rs"],
    )
    assert uncovered_regions(_export(function)) == [Region("src/macros.rs", 3, 9)]


def test_uncovered_regions_groups_by_the_body_file() -> None:
    # The first region is in the expanded file, but the group is keyed by the
    # body's own file (the one no expansion expands into).
    function = _function(
        [_region(3, 9, 0, file_id=1), _region(10, 1, 1), _region(11, 5, 1, kind=1, expanded=1)],
        filenames=[_LIB, "src/macros.rs"],
    )
    assert uncovered_regions(_export(function)) == [Region("src/macros.rs", 3, 9)]


def test_uncovered_regions_skips_unreported_files() -> None:
    # A function whose body is in a file the report filters out (a test file)
    # is not in the totals, so it is not listed.
    export = _export(
        _function([_region(1, 1, 0)], name="t", filenames=["tests/it.rs"]),
        _function([_region(1, 1, 1)]),
    )
    assert uncovered_regions(export) == []


def test_uncovered_regions_skips_a_function_with_no_regions() -> None:
    assert uncovered_regions(_export(_function([_region(1, 1, 1)]), _function([]))) == []


@pytest.mark.parametrize(
    "export",
    [
        {},
        {"data": "x"},
        {"data": ["x"]},
        {"data": [{"files": []}]},
        {"data": [{"functions": []}]},
        {"data": [{"functions": [], "files": [{"name": "x"}]}]},
        {"data": [{"functions": [{"name": "f"}], "files": []}]},
        {"data": [{"functions": [{"regions": [[1, 2]], "filenames": [_LIB]}], "files": []}]},
        {"data": [{"functions": [{"regions": [], "filenames": [3]}], "files": []}]},
        {"data": [{"functions": [_function([_region(1, 1, 0, file_id=1)])], "files": []}]},
        {"data": [{"functions": [_function([_region(1, 1, 0, kind=1)])], "files": []}]},
    ],
)
def test_uncovered_regions_rejects_a_malformed_export(export: dict[str, Any]) -> None:
    with pytest.raises(CoverageExportError):
        uncovered_regions(export)


@pytest.mark.parametrize(
    "export",
    [
        {"data": [{}]},
        {"data": [{"totals": {"regions": {}}}]},
        {"data": [{"totals": {"regions": {"notcovered": "x"}}}]},
        {"data": [{"totals": None}]},
    ],
)
def test_missed_regions_rejects_missing_totals(export: dict[str, Any]) -> None:
    with pytest.raises(CoverageExportError):
        missed_regions(export)


def test_format_regions() -> None:
    regions = [Region("/workspace/src/lib.rs", 12, 21), Region("/elsewhere/x.rs", 1, 2)]
    assert format_regions(regions) == [
        "/workspace/src/lib.rs:12:21: uncovered region",
        "/elsewhere/x.rs:1:2: uncovered region",
    ]
    assert format_regions(regions, root=Path("/workspace")) == [
        "src/lib.rs:12:21: uncovered region",
        "/elsewhere/x.rs:1:2: uncovered region",
    ]
    assert format_regions([]) == []
