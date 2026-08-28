# Test-stage performance baseline (epic #333, Task 1)

Recorded evidence for Phase 0 of the fleet-wide Python test-performance epic. This is the measurement record produced by `scripts/perf/measure_test_stage.py`; later tasks re-run the same harness to measure their deltas against these numbers.

## Environment

- Recorded: 2026-08-28 17:07 UTC
- Interpreter: CPython 3.12.14 (aarch64)
- Platform: Linux 6.8.0-106-generic
- Tests collected: 5192
- Sampling: median of 5 timed warm run(s) after 1 discarded warm-up(s)
- Invocation: `uv run pytest --cov=src --cov-branch --cov-report=term-missing` plus each config's argv/env, run inside the dev container

## Cumulative configuration medians

Each row adds one lever to the row above it, so the deltas attribute the wall-clock change to each lever. `speedup` is baseline-median / row-median.

| Configuration | Median (s) | Per-run (s) | Speedup vs baseline | Exit codes |
| --- | ---: | --- | ---: | --- |
| baseline (serial, C-tracer) | 23.47 | 22.83, 23.47, 27.21, 24.48, 23.21 | 1.00x | 0, 0, 0, 0, 0 |
| +sysmon (serial) | 23.01 | 23.01, 23.01, 22.97, 23.01, 23.01 | 1.02x | 0, 0, 0, 0, 0 |
| +xdist -n auto | 7.71 | 7.70, 7.71, 7.71, 7.81, 7.78 | 3.04x | 0, 0, 0, 0, 0 |
| +worksteal | 7.41 | 7.33, 7.44, 7.34, 7.41, 7.45 | 3.17x | 0, 0, 0, 0, 0 |
| +import-mode | 7.42 | 7.46, 7.42, 7.44, 7.34, 7.30 | 3.16x | 1, 1, 1, 1, 1 (non-zero: see notes) |

> Exit codes are recorded verbatim. A non-zero code under `+import-mode` means collection changed (see Finding 2); the sysmon rows exited 0 but did **not** actually run sysmon (see Finding 1).

## Key findings

These interpret the measured numbers above and directly reshape later phases.
Each separates **data** (what the run produced) from **judgment** (the
recommendation on top of it). Findings 1, 2, and 4 should be raised to the epic
driver before the phases they touch begin.

### Finding 1 — sysmon is silently inactive under branch coverage (gates Task 4 / Phase 1)

