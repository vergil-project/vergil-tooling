# Progress Framework Design

**Date:** 2026-06-05
**Status:** Draft
**Scope:** `vergil-tooling` — `vergil_tooling/lib/progress.py` and all long-running procedural commands

---

## Problem

Long-running procedural commands (`vrg-release`, `vrg-validate`, and others) currently stream
raw output via bare `print()` calls. A full release run generates several pages of output over
several minutes, with no visual hierarchy, no stage boundaries, and no way to distinguish a
fatal error from a passing remark. When a stage like `build-images` fails, the process aborts
and the user has to scroll back through a wall of text to find what went wrong.

## Goals

- Give each long-running command a consistent, stage-aware progress display
- Let completed stages collapse to a single summary line so the user sees state, not noise
- Capture full verbose output to a log file without cluttering the terminal
- Distinguish three failure modes clearly and handle them consistently
- Work well both interactively and in CI

## Non-goals

- Atomic quick commands (`vrg-commit`, `vrg-gh`, etc.) — this framework is for multi-step
  procedures only
- A general-purpose TUI or dashboard — this is a progress display, not an interactive interface

---

## Architecture

### Module

All framework code lives in `vergil_tooling/lib/progress.py`. Commands import from it; there
is no separate package or submodule.

### Dependencies

`rich` is added as the first external dependency in `vergil-tooling`. It provides the live
display, spinner, and terminal detection used by the TTY renderer. Its CI-safe behavior
(auto-disables live rendering when stdout is not a TTY) aligns with the auto-detect renderer
selection described below.

---

## Stage API

Each procedural command defines its stages as a declarative list at the top of its `run()`
function. The pipeline runner iterates the list in order.

```python
from vergil_tooling.lib.progress import Stage, run_pipeline

STAGES = [
    Stage("audit",         run_audit,            mode="fail_fast", skip_flag="skip_audit"),
    Stage("changelog",     run_changelog,         mode="fail_defer"),
    Stage("version-bump",  run_version_bump,      mode="fail_defer"),
    Stage("tag-release",   run_tag_and_release,   mode="fail_defer"),
    Stage("build-images",  run_build_images,      mode="fail_defer"),
    Stage("publish-pypi",  run_publish_pypi,      mode="fail_defer"),
    Stage("docs-deploy",   run_docs_deploy,       mode="fail_defer"),
]

def run(ctx: ReleaseContext) -> int:
    return run_pipeline(ctx, STAGES)
```

**Rules:**
- If it is a stage, it is in the list. No inline one-offs.
- The list is the complete, readable spec of what the command does — in the order it does it.
- `run_pipeline` returns 0 on full success, 1 if any `fail_defer` or `fail_fast` stage failed.

### `Stage` fields

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | yes | Display name shown in progress output |
| `fn` | `Callable[[Any], None]` | yes | Function to call; receives the pipeline context |
| `mode` | `"warn" \| "fail_defer" \| "fail_fast"` | yes | Failure handling mode (see below) |
| `skip_flag` | `str \| None` | no | If set, `--skip-<skip_flag>` CLI arg demotes this stage to `warn` |

---

## Failure Modes

Every stage declares exactly one failure mode. There are three:

### `warn`
The stage failed but this is not considered an error. Show a yellow `⚠` in the progress
display and note it in the final summary. Exit code is not affected. Use sparingly — most
failures are not truly ignorable.

### `fail_defer`
The stage failed and this is a real error, but subsequent stages can still run. Record the
failure, show a red `✗` in the progress display, set the global error condition, and continue.
After all stages complete, raise `PipelineError` listing every deferred failure. Exit code: 1.

This is the correct mode for stages whose failure does not invalidate later stages. Example:
`build-images` failing does not prevent `publish-pypi` from running — the release can still
complete and the image build failure is reported at the end.

### `fail_fast`
The stage failed and continuing is meaningless or dangerous. Raise immediately, skip all
remaining stages, print the error and a pointer to the log. Exit code: 1.

Example: `audit` failing means there are known vulnerabilities in the release — proceeding
without addressing them is unsafe by default.

### Escape hatches

A `fail_fast` stage may declare a `skip_flag`. When the user passes `--skip-<skip_flag>` on
the command line, that stage is demoted to `warn` for that run — it is skipped with a visible
warning rather than aborting the process. The demotion is always shown in the final summary so
the skipped check is never silent.

```
$ vrg release 2.1.2 --skip-audit
⚠ audit — skipped via --skip-audit
✓ changelog — 3.2s
...
```

