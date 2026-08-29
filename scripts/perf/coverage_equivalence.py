#!/usr/bin/env python3
"""Coverage-equivalence proof for the test-perf epic (epic #333, Task 2).

The 100% branch-coverage gate (`--cov-branch --cov-fail-under=100`) is
inviolate: every speed lever in this epic must keep measuring the *identical*
missing-line/branch set. This module proves that by running the suite under two
configurations, exporting each as a Cobertura ``coverage.xml`` report, and
diffing the two missing sets. An empty diff means the configurations are
coverage-equivalent — the gate measures the same thing either way.

Pure logic (under the unit-test/coverage gate):

    diff_reports(a: Path, b: Path) -> list[str]

    Parse two ``coverage.xml`` reports, collect each report's set of misses
    (uncovered lines and partially-covered branches), and return the sorted
    *symmetric difference* as human-readable strings. An empty list means the
    two reports have the identical miss set (coverage-equivalent).

The run-and-diff driver below is a thin ``scripts/`` layer (not coverage
measured — ``vrg-validate`` scopes coverage to ``src/`` only). It is what
produces ``epics/333-python-test-perf/evidence/coverage-equivalence.md``.

## What this proof actually gates (read the baseline first)

Per the Phase-0 baseline (`evidence/baseline.md`, Finding 1): under
``--cov-branch``, setting ``COVERAGE_CORE=sysmon`` does **not** activate
``sys.monitoring``. coverage.py 7.14 warns ``no-sysmon: sys.monitoring can't
measure branches`` and silently falls back to the C tracer. So a "sysmon" run
under our branch-coverage gate is *really a C-tracer run*, and a
C-tracer-vs-sysmon diff is trivially empty because both sides use the same
backend. That trivially-empty diff corroborates the baseline; it is **not** a
meaningful gate.

The load-bearing proof is therefore **C-tracer serial** vs
**C-tracer + ``-n auto --dist worksteal``** (serial vs parallel). Process-level
parallelism (pytest-xdist) is the one lever that could genuinely change what
coverage sees — each worker measures a subset and pytest-cov combines them — so
this is the diff that must be empty before the xdist command (Task 6) may ship.

Usage (inside the dev container so the numbers reflect the real gate)::

    vrg-container-run -- uv run python scripts/perf/coverage_equivalence.py \\
        --emit-evidence \\
        --out epics/333-python-test-perf/evidence/coverage-equivalence.md
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
# defusedxml hardens XML parsing against entity-expansion / external-entity
# attacks (the Semgrep security gate rejects stdlib xml.etree). Its parse()
# returns standard ElementTree objects, so downstream navigation is unchanged.
import defusedxml.ElementTree as ET

# --------------------------------------------------------------------------- #
# Pure logic (the only unit-tested surface).
# --------------------------------------------------------------------------- #


def _collect_misses(report: Path) -> set[str]:
    """Collect the set of misses from one Cobertura ``coverage.xml`` report.

    A *miss* is either:

    - an **uncovered line** — a ``<line>`` with ``hits="0"``; or
    - a **partially-covered branch** — a ``<line branch="true">`` whose
      ``condition-coverage`` is not ``100%`` (some arms of the branch were never
      taken).

    Each miss is rendered as a stable, human-readable string keyed on
    ``filename`` + line number so two reports produce comparable sets. The line
    and branch namespaces are kept distinct so an uncovered line and a partial
    branch on the same physical line never collide.
    """
    tree = ET.parse(report)
    root = tree.getroot()
    misses: set[str] = set()
    for cls in root.iter("class"):
        filename = cls.get("filename", "")
        lines = cls.find("lines")
        if lines is None:
            continue
        for line in lines.iter("line"):
            number = line.get("number", "")
            hits = line.get("hits", "0")
            if hits == "0":
                misses.add(f"{filename}:{number} line not covered")
            if line.get("branch") == "true":
                condition = line.get("condition-coverage", "")
                if condition and not condition.startswith("100%"):
                    misses.add(f"{filename}:{number} branch partial: {condition}")
    return misses


def diff_reports(a: Path, b: Path) -> list[str]:
    """Return the sorted symmetric difference of two reports' miss sets.

    An empty list means the two ``coverage.xml`` reports measure the identical
    set of uncovered lines and partial branches — i.e. the two configurations
    are coverage-equivalent. A non-empty list names every miss that is present
    in exactly one of the two reports, tagged with which side it came from.
    """
    misses_a = _collect_misses(a)
    misses_b = _collect_misses(b)
    only_a = sorted(f"only in {a.name}: {m}" for m in misses_a - misses_b)
    only_b = sorted(f"only in {b.name}: {m}" for m in misses_b - misses_a)
    return only_a + only_b


# --------------------------------------------------------------------------- #
# Run-and-diff driver (thin scripts/ layer; not coverage measured).
# --------------------------------------------------------------------------- #

# Base pytest args mirroring the real ``vrg-validate`` TEST gate. ``--cov-branch``
# is the inviolate gate this proof defends; ``--cov-fail-under`` is omitted so a
# genuine coverage difference surfaces as a non-empty diff (the thing we are
# measuring) rather than aborting the run before the report is written.
BASE_ARGS: list[str] = ["--cov=src", "--cov-branch"]


@dataclass(frozen=True)
class ProofConfig:
    """One configuration to run and export a ``coverage.xml`` for."""

    label: str
    pytest_args: list[str]
    env: dict[str, str] = field(default_factory=dict)


def _run_and_export(config: ProofConfig, xml_out: Path) -> int:
    """Run the suite under ``config`` and export its coverage to ``xml_out``.

    Returns pytest's exit code (recorded, never swallowed). The C tracer is
    forced explicitly via ``COVERAGE_CORE=ctrace`` so the backend is
    deterministic regardless of any inherited environment.
    """
    cmd = [
        "uv",
        "run",
        "pytest",
        *BASE_ARGS,
        f"--cov-report=xml:{xml_out}",
        *config.pytest_args,
    ]
    run_env = {**os.environ, **config.env}
    print(f"  [{config.label}] running: {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, env=run_env, check=False)  # noqa: S603
    print(f"  [{config.label}] exit code {proc.returncode}", flush=True)
    return proc.returncode


def proof_configs() -> tuple[ProofConfig, ProofConfig, ProofConfig]:
    """The three configurations named in the plan.

    - ``serial`` — the reference: C tracer, serial (``-n0`` overrides this repo's
      ``addopts = ["-n", "auto"]``).
    - ``parallel`` — the load-bearing comparison: C tracer under
      ``-n auto --dist worksteal``. Its diff against ``serial`` is the gate that
      lets the xdist command (Task 6) ship.
    - ``sysmon`` — the plan's "C-tracer vs sysmon" comparison. Under
      ``--cov-branch`` this *also* runs the C tracer (sysmon silently falls back,
      baseline Finding 1), so its diff against ``serial`` is trivially empty and
      corroborates the baseline rather than proving sysmon equivalent.
    """
    ctrace = {"COVERAGE_CORE": "ctrace"}
    sysmon = {"COVERAGE_CORE": "sysmon"}
    return (
        ProofConfig("C-tracer serial", ["-n0"], ctrace),
        ProofConfig("C-tracer parallel (-n auto --dist worksteal)", ["-n", "auto", "--dist", "worksteal"], ctrace),
        ProofConfig("sysmon serial (falls back to C tracer under --cov-branch)", ["-n0"], sysmon),
    )


def render_evidence(
    *,
    serial: ProofConfig,
    parallel: ProofConfig,
    sysmon: ProofConfig,
    codes: dict[str, int],
    parallel_diff: list[str],
    sysmon_diff: list[str],
    test_count: int,
) -> str:
    """Render the coverage-equivalence evidence markdown (MD013 relaxed prose)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parallel_ok = not parallel_diff
    sysmon_ok = not sysmon_diff

    lines: list[str] = []
    lines.append("# Coverage-equivalence proof (epic #333, Task 2)")
    lines.append("")
    lines.append(
        "Recorded evidence that the epic's speed levers do not change what the "
        "inviolate `--cov-branch --cov-fail-under=100` gate measures. Produced by "
        "`scripts/perf/coverage_equivalence.py`: it runs the suite under each "
        "configuration, exports a Cobertura `coverage.xml`, and diffs the "
        "missing-line/branch sets with the unit-tested `diff_reports`. An empty "
        "diff means the two configurations are coverage-equivalent."
    )
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- Recorded: {now}")
    lines.append(f"- Interpreter: CPython {platform.python_version()} ({platform.machine()})")
    lines.append(f"- Platform: {platform.system()} {platform.release()}")
    lines.append(f"- Tests collected: {test_count}")
    lines.append(
        "- Invocation: `uv run pytest --cov=src --cov-branch --cov-report=xml:<f>` "
        "plus each config's argv/env, run inside the dev container"
    )
    lines.append("")
    lines.append("## The load-bearing proof: serial vs parallel (C tracer)")
    lines.append("")
    lines.append(
        "Process-level parallelism (pytest-xdist) is the one lever that could "
        "genuinely change what coverage sees: each worker measures a subset of the "
        "suite and pytest-cov combines the per-worker data. If combination dropped "
        "or double-counted anything, the parallel run's missing set would differ "
        "from the serial run's. **This diff must be empty for the xdist command "
        "(Task 6) to ship.**"
    )
    lines.append("")
    lines.append(f"- Reference: `{serial.label}` (exit code {codes[serial.label]})")
    lines.append(f"- Compared:  `{parallel.label}` (exit code {codes[parallel.label]})")
    lines.append("")
    if parallel_ok:
        lines.append(
            "**Result: EMPTY diff — coverage-equivalent.** Serial and parallel runs "
            "measure the identical missing-line/branch set. pytest-cov's per-worker "
            "combination preserves the gate exactly, so `-n auto --dist worksteal` "
            "is safe to ship under the 100% branch-coverage gate."
        )
    else:
        lines.append(
            "**Result: NON-EMPTY diff — BLOCKS Phase 2.** Serial and parallel runs "
            "measure a different missing-line/branch set. The xdist command (Task 6) "
            "MUST NOT ship until this is resolved. The differing entries are:"
        )
        lines.append("")
        lines.append("```text")
        lines.extend(parallel_diff)
        lines.append("```")
    lines.append("")
    lines.append("## The plan's C-tracer-vs-sysmon comparison (corroborates baseline Finding 1)")
    lines.append("")
    lines.append(
        "The plan also lists a C-tracer-vs-sysmon diff. Per the Phase-0 baseline "
        "(`evidence/baseline.md`, Finding 1), under `--cov-branch` coverage.py 7.14 "
        "warns `no-sysmon: sys.monitoring can't measure branches` and **silently "
        "falls back to the C tracer** — so a `COVERAGE_CORE=sysmon` run is really a "
        "C-tracer run. Both sides of this diff therefore use the same backend, and "
        "the diff is trivially empty. This is recorded as corroboration of the "
        "baseline finding, **not** treated as a meaningful equivalence gate: it does "
        "not prove sysmon-vs-C-tracer equivalence because sysmon never ran. We do "
        "not force sysmon on."
    )
    lines.append("")
    lines.append(f"- Reference: `{serial.label}` (exit code {codes[serial.label]})")
    lines.append(f"- Compared:  `{sysmon.label}` (exit code {codes[sysmon.label]})")
    lines.append("")
    if sysmon_ok:
        lines.append(
            "**Result: EMPTY diff (trivially, both C tracer).** Consistent with "
            "baseline Finding 1: sysmon is inactive under branch coverage, so this "
            "adds no independent evidence beyond confirming the fallback."
        )
    else:
        lines.append(
            "**Result: NON-EMPTY diff.** Unexpected — investigate before relying on "
            "the sysmon overlay. The differing entries are:"
        )
        lines.append("")
        lines.append("```text")
        lines.extend(sysmon_diff)
        lines.append("```")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Comparison | Backend both sides | Diff | Gate |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(
        f"| serial vs parallel | C tracer | {'empty' if parallel_ok else 'NON-EMPTY'} | "
        f"{'xdist safe to ship' if parallel_ok else 'BLOCKS Phase 2'} |"
    )
    lines.append(
        f"| serial vs sysmon | C tracer (sysmon fell back) | {'empty' if sysmon_ok else 'NON-EMPTY'} | "
        "corroborates baseline Finding 1 (not a gate) |"
    )
    lines.append("")
    return "\n".join(lines)