**Data.** Every run that set `COVERAGE_CORE=sysmon` emitted, from coverage 7.14.0:
`CoverageWarning: Can't use core=sysmon: sys.monitoring can't measure branches in this version, using default core (no-sysmon)`
(reference: <https://coverage.readthedocs.io/en/7.14.0/messages.html#warning-no-sysmon>).
coverage therefore ran the default **C tracer**, not sysmon. The numbers agree:
`+sysmon (serial)` at 23.01s is within noise of the 23.47s baseline (1.02x), i.e.
the same C tracer measured twice.

**Judgment.** Under this epic's inviolate `--cov-branch` gate, sysmon delivers
**no speedup** on the current coverage/CPython combination — `sys.monitoring`
branch support is not available in coverage 7.14.0, and coverage falls back
rather than erroring. This contradicts the spec's §4.1 assumption ("branch
support followed around 7.7"). Phase 1 (Task 4) must not assume a sysmon win:
either ship the overlay as a guarded no-op-until-supported and revisit when the
coverage/CPython floor moves, or re-scope Phase 1. Critically, Task 2's
coverage-equivalence proof will pass **trivially** here because both sides run
the C tracer — that must not be read as "sysmon proven equivalent."

### Finding 2 — the suite is not importlib-safe as-is (gates Task 10 / Phase 3)

**Data.** All five `+import-mode` runs exited rc=1 with
`ERROR tests/vergil_tooling/test_measure_test_stage.py - ImportError`; the other
5190 tests passed. Cause: this harness's own test imports
`from scripts.perf.measure_test_stage import median_seconds`, which resolves only
because pytest's default `prepend` import mode inserts the repo root onto
`sys.path`; under `--import-mode=importlib` that insertion does not happen, so
`scripts.perf` is unimportable and collection errors.

**Judgment.** This is a concrete, reproducible instance of the collection-safety
surface Task 10/Task 3 must gate on: importlib mode changes `sys.path` semantics
and breaks a real import in this repo today. On the numbers, import-mode is also
**not measurably faster** (7.42s vs the 7.41s worksteal median), so it fails the
spec §7 Phase 3 "measurable speedup" bar and is a drop candidate on hygiene
alone. The default gate (prepend mode) is unaffected — `vrg-validate` stays green.

### Finding 3 — parallelism (xdist) is the only material lever on this suite

**Data.** baseline 23.47s -> `+xdist -n auto` 7.71s (3.04x) -> `+worksteal` 7.41s
(3.17x; worksteal adds ~4% over default `load` distribution). sysmon 1.02x,
import-mode 1.00x relative to worksteal.

**Judgment.** For vergil-tooling's suite on this host, essentially the entire
measured win is process-level parallelism, and worksteal is a small but real
improvement over default load balancing (consistent with one 6.3k-line test file
dominating). The universal-lever projection for Task 11's stopping target is the
worksteal median, **not** a sysmon+xdist stack.

### Finding 4 — no dominant subprocess hotspot remains (re-scopes Task 11 / Phase 4)

**Data.** The test tree has **12** inline `subprocess.*(` calls versus **541**
textual mentions that are overwhelmingly `patch("subprocess.run")` mock targets.
The slowest single test is 0.33s; the import-time table is dominated by
framework imports (`pytest`, `_pytest._code`, `pygments`), not our code.

**Judgment.** The spec's premise of "~551 real `subprocess.run` sites" to refactor
is **not** borne out — those are almost entirely already-mocked patch targets, not
live subprocess spawns. With xdist active (7.41s) there is no fat subprocess
hotspot left to remove; the handful of real inline calls (4 in
`test_report_emission.py`, the rest scattered) are already fast. Task 11's
measured stopping target is therefore essentially already met by parallelism
alone — recommend Task 11 be reduced or dropped, with this evidence as the
record. Decision to the epic driver.

## sysmon + xdist projection (Task 11 stopping target)

The `+worksteal` median is the projected steady-state of the **effective**
universal lever on this suite — xdist worksteal (sysmon is inactive under branch
coverage, Finding 1) — before any subprocess-hotspot refactor. Task 11 (if it
proceeds, Finding 4) refactors hotspots until the test-stage wall-clock is within
the agreed margin of this projection, or the top-N by duration are all addressed,
whichever comes first.

- Projection (effective universal lever, `-n auto --dist worksteal`): **7.41s** median

## Hotspot map

### Slowest tests (`--durations`)

| Seconds | Phase | Node ID |
| ---: | --- | --- |
| 0.33 | call | `tests/vergil_tooling/test_report_emission.py::test_typecheck_mypy_writes_junit_report` |
| 0.21 | call | `tests/vergil_tooling/test_progress.py::test_rich_renderer_window_zero_streams` |
| 0.20 | call | `tests/vergil_tooling/test_lima.py::TestProvisionMonitor::test_tails_and_emits_heartbeat` |
| 0.14 | call | `tests/vergil_tooling/test_report_emission.py::test_audit_pip_licenses_writes_report` |
| 0.13 | call | `tests/vergil_tooling/test_report_emission.py::test_test_command_report_flags_write_files` |
| 0.12 | call | `tests/vergil_tooling/test_vrg_reword.py::test_reword_midchain_commit_end_to_end` |
| 0.12 | call | `tests/vergil_tooling/test_vrg_reword.py::test_reword_head_commit_end_to_end` |
| 0.11 | call | `tests/vergil_tooling/pr_workflow/test_cli_e2e.py::test_report_ready_after_unfreeze_refreezes` |
| 0.11 | call | `tests/vergil_tooling/pr_workflow/test_cli_e2e.py::test_report_ready_rerun_overwrites` |
| 0.10 | call | `tests/vergil_tooling/pr_workflow/test_cli_e2e.py::test_report_ready_rejects_stale_issue` |
| 0.09 | call | `tests/vergil_tooling/test_validate_common.py::test_main_docs_site_long_line_fails_md013` |
| 0.09 | call | `tests/vergil_tooling/test_validate_common.py::test_main_epics_long_line_passes_md013_relaxed` |
| 0.08 | call | `tests/vergil_tooling/pr_workflow/test_cli_e2e.py::test_report_ready_initializes_and_records` |
| 0.07 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-finalize-pr]` |
| 0.07 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-submit-pr]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-vm]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-worktree-status]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-release]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-update-deps]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-commit]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-docs-stage]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-ci-evidence]` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-github-repo-init]` |
| 0.06 | call | `tests/vergil_tooling/pr_workflow/test_cli_e2e.py::test_report_ready_pushes_relay_ref` |
| 0.06 | call | `tests/vergil_tooling/test_help_coverage.py::test_tool_answers_help[vrg-github-repo-config]` |

### Import-time offenders (`python -X importtime`, by cumulative)

| Cumulative (us) | Module |
| ---: | --- |
| 43464 | `pytest` |
| 24688 | `_pytest._code` |
| 24633 | `_pytest._code.code` |
| 12848 | `_pytest._io` |
| 12789 | `_pytest._io.terminalwriter` |
| 9052 | `_pytest.assertion` |
| 8852 | `_pytest.assertion.rewrite` |
| 8150 | `pygments.formatters.terminal` |
| 7758 | `pygments.formatters` |
| 7376 | `pygments.plugin` |
| 7330 | `importlib.metadata` |
| 5395 | `_pytest.fixtures` |
| 4260 | `importlib.metadata._adapters` |
| 4085 | `email.message` |
| 3315 | `email.utils` |
| 3069 | `xdist.newhooks` |
| 2989 | `_pytest.assertion.util` |
| 2934 | `execnet` |
| 2778 | `_pytest.config` |
| 2672 | `ast` |
| 2639 | `site` |
| 2413 | `_pytest.nodes` |
| 2335 | `_pytest.legacypath` |
| 2332 | `dataclasses` |
| 2214 | `_pytest.mark.structures` |

### Subprocess hotspots (ranked by static site count)

Per test file: `real calls` counts inline `subprocess.run/Popen/call/check_call/check_output(` invocations; `mentions` counts every textual reference including `patch("subprocess.run")` mock targets (the metric behind the spec's "~551 sites" figure). Cross-referenced with the slowest-tests table, the top `real calls` rows are Task 11's refactor work-list — inline calls that exercise *our* argv-construction/parsing logic (not genuine integration) are the candidates to replace with argv-asserting fakes.

Totals across 36 files: 12 inline `subprocess.*(` calls, 541 total mentions (mostly mock targets — the suite already mocks subprocess heavily, a key Phase-0 finding for scoping Task 11).

| Real calls | Mentions | Test file |
| ---: | ---: | --- |
| 0 | 62 | `tests/vergil_tooling/test_vm_transport.py` |
| 0 | 60 | `tests/vergil_tooling/test_vrg_git.py` |
| 0 | 49 | `tests/vergil_tooling/test_github.py` |
| 0 | 42 | `tests/vergil_tooling/test_container_cache.py` |
| 2 | 36 | `tests/vergil_tooling/test_vm_cloud.py` |
| 0 | 35 | `tests/vergil_tooling/test_vrg_submit_pr.py` |
| 0 | 32 | `tests/vergil_tooling/test_vrg_gh.py` |
| 0 | 29 | `tests/vergil_tooling/test_vrg_finalize_pr.py` |
| 0 | 26 | `tests/vergil_tooling/test_vm_provider.py` |
| 0 | 21 | `tests/vergil_tooling/test_validate_common.py` |
| 0 | 21 | `tests/vergil_tooling/test_vrg_vm.py` |
| 0 | 18 | `tests/vergil_tooling/test_version.py` |
| 0 | 14 | `tests/vergil_tooling/test_git.py` |
| 0 | 12 | `tests/vergil_tooling/test_lima.py` |
| 0 | 12 | `tests/vergil_tooling/test_promote.py` |
| 0 | 9 | `tests/vergil_tooling/test_changelog.py` |
| 0 | 8 | `tests/vergil_tooling/test_repo_init.py` |
| 0 | 7 | `tests/vergil_tooling/test_semgrep.py` |
| 2 | 5 | `tests/vergil_tooling/test_fleet_sweep.py` |
| 0 | 5 | `tests/vergil_tooling/test_retry.py` |
| 0 | 4 | `tests/vergil_tooling/test_container.py` |
| 4 | 4 | `tests/vergil_tooling/test_report_emission.py` |
| 0 | 4 | `tests/vergil_tooling/test_vrg_container_test.py` |
| 0 | 3 | `tests/vergil_tooling/test_release_preflight.py` |
| 0 | 3 | `tests/vergil_tooling/test_vm_guest.py` |
| 0 | 3 | `tests/vergil_tooling/test_vrg_changelog.py` |
| 0 | 3 | `tests/vergil_tooling/test_vrg_container_cache.py` |
| 0 | 3 | `tests/vergil_tooling/test_vrg_promote.py` |
| 0 | 2 | `tests/vergil_tooling/test_github_config_lib.py` |
| 0 | 2 | `tests/vergil_tooling/test_release_handoff.py` |
| 1 | 2 | `tests/vergil_tooling/test_vrg_reword.py` |
| 1 | 1 | `tests/vergil_tooling/pr_workflow/test_cli_e2e.py` |
| 1 | 1 | `tests/vergil_tooling/pr_workflow/test_github_transport.py` |
| 1 | 1 | `tests/vergil_tooling/test_help_coverage.py` |
| 0 | 1 | `tests/vergil_tooling/test_progress.py` |
| 0 | 1 | `tests/vergil_tooling/test_trivy.py` |