Escape hatches are defined per-stage in code, not as generic flags. `--skip-audit` exists
because the `audit` stage declares `skip_flag="skip_audit"`. No blanket `--skip-all` or
`--continue-on-error` flag is provided.

---

## Renderer Selection

The framework auto-detects the appropriate renderer based on the execution environment.
A `--output-format` flag overrides the auto-detection.

| Environment | Renderer | Detection |
|---|---|---|
| Interactive terminal | `RichRenderer` | `sys.stdout.isatty()` |
| GitHub Actions | `GhaRenderer` | `$GITHUB_ACTIONS == "true"` |
| Piped / other CI | `PlainRenderer` | fallback |

Detection order: TTY check first, then `$GITHUB_ACTIONS`, then plain fallback.

### `RichRenderer` (TTY)

Uses `rich` live display. While a stage is running:
- Completed stages show as `✓ name  elapsed` (green) or `✗ name  elapsed` (red) or `⚠ name` (yellow)
- The active stage shows a spinner, name, and a rolling window of the last N lines of its output
- Output older than the window is not shown on screen (it goes to the log file)

**`--output-window N`** controls the window size. Default: 5 lines. `0` means show all output
as it streams, then collapse the stage to a summary line on completion (equivalent to the full
stream behavior).

### `GhaRenderer` (GitHub Actions)

Emits `::group::<stage-name>` and `::endgroup::` annotations. Each stage becomes a collapsible
section in the Actions log UI. Failed stages auto-expand in the GHA interface. All subprocess
output is emitted inside the group — no truncation.

### `PlainRenderer` (piped / other CI)

Sequential flat output: a start marker, all subprocess output, then a completion marker.
No cursor movement. Works identically in any CI system and when output is piped locally.

```
→ audit  starting...
[all audit output]
✓ audit  2.1s

→ changelog  starting...
...
```

---

## Log File

Every run of a progress-aware command writes a full verbose log regardless of renderer.
All subprocess stdout and stderr is captured and written to the log, even lines not shown
in the terminal window.

**Path:** `.vergil/<command>-YYYYMMDD-HHMMSS.log`

Examples:
- `.vergil/vrg-release-20260605-143022.log`
- `.vergil/vrg-validate-20260605-091145.log`

The `.vergil/` directory is the project-local scratch directory for ephemeral tooling files.
It is git-ignored. The log path is printed at the bottom of the final summary so the user
always knows where to find full output.

---

## Final Summary

After all stages complete (or after a `fail_fast` abort), the framework always prints a
structured summary:

```
─────────────────────────────────────────────
⚠  warnings (non-fatal):
   audit — skipped via --skip-audit flag

✗  deferred failures:
   build-images — docker build exited 1
     ghcr.io/vergil-project/dev-base:2.1.2 failed at step 9/12
     full output → .vergil/vrg-release-20260605-143022.log

release 2.1.2 completed with errors  (total: 01:01)
PipelineError: 1 stage failed (build-images) · exit 1
```

On full success:

```
─────────────────────────────────────────────
✓  release 2.1.2 complete  (total: 00:58)
   full log → .vergil/vrg-release-20260605-143022.log
```

---

## CLI Flags

All progress-aware commands gain these flags automatically via the framework:

| Flag | Default | Description |
|---|---|---|
| `--output-window N` | `5` | Rolling window size for TTY renderer. `0` = full stream then collapse. |
| `--output-format FORMAT` | auto | Override renderer: `rich`, `gha`, or `plain`. |
| `--skip-<stage>` | — | Demote a `fail_fast` stage to `warn` (only available if the stage declares a `skip_flag`). |

---

## Scope and Rollout

### Commands in scope (long-running procedural)

- `vrg-release` — first adopter; reference implementation
- `vrg-validate` — multiple sequential lint/check stages
- Others as identified during implementation

### Commands out of scope (atomic / quick)

- `vrg-commit`, `vrg-gh`, `vrg-git`, `vrg-version`, `vrg-changelog` (standalone), and similar
  commands that run a single operation and return quickly

### Rollout order

1. Implement `vergil_tooling/lib/progress.py` with all three renderers and the `Stage` / `run_pipeline` API
2. Migrate `vrg-release` as the reference implementation — validates the full framework end-to-end
3. Migrate `vrg-validate` — second adopter, confirms the framework generalizes cleanly
4. Remaining long-running commands adopted incrementally

---

## Open Questions

None. All design decisions are resolved.
