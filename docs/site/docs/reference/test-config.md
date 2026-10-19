# Test Config Reference (`[test]`)

A repo tunes how its **test stage** runs under `vrg-validate` through the
`[test]` section of its `vergil.toml`. The section is optional; every key
defaults so that a repo which omits `[test]` gets the fleet-wide default
behavior.

## Structure

```toml
[test]
parallel = false   # opt out of parallel test execution (default: true)
workers = "auto"   # xdist worker count: "auto" or a positive integer

[test.local-vm]    # override for one validation context (see below)
workers = 12
```

## Keys

| Key | Type | Default | Semantics |
|---|---|---|---|
| `parallel` | boolean | `true` | Whether the Python validation gate runs tests in parallel via pytest-xdist. On by default fleet-wide; set `false` to force serial execution |
| `workers` | `"auto"` or positive integer | `"auto"` | The pytest-xdist worker count (`-n`) when the gate runs in parallel |
| `[test.<context>].workers` | `"auto"` or positive integer | *(none)* | The worker count for one validation context: `ci`, `local-vm`, `cloud-vm`, or `physical-host`. Wins over `workers` in that context. A context subtable must set `workers` |

A non-boolean `parallel` is **rejected** rather than silently coerced, so a
mistyped opt-out (for example `parallel = "no"`) fails loudly at config-parse
time instead of quietly leaving parallelism on. A `workers` that is not
`"auto"` or a positive integer (for example `0`, `-2`, `true`, or `"many"`)
is rejected the same way.

## `parallel`

Parallelism is **on by default** for every Python repo. When `vrg-validate`
runs the Python test stage, the shared command
([`languages.py`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/lib/languages.py))
appends pytest-xdist's work-stealing flags — `-n <workers> --dist worksteal` —
to the base coverage-gate argv, so the suite fans out across workers (one per
reported CPU at the default `auto`; see [`workers`](#workers)).
On this repo's own suite that lever alone took the test stage from ~23.5 s to
~7.4 s (≈3.2× faster; see
[`epics/333-python-test-perf/evidence/baseline.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/epics/333-python-test-perf/evidence/baseline.md)),
and it was landed fleet-wide as the new default (epic
[vergil-project/.github#333](https://github.com/vergil-project/.github/issues/333)).

### How the flag is computed

The Python TEST command is **computed**, not stored as a fixed list. The shared
command derives the final argv from three inputs:

- **`[test].parallel`** — the repo's declared intent (this knob).
- **xdist availability** — a live probe of the **project's test environment**,
  run when the test stage starts (after the install stage has populated
  `.venv`). It asks the Python interpreter next to the `pytest` on `PATH` —
  the same `pytest` the command runs, with `.venv/bin` first — whether
  `xdist` is importable. It deliberately does **not** consult `vrg-validate`'s
  own interpreter: that is usually a uv tool venv without pytest-xdist, which
  once left every consumer repo running serially
  ([#3322](https://github.com/vergil-project/vergil-tooling/issues/3322)).
- **The resolved worker count** — see [`workers`](#workers).

`-n <workers> --dist worksteal` is appended **iff `xdist_available and
parallel`**. The order that produces:

- **Default (`parallel = true`) with pytest-xdist in the project environment**
  → parallel run. This is the fleet default: every Python repo carries
  `pytest-xdist` in its dev group.
- **`parallel = false`** → serial run, no error, and the probe does not run.
  This is the per-repo opt-out.
- **pytest-xdist not installed in the project environment** → serial run, no
  error. Availability is a probe, never a hard requirement.
- **The probe itself fails** (no `pytest` on `PATH`, no interpreter beside it,
  or the interpreter errors) → the test stage **fails loudly**. A broken probe
  is never read as "not installed".

The stage prints its decision — for example
`Test parallelism: -n 12 (from [test.local-vm].workers, context local-vm)` —
so the run log always shows whether, and how widely, the suite ran in parallel.

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

## `workers`

`auto` gives one xdist worker per CPU the machine **reports**. That is the right
default on a GitHub runner or a physical host, but it can be badly wrong in a VM
whose vCPUs exceed the host's real cores, or that shares them with other VMs:
the workers contend and most of the speed-up disappears. One measurement
([#3322](https://github.com/vergil-project/vergil-tooling/issues/3322)) on a
24-vCPU Lima VM on an 18-core Mac:

| Workers | Wall clock |
|---|---|
| serial | 196 s |
| 8 | 48–52 s |
| 12 | **42 s** |
| 16 | 56 s |
| `auto` (= 24) | 136–162 s |

The optimum depends on both the suite and the machine, so it has to be
**measured**, and it can be set at three levels:

```toml
[test]
workers = "auto"     # repo default — anything not listed below

[test.local-vm]
workers = 12         # this repo's Lima VM (measured)
```

### Validation contexts

A context names *where* validation runs:

| Context | Means |
|---|---|
| `ci` | A GitHub Actions runner (`GITHUB_ACTIONS=true`) |
| `local-vm` | A Lima agent VM (`vrg-whoami --platform`) |
| `cloud-vm` | An off-platform cloud agent VM, or any VM not positively confirmed local (`vrg-whoami --platform`, fail-closed) |
| `physical-host` | The developer's Mac itself (`vrg-whoami --platform`) |

`vrg-container-run` resolves the context **outside** the container, where the
platform is observable, and forwards it into the container as
`VRG_TEST_CONTEXT`. Setting `VRG_TEST_CONTEXT` yourself overrides that
detection. When `vrg-validate` runs without `vrg-container-run`, a GitHub
Actions runner is still recognized as `ci`; any other run has no context and
uses `[test].workers`. A `VRG_TEST_CONTEXT` that names no known context is an
error.

### Precedence

Highest first:

1. **`VRG_TEST_WORKERS`** — an environment override for one machine (for
   example a second Mac whose VM measured differently). `vrg-container-run`
   forwards it into the container. It takes the same values as `workers`, and
   an invalid value fails the run.
2. **`[test.<context>].workers`** — the repo's tuned count for the current
   context.
3. **`[test].workers`** — the repo default.
4. **`"auto"`**.

Because the tuned value lives in the repo's `vergil.toml`, retuning is a
commit. No VM rebuild or `vrg-vm update` is needed.

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
