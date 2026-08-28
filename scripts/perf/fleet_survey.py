#!/usr/bin/env python3
"""Phase-0 fleet survey: test-layout, dev-dep shape, and Python floor.

Epic vergil-project/.github#333, Task 3. This driver imports the two ``lib``
classifiers — the import-graph leaves that library code (Tasks 9 and 10)
consumes — and adds a survey-only ``python_floor`` probe, then renders three
markdown tables over the Python repos found among the sibling clones.

The classifiers live in ``lib/`` (not here) precisely so ``languages.py`` and
``lib/xdist_applicator.py`` can consume them without ``lib`` importing from
``scripts/``. This script is the *consumer* side: a plain driver, not gated
library code.

Usage::

    uv run python scripts/perf/fleet_survey.py [--root <dir>] [--out <file>]

``--root`` defaults to the parent of this repo (the sibling-clones directory);
``--out`` defaults to stdout. Repos flagged ``UNKNOWN`` dev-dep shape or
``importlib_safe = False`` are reported loudly on stderr (never a silent no-op).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from vergil_tooling.lib.dev_deps import DevDepShape, classify_dev_deps
from vergil_tooling.lib.test_layout import LayoutVerdict, classify_test_layout


def python_floor(repo: Path) -> str | None:
    """Return the repo's parsed ``requires-python`` string, or ``None``.

    Survey-only (used as a Task-4 sysmon-guard sanity check): sysmon requires
    3.12+, so a floor below that would flag a repo where the guard matters.
    """
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    floor = project.get("requires-python")
    return floor if isinstance(floor, str) else None


def is_python_repo(repo: Path) -> bool:
    """A repo is Python-surveyable when it ships a root ``pyproject.toml``."""
    return (repo / "pyproject.toml").is_file()


def discover_python_repos(root: Path) -> list[Path]:
    """Sibling clones under ``root`` that are Python repos, sorted by name."""
    return sorted(
        (
            p
            for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".") and is_python_repo(p)
        ),
        key=lambda p: p.name,
    )


def _render_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def render_layout_table(rows: list[tuple[str, LayoutVerdict]]) -> str:
    lines = [
        _render_row(["Repo", "Packaged", "Duplicate basenames", "importlib_safe"]),
        _render_row(["---", "---", "---", "---"]),
    ]
    for name, v in rows:
        dupes = ", ".join(f"`{d}`" for d in v.duplicate_basenames) or "—"
        lines.append(_render_row([f"`{name}`", str(v.packaged), dupes, str(v.importlib_safe)]))
    return "\n".join(lines)


def render_shape_table(rows: list[tuple[str, DevDepShape]]) -> str:
    lines = [
        _render_row(["Repo", "Dev-dependency shape"]),
        _render_row(["---", "---"]),
    ]
    for name, shape in rows:
        lines.append(_render_row([f"`{name}`", f"`{shape.value}`"]))
    return "\n".join(lines)


def render_floor_table(rows: list[tuple[str, str | None]]) -> str:
    lines = [
        _render_row(["Repo", "requires-python"]),
        _render_row(["---", "---"]),
    ]
    for name, floor in rows:
        lines.append(_render_row([f"`{name}`", f"`{floor}`" if floor else "—"]))
    return "\n".join(lines)


def build_report(repos: list[Path]) -> tuple[str, list[str]]:
    """Render the three tables and collect loud flags for risky repos."""
    layout_rows = [(r.name, classify_test_layout(r)) for r in repos]
    shape_rows = [(r.name, classify_dev_deps(r)) for r in repos]
    floor_rows = [(r.name, python_floor(r)) for r in repos]

    flags: list[str] = []
    for name, verdict in layout_rows:
        if not verdict.importlib_safe:
            flags.append(f"{name}: importlib_safe = False (duplicate basenames)")
    for name, shape in shape_rows:
        if shape is DevDepShape.UNKNOWN:
            flags.append(f"{name}: dev-dependency shape UNKNOWN (sweep must report loudly)")

    report = "\n\n".join(
        [
            "## Table 1 — Collection safety (import-mode)",
            render_layout_table(layout_rows),
            "## Table 2 — Dev-dependency shape",
            render_shape_table(shape_rows),
            "## Table 3 — Python floor (`requires-python`)",
            render_floor_table(floor_rows),
        ]
    )
    return report, flags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="Directory holding sibling repo clones (default: parent of this repo).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the report here (default: stdout).",
    )
    args = parser.parse_args(argv)

    repos = discover_python_repos(args.root)
    report, flags = build_report(repos)

    surveyed = ", ".join(r.name for r in repos) or "(none)"
    header = (
        f"<!-- generated by scripts/perf/fleet_survey.py over {args.root} -->\n"
        f"Surveyed Python repos: {surveyed}\n"
    )
    body = header + "\n" + report + "\n"

    if args.out is not None:
        args.out.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)

    if flags:
        sys.stderr.write("\nFLAGGED repos (need follow-up):\n")
        for flag in flags:
            sys.stderr.write(f"  - {flag}\n")

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
