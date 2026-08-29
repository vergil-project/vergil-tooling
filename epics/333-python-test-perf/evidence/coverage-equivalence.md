# Coverage-equivalence proof (epic #333, Task 2)

Recorded evidence that the epic's speed levers do not change what the inviolate `--cov-branch --cov-fail-under=100` gate measures. Produced by `scripts/perf/coverage_equivalence.py`: it runs the suite under each configuration, exports a Cobertura `coverage.xml`, and diffs the missing-line/branch sets with the unit-tested `diff_reports`. An empty diff means the two configurations are coverage-equivalent.

## Environment

- Recorded: 2026-08-29 11:06 UTC
- Interpreter: CPython 3.12.14 (aarch64)
- Platform: Linux 6.8.0-106-generic
- Tests collected: 5217
- Invocation: `uv run pytest --cov=src --cov-branch --cov-report=xml:<f>` plus each config's argv/env, run inside the dev container

## The load-bearing proof: serial vs parallel (C tracer)

Process-level parallelism (pytest-xdist) is the one lever that could genuinely change what coverage sees: each worker measures a subset of the suite and pytest-cov combines the per-worker data. If combination dropped or double-counted anything, the parallel run's missing set would differ from the serial run's. **This diff must be empty for the xdist command (Task 6) to ship.**

- Reference: `C-tracer serial` (exit code 0)
- Compared:  `C-tracer parallel (-n auto --dist worksteal)` (exit code 0)

**Result: EMPTY diff — coverage-equivalent.** Serial and parallel runs measure the identical missing-line/branch set. pytest-cov's per-worker combination preserves the gate exactly, so `-n auto --dist worksteal` is safe to ship under the 100% branch-coverage gate.

## The plan's C-tracer-vs-sysmon comparison (corroborates baseline Finding 1)

The plan also lists a C-tracer-vs-sysmon diff. Per the Phase-0 baseline (`evidence/baseline.md`, Finding 1), under `--cov-branch` coverage.py 7.14 warns `no-sysmon: sys.monitoring can't measure branches` and **silently falls back to the C tracer** — so a `COVERAGE_CORE=sysmon` run is really a C-tracer run. Both sides of this diff therefore use the same backend, and the diff is trivially empty. This is recorded as corroboration of the baseline finding, **not** treated as a meaningful equivalence gate: it does not prove sysmon-vs-C-tracer equivalence because sysmon never ran. We do not force sysmon on.

- Reference: `C-tracer serial` (exit code 0)
- Compared:  `sysmon serial (falls back to C tracer under --cov-branch)` (exit code 0)

**Result: EMPTY diff (trivially, both C tracer).** Consistent with baseline Finding 1: sysmon is inactive under branch coverage, so this adds no independent evidence beyond confirming the fallback.

## Summary

| Comparison | Backend both sides | Diff | Gate |
| --- | --- | --- | --- |
| serial vs parallel | C tracer | empty | xdist safe to ship |
| serial vs sysmon | C tracer (sysmon fell back) | empty | corroborates baseline Finding 1 (not a gate) |
