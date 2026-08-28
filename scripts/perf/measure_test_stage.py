#!/usr/bin/env python3
"""Warm-run measurement harness for the Python TEST stage (epic #333, Task 1).

Runs a given pytest invocation ``runs`` times after ``warmup`` discarded warm-up
runs (container up, deps synced), and reports the median wall-clock plus the raw
per-run list. The pytest argv/env is parametrized so the *same* harness measures
each cumulative configuration of the epic's levers:

    baseline (serial, C-tracer) -> +sysmon -> +xdist -n auto -> +worksteal
    -> +import-mode

It also captures a hotspot map by running once with ``--durations`` and once
with ``-X importtime``, and by statically ranking the ``subprocess`` call sites
in the test tree (Task 11's work-list / Task 10's stopping-target input).

Only :func:`median_seconds` is pure logic under the unit-test/coverage gate; the
run loop and evidence renderer are a thin ``scripts/`` driver (not coverage
measured — ``vrg-validate`` runs coverage over ``src/`` only).

Usage (inside the dev container so the numbers reflect the real gate)::

    vrg-container-run -- uv run python scripts/perf/measure_test_stage.py \\
        --emit-evidence --runs 3 \\
        --out epics/333-python-test-perf/evidence/baseline.md
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

# --------------------------------------------------------------------------- #
# Pure logic (the only unit-tested surface).
# --------------------------------------------------------------------------- #


def median_seconds(samples: list[float]) -> float:
    """Return the median of the timed run samples (warm-up already discarded)."""
    return median(samples)


# --------------------------------------------------------------------------- #
# Measurement driver.
# --------------------------------------------------------------------------- #

# Base pytest args mirroring the real ``vrg-validate`` TEST gate. The report
# files are dropped (they are irrelevant to timing) but the coverage flags stay
# so the measured cost includes coverage collection — the very thing sysmon
# accelerates. ``--cov-fail-under`` is omitted so a config whose coverage set
# legitimately differs (proven separately by Task 2) does not abort the timing
# run; a non-zero exit is still recorded and reported, never swallowed.
BASE_ARGS: list[str] = [
    "--cov=src",
    "--cov-branch",
    "--cov-report=term-missing",
]


@dataclass(frozen=True)
class MeasureConfig:
    """One cumulative configuration to time."""

    label: str
    pytest_args: list[str]
    env: dict[str, str] = field(default_factory=dict)
    runs: int = 5
    warmup: int = 1


@dataclass
class MeasureResult:
    """The timing outcome for one :class:`MeasureConfig`."""

    label: str
    per_run: list[float]
    median_seconds: float
    returncodes: list[int]


def _run_once(pytest_args: list[str], env_overlay: dict[str, str]) -> tuple[float, int]:
    """Time a single ``uv run pytest`` invocation; return (seconds, returncode)."""
    cmd = ["uv", "run", "pytest", *BASE_ARGS, *pytest_args]
    run_env = {**os.environ, **env_overlay}
    start = time.perf_counter()
    proc = subprocess.run(cmd, env=run_env, check=False)  # noqa: S603
    return time.perf_counter() - start, proc.returncode


def run(config: MeasureConfig) -> MeasureResult:
    """Run ``config`` ``warmup`` + ``runs`` times, discarding the warm-up runs."""
    for i in range(config.warmup):
        print(f"  [{config.label}] warm-up {i + 1}/{config.warmup} ...", flush=True)
        _run_once(config.pytest_args, config.env)

    per_run: list[float] = []
    returncodes: list[int] = []
    for i in range(config.runs):
        elapsed, rc = _run_once(config.pytest_args, config.env)
        per_run.append(elapsed)
        returncodes.append(rc)
        print(f"  [{config.label}] run {i + 1}/{config.runs}: {elapsed:.2f}s (rc={rc})", flush=True)

    return MeasureResult(
        label=config.label,
        per_run=per_run,
        median_seconds=median_seconds(per_run),
        returncodes=returncodes,
    )


def cumulative_configs(runs: int, warmup: int) -> list[MeasureConfig]:
    """The five cumulative lever configurations, in attribution order."""
    sysmon = {"COVERAGE_CORE": "sysmon"}
    return [
        # C-tracer forced explicitly so the baseline is deterministic regardless
        # of any inherited COVERAGE_CORE; ``-n0`` overrides this repo's addopts.
        MeasureConfig("baseline (serial, C-tracer)", ["-n0"], {"COVERAGE_CORE": "ctrace"}, runs, warmup),
        MeasureConfig("+sysmon (serial)", ["-n0"], sysmon, runs, warmup),
        MeasureConfig("+xdist -n auto", ["-n", "auto"], sysmon, runs, warmup),
        MeasureConfig("+worksteal", ["-n", "auto", "--dist", "worksteal"], sysmon, runs, warmup),
        MeasureConfig(
            "+import-mode",
            ["-n", "auto", "--dist", "worksteal", "--import-mode=importlib"],
            sysmon,
            runs,
            warmup,
        ),
    ]


# --------------------------------------------------------------------------- #
# Hotspot map.
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(r"^\s*([0-9]+\.[0-9]+)s\s+(call|setup|teardown)\s+(\S+)\s*$")
_IMPORTTIME_RE = re.compile(r"^import time:\s+([0-9]+)\s*\|\s+([0-9]+)\s*\|\s+(.+?)\s*$")
# A real, inline subprocess invocation (a refactor candidate if it exercises our
# own logic): the ``subprocess.<method>(`` call form.
_SUBPROCESS_CALL_RE = re.compile(r"subprocess\.(run|Popen|call|check_call|check_output)\s*\(")
# Any textual mention of a subprocess method — including ``patch("subprocess.run")``
# mock targets. This is the metric the epic spec's "~551 sites" figure counts.
_SUBPROCESS_MENTION_RE = re.compile(r"subprocess\.(run|Popen|call|check_call|check_output)")


def parse_durations(stdout: str, top: int) -> list[tuple[float, str, str]]:
    """Parse pytest ``--durations`` output into (seconds, phase, nodeid) rows."""
    rows: list[tuple[float, str, str]] = []
    for line in stdout.splitlines():
        m = _DURATION_RE.match(line)
        if m:
            rows.append((float(m.group(1)), m.group(2), m.group(3)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows[:top]


def parse_importtime(stderr: str, top: int) -> list[tuple[int, str]]:
    """Parse ``python -X importtime`` stderr into (cumulative_us, module) rows."""
    rows: list[tuple[int, str]] = []
    for line in stderr.splitlines():
        m = _IMPORTTIME_RE.match(line)
        if m:
            rows.append((int(m.group(2)), m.group(3)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows[:top]


def count_subprocess_sites(tests_dir: Path) -> list[tuple[int, int, str]]:
    """Rank test files by ``subprocess`` usage.

    Returns ``(real_calls, mentions, path)`` rows sorted by ``mentions``
    descending. ``real_calls`` counts inline ``subprocess.<method>(`` invocations
    (the true refactor candidates); ``mentions`` counts every textual reference
    including ``patch("subprocess.run")`` mock targets (the spec's "~551" metric).
    """
    counts: list[tuple[int, int, str]] = []
    for path in sorted(tests_dir.rglob("*.py")):
        lines = path.read_text().splitlines()
        calls = sum(1 for line in lines if _SUBPROCESS_CALL_RE.search(line))
        mentions = sum(1 for line in lines if _SUBPROCESS_MENTION_RE.search(line))
        if mentions:
            counts.append((calls, mentions, str(path)))
    counts.sort(key=lambda r: r[1], reverse=True)
    return counts


def _capture_durations(top: int) -> str:
    proc = subprocess.run(  # noqa: S603
        ["uv", "run", "pytest", "-n0", f"--durations={top}", "-q", *BASE_ARGS],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _capture_importtime() -> str:
    proc = subprocess.run(  # noqa: S603
        ["uv", "run", "python", "-X", "importtime", "-m", "pytest", "-n0", "--co", "-q"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stderr


# --------------------------------------------------------------------------- #
# Evidence rendering.
# --------------------------------------------------------------------------- #


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
    # Fallback: count collected node lines.
    return sum(1 for line in proc.stdout.splitlines() if "::" in line)


def render_evidence(
    results: list[MeasureResult],
    *,
    test_count: int,
    durations: list[tuple[float, str, str]],
    importtime: list[tuple[int, str]],
    subprocess_sites: list[tuple[int, int, str]],
    runs: int,
    warmup: int,
) -> str:
    """Render the baseline evidence markdown (prose-scope, MD013 relaxed)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    baseline_median = results[0].median_seconds if results else 0.0

    lines: list[str] = []
    lines.append("# Test-stage performance baseline (epic #333, Task 1)")
    lines.append("")
    lines.append(
        "Recorded evidence for Phase 0 of the fleet-wide Python test-performance "
        "epic. This is the measurement record produced by "
        "`scripts/perf/measure_test_stage.py`; later tasks re-run the same harness "
        "to measure their deltas against these numbers."
    )
    lines.append("")
    lines.append("## Environment")
    lines.append("")
    lines.append(f"- Recorded: {now}")
    lines.append(f"- Interpreter: CPython {platform.python_version()} ({platform.machine()})")
    lines.append(f"- Platform: {platform.system()} {platform.release()}")
    lines.append(f"- Tests collected: {test_count}")
    lines.append(f"- Sampling: median of {runs} timed warm run(s) after {warmup} discarded warm-up(s)")
    lines.append(
        "- Invocation: `uv run pytest --cov=src --cov-branch --cov-report=term-missing` "
        "plus each config's argv/env, run inside the dev container"
    )
    lines.append("")
    lines.append("## Cumulative configuration medians")
    lines.append("")
    lines.append(
        "Each row adds one lever to the row above it, so the deltas attribute the "
        "wall-clock change to each lever. `speedup` is baseline-median / row-median."
    )
    lines.append("")
    lines.append("| Configuration | Median (s) | Per-run (s) | Speedup vs baseline | Exit codes |")
    lines.append("| --- | ---: | --- | ---: | --- |")
    for r in results:
        per = ", ".join(f"{x:.2f}" for x in r.per_run)
        speedup = (baseline_median / r.median_seconds) if r.median_seconds else 0.0
        rcs = ", ".join(str(c) for c in r.returncodes)
        rc_note = rcs if set(r.returncodes) == {0} else f"{rcs} (non-zero: see notes)"
        lines.append(f"| {r.label} | {r.median_seconds:.2f} | {per} | {speedup:.2f}x | {rc_note} |")
    lines.append("")
    lines.append(
        "> Exit codes are recorded verbatim, never swallowed. A non-zero code "
        "under `+import-mode` typically means collection changed (duplicate "
        "basenames / unpackaged tests / sys.path semantics). Note that a "
        "`COVERAGE_CORE=sysmon` row can still exit 0 while coverage silently falls "
        "back to the C tracer (it warns `no-sysmon` when sys.monitoring cannot "
        "measure branches) — inspect the run log, do not assume sysmon was active. "
        "Author any interpretation as a hand-added Key findings section."
    )
    lines.append("")
    lines.append("## sysmon + xdist projection (Task 11 stopping target)")
    lines.append("")
    lines.append(
        "The `+worksteal` median is the projected steady-state of the universal "
        "levers (sysmon + xdist worksteal) before any subprocess-hotspot refactor. "
        "Task 11 refactors the hotspots below until the test-stage wall-clock is "
        "within the agreed margin of this projection, or the top-N by duration are "
        "all addressed, whichever comes first."
    )
    if len(results) >= 4:
        proj = results[3].median_seconds
        lines.append("")
        lines.append(f"- Projection (sysmon + `-n auto --dist worksteal`): **{proj:.2f}s** median")
    lines.append("")
    lines.append("## Hotspot map")
    lines.append("")
    lines.append("### Slowest tests (`--durations`)")
    lines.append("")
    if durations:
        lines.append("| Seconds | Phase | Node ID |")
        lines.append("| ---: | --- | --- |")
        for secs, phase, nodeid in durations:
            lines.append(f"| {secs:.2f} | {phase} | `{nodeid}` |")
    else:
        lines.append("_No durations captured (parser found no rows in the pytest output)._")
    lines.append("")
    lines.append("### Import-time offenders (`python -X importtime`, by cumulative)")
    lines.append("")
    if importtime:
        lines.append("| Cumulative (us) | Module |")
        lines.append("| ---: | --- |")
        for cum, mod in importtime:
            lines.append(f"| {cum} | `{mod}` |")
    else:
        lines.append("_No import-time rows captured._")
    lines.append("")
    lines.append("### Subprocess hotspots (ranked by static site count)")
    lines.append("")
    lines.append(
        "Per test file: `real calls` counts inline "
        "`subprocess.run/Popen/call/check_call/check_output(` invocations; "
        "`mentions` counts every textual reference including "
        "`patch(\"subprocess.run\")` mock targets (the metric behind the spec's "
        "\"~551 sites\" figure). Cross-referenced with the slowest-tests table, the "
        "top `real calls` rows are Task 11's refactor work-list — inline calls that "
        "exercise *our* argv-construction/parsing logic (not genuine integration) "
        "are the candidates to replace with argv-asserting fakes."
    )
    lines.append("")
    if subprocess_sites:
        total_calls = sum(c for c, _, _ in subprocess_sites)
        total_mentions = sum(m for _, m, _ in subprocess_sites)
        lines.append(
            f"Totals across {len(subprocess_sites)} files: {total_calls} inline "
            f"`subprocess.*(` calls, {total_mentions} total mentions (mostly mock "
            "targets — the suite already mocks subprocess heavily, a key Phase-0 "
            "finding for scoping Task 11)."
        )
        lines.append("")
        lines.append("| Real calls | Mentions | Test file |")
        lines.append("| ---: | ---: | --- |")
        for calls, mentions, path in subprocess_sites:
            lines.append(f"| {calls} | {mentions} | `{path}` |")
    else:
        lines.append("_No subprocess sites found._")
    lines.append("")
    return "\n".join(lines)


def emit_evidence(out_path: Path, *, runs: int, warmup: int, durations_top: int) -> int:
    """Run every configuration + hotspot capture and write the evidence file."""
    print("== Measuring cumulative configurations ==", flush=True)
    results = [run(cfg) for cfg in cumulative_configs(runs, warmup)]

    print("== Capturing hotspot map ==", flush=True)
    test_count = _count_tests()
    durations = parse_durations(_capture_durations(durations_top), durations_top)
    importtime = parse_importtime(_capture_importtime(), 25)
    subprocess_sites = count_subprocess_sites(Path("tests"))

    markdown = render_evidence(
        results,
        test_count=test_count,
        durations=durations,
        importtime=importtime,
        subprocess_sites=subprocess_sites,
        runs=runs,
        warmup=warmup,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown)
    print(f"Wrote evidence to {out_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit-evidence", action="store_true", help="run all configs and write the evidence file")
    parser.add_argument("--runs", type=int, default=5, help="timed runs per config (default: 5)")
    parser.add_argument("--warmup", type=int, default=1, help="discarded warm-up runs per config (default: 1)")
    parser.add_argument("--durations-top", type=int, default=25, help="rows for --durations (default: 25)")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("epics/333-python-test-perf/evidence/baseline.md"),
        help="evidence output path",
    )
    args = parser.parse_args(argv)

    if args.emit_evidence:
        return emit_evidence(args.out, runs=args.runs, warmup=args.warmup, durations_top=args.durations_top)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
