"""Locate the uncovered regions behind a failed Rust region-coverage gate.

The Rust TEST gate holds ``cargo llvm-cov report`` to 100% lines and 100%
regions (epic vergil-project/.github#1165, spec §6.2). ``--show-missing-lines``
names only lines that never ran, so a missed region on a line that did run (an
untested ``?`` error path, an ``else`` arm) would fail the gate without saying
where. :func:`uncovered_regions` reads the same report as JSON (``cargo
llvm-cov report --json``, llvm-cov's export format) and names each uncovered
region by file, line and column.

The listing reproduces llvm-cov's own region count, so it is non-empty exactly
when the region total is below 100%, and its length is the number of regions
the total counts as missed (the scope change on
vergil-project/vergil-tooling#4472). llvm-cov's summary
(``CoverageReport::prepareSingleFileReport``) groups a function's
instantiations by source function, the file and start position of its body,
and counts each group once, with the covered count of its best-covered
instantiation: a generic function's region is not covered merely because some
other instantiation ran it. Only code regions (kind 0) count; expansion, gap,
skipped and branch regions do not. A function is counted in the file that
holds its body, and only when that file is in the report's ``files`` (the
report's filename filters apply to the totals the gate reads).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A region array is ``[line_start, col_start, line_end, col_end,
# execution_count, file_id, expanded_file_id, kind]``.
_LINE, _COL, _COUNT, _FILE_ID, _EXPANDED_FILE_ID, _KIND = 0, 1, 4, 5, 6, 7
_REGION_FIELDS = 8
_CODE_REGION = 0
_EXPANSION_REGION = 1


class CoverageExportError(ValueError):
    """The coverage export is not the llvm-cov JSON export this module reads."""


@dataclass(frozen=True, order=True)
class Region:
    """One uncovered code region: its file and its start line and column."""

    file: str
    line: int
    col: int


def _main_file_id(regions: list[list[int]]) -> int:
    """The file ID of the function's body: one no expansion region expands into.

    This is llvm-cov's ``findMainViewFileID``: a function's regions span its
    own file and every file a macro expansion pulls in, and the body's file is
    the one that is not itself an expansion's target.
    """
    expanded = {r[_EXPANDED_FILE_ID] for r in regions if r[_KIND] == _EXPANSION_REGION}
    main = sorted({r[_FILE_ID] for r in regions} - expanded)
    if not main:
        msg = "coverage export: a function has no region outside a macro expansion"
        raise CoverageExportError(msg)
    return main[0]


def _covered(regions: list[list[int]]) -> int:
    return sum(1 for r in regions if r[_KIND] == _CODE_REGION and r[_COUNT] != 0)


def _units(export: dict[str, Any]) -> list[dict[str, Any]]:
    data = export.get("data")
    if not isinstance(data, list) or not all(isinstance(unit, dict) for unit in data):
        msg = "coverage export: no 'data' list of objects (not an llvm-cov JSON export)"
        raise CoverageExportError(msg)
    return data


def _functions(export: dict[str, Any]) -> list[dict[str, Any]]:
    functions: list[dict[str, Any]] = []
    for unit in _units(export):
        if not isinstance(unit.get("functions"), list):
            msg = "coverage export: a 'data' entry has no 'functions' list (a summary-only export?)"
            raise CoverageExportError(msg)
        functions.extend(unit["functions"])
    return functions


def _reported_files(export: dict[str, Any]) -> set[str]:
    files: set[str] = set()
    for unit in _units(export):
        entries = unit.get("files")
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("filename"), str) for entry in entries
        ):
            msg = "coverage export: a 'data' entry has no 'files' list of named files"
            raise CoverageExportError(msg)
        files.update(entry["filename"] for entry in entries)
    return files


def uncovered_regions(export: dict[str, Any]) -> list[Region]:
    """The code regions llvm-cov's region total counts as missed, sorted.

    *export* is the parsed output of ``cargo llvm-cov report --json``. Each
    source function contributes the count-0 code regions of its best-covered
    instantiation (the most covered code regions; a tie goes to the first in
    export order). Two source functions that share a region (one macro
    expanded in both) each list it, as llvm-cov counts it in each.
    """
    reported = _reported_files(export)
    best: dict[tuple[str, int, int], list[list[int]]] = {}
    filenames_of: dict[tuple[str, int, int], list[str]] = {}
    for function in _functions(export):
        regions = function.get("regions")
        filenames = function.get("filenames")
        if not isinstance(regions, list) or not isinstance(filenames, list):
            msg = f"coverage export: function {function.get('name')!r} has no regions or filenames"
            raise CoverageExportError(msg)
        if not all(
            isinstance(r, list) and len(r) >= _REGION_FIELDS and all(isinstance(v, int) for v in r)
            for r in regions
        ) or not all(isinstance(name, str) for name in filenames):
            msg = f"coverage export: function {function.get('name')!r} has a malformed region"
            raise CoverageExportError(msg)
        if any(r[_FILE_ID] >= len(filenames) for r in regions):
            msg = f"coverage export: function {function.get('name')!r} names a missing file"
            raise CoverageExportError(msg)
        if not regions:
            continue
        main = _main_file_id(regions)
        main_file = filenames[main]
        if main_file not in reported:
            continue
        first = next(r for r in regions if r[_FILE_ID] == main)
        key = (main_file, first[_LINE], first[_COL])
        if key not in best or _covered(regions) > _covered(best[key]):
            best[key] = regions
            filenames_of[key] = filenames
    return sorted(
        Region(filenames_of[key][r[_FILE_ID]], r[_LINE], r[_COL])
        for key, regions in best.items()
        for r in regions
        if r[_KIND] == _CODE_REGION and r[_COUNT] == 0
    )


def missed_regions(export: dict[str, Any]) -> int:
    """The number of regions the export's totals count as not covered.

    It is the count the ``--fail-under-regions`` gate reads, summed over the
    export's ``data`` entries; :func:`uncovered_regions` lists that many.
    """
    total = 0
    for unit in _units(export):
        try:
            total += int(unit["totals"]["regions"]["notcovered"])
        except (KeyError, TypeError, ValueError) as exc:
            msg = "coverage export: a 'data' entry has no totals.regions.notcovered count"
            raise CoverageExportError(msg) from exc
    return total


def format_regions(regions: list[Region], *, root: Path | None = None) -> list[str]:
    """One ``<file>:<line>:<col>: uncovered region`` line per region.

    llvm-cov names files by absolute path; with *root*, a file under it is
    shown relative to it.
    """

    def shown(file: str) -> str:
        path = Path(file)
        if root is not None and path.is_relative_to(root):
            return str(path.relative_to(root))
        return file

    return [f"{shown(r.file)}:{r.line}:{r.col}: uncovered region" for r in regions]
