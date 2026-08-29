# Test Config Reference (`[test]`)

A repo tunes how its **test stage** runs under `vrg-validate` through the
`[test]` section of its `vergil.toml`. The section is optional; every key
defaults so that a repo which omits `[test]` gets the fleet-wide default
behavior.

## Structure

```toml
[test]
parallel = false   # opt out of parallel test execution (default: true)
```

## Keys

| Key | Type | Default | Semantics |
|---|---|---|---|
| `parallel` | boolean | `true` | Whether the Python validation gate runs tests in parallel via pytest-xdist. On by default fleet-wide; set `false` to force serial execution |

A non-boolean `parallel` is **rejected** rather than silently coerced, so a
mistyped opt-out (for example `parallel = "no"`) fails loudly at config-parse
time instead of quietly leaving parallelism on.

## `parallel`

Parallelism is **on by default** for every Python repo. When `vrg-validate`
runs the Python test stage, the shared command
([`languages.py`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/lib/languages.py))
appends pytest-xdist's work-stealing flags — `-n auto --dist worksteal` — to
the base coverage-gate argv, so the suite fans out across every available core.
On this repo's own suite that lever alone took the test stage from ~23.5 s to
~7.4 s (≈3.2× faster; see
[`epics/333-python-test-perf/evidence/baseline.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/epics/333-python-test-perf/evidence/baseline.md)),
and it was landed fleet-wide as the new default (epic
[vergil-project/.github#333](https://github.com/vergil-project/.github/issues/333)).

### How the flag is computed

The Python TEST command is **computed**, not stored as a fixed list. The shared
command derives the final argv from two inputs:

- **`[test].parallel`** — the repo's declared intent (this knob).
- **xdist availability** — a live probe (`importlib.util.find_spec("xdist")`)
  that the caller supplies.

`-n auto --dist worksteal` is appended **iff `xdist_available and parallel`**.
The order that produces:

- **Default (`parallel = true`) with pytest-xdist present** → parallel run.
  This is the fleet default: the Python dev image ships `pytest-xdist`, so the
  flag resolves and the gate runs in parallel everywhere.
- **`parallel = false`** → serial run, no error. This is the per-repo opt-out.
- **pytest-xdist not installed** → serial run, no error. Availability is a
  probe, never a hard requirement, so a repo whose environment lacks xdist
  degrades to serial rather than failing.

The coverage gate (`--cov-branch --cov-fail-under=100`) is part of the base argv
and is therefore present in **every** computed command — serial or parallel,
opted-in or opted-out. Parallelism changes only *how fast* the suite runs, never
*what* the coverage gate measures.

### When to opt out

Set `parallel = false` only when the suite is **order-dependent** or otherwise
unsafe to distribute across workers (shared mutable fixtures, hard-coded ports,
tests that assume single-process execution). The knob exists so that no repo is
ever left known-broken by the fleet-wide default — but the goal is a suite that
runs correctly in parallel, so treat an opt-out as a bug to fix, not a
resting state.

## Levers evaluated and dropped

Two other test-stage levers were evaluated during epic
[vergil-project/.github#333](https://github.com/vergil-project/.github/issues/333)
and **deliberately not adopted**. They are recorded here so they are not
mistaken for available knobs:

- **`COVERAGE_CORE=sysmon` (the `sys.monitoring` coverage backend)** — inert
  under this fleet's inviolate `--cov-branch` gate. coverage.py cannot measure
  branches with `sys.monitoring` in the shipped coverage version and silently
  falls back to the C tracer, so setting it was misleading, not a speedup. See
  [`baseline.md` Finding 1](https://github.com/vergil-project/vergil-tooling/blob/develop/epics/333-python-test-perf/evidence/baseline.md).
- **`--import-mode=importlib`** — no measured speedup and not fleet-safe (it
  broke a real import in this repo's own suite). See
  [`baseline.md` Finding 2](https://github.com/vergil-project/vergil-tooling/blob/develop/epics/333-python-test-perf/evidence/baseline.md).

Neither is emitted by the shared command; parallelism via pytest-xdist is the
one universal lever that shipped.