def _count_tests() -> int:
    proc = subprocess.run(  # noqa: S603
        ["uv", "run", "pytest", "-n0", "--co", "-q"],
        check=False,
        capture_output=True,
        text=True,
    )
    m = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
    if m:
        return int(m.group(1))
    return sum(1 for line in proc.stdout.splitlines() if "::" in line)


def emit_evidence(out_path: Path) -> int:
    """Run each configuration, diff the reports, and write the evidence file.

    Returns a non-zero exit code if the **load-bearing** serial-vs-parallel diff
    is non-empty — that blocks Phase 2 and must fail loudly, never silently.
    """
    serial, parallel, sysmon = proof_configs()
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        serial_xml = tmpdir / "serial.xml"
        parallel_xml = tmpdir / "parallel.xml"
        sysmon_xml = tmpdir / "sysmon.xml"

        print("== Running configurations ==", flush=True)
        codes = {
            serial.label: _run_and_export(serial, serial_xml),
            parallel.label: _run_and_export(parallel, parallel_xml),
            sysmon.label: _run_and_export(sysmon, sysmon_xml),
        }

        print("== Diffing coverage reports ==", flush=True)
        parallel_diff = diff_reports(serial_xml, parallel_xml)
        sysmon_diff = diff_reports(serial_xml, sysmon_xml)

        test_count = _count_tests()
        markdown = render_evidence(
            serial=serial,
            parallel=parallel,
            sysmon=sysmon,
            codes=codes,
            parallel_diff=parallel_diff,
            sysmon_diff=sysmon_diff,
            test_count=test_count,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown)
    print(f"Wrote evidence to {out_path}", flush=True)

    if parallel_diff:
        print("!! LOAD-BEARING serial-vs-parallel diff is NON-EMPTY — this BLOCKS Phase 2:", flush=True)
        for entry in parallel_diff:
            print(f"   {entry}", flush=True)
        return 1
    print("serial-vs-parallel diff is EMPTY — xdist is coverage-safe to ship.", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emit-evidence",
        action="store_true",
        help="run each configuration, diff, and write the evidence file",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("epics/333-python-test-perf/evidence/coverage-equivalence.md"),
        help="evidence output path",
    )
    args = parser.parse_args(argv)

    if args.emit_evidence:
        return emit_evidence(args.out)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
