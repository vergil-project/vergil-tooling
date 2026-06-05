# Progress Framework Design

**Date:** 2026-06-05
**Status:** Reviewed (pushback review applied 2026-06-05)
**Scope:** `vergil-tooling` — `vergil_tooling/lib/progress.py` and long-running, human-invoked procedural commands

---

## Problem

Long-running procedural commands (`vrg-release`, `vrg-validate`, and others) currently stream
raw output via bare `print()` calls. A full release run generates several pages of output over
several minutes, with no visual hierarchy, no stage boundaries, and no way to distinguish a
fatal error from a passing remark. When a stage like `build-images` fails, the process aborts
and the user has to scroll back through a wall of text to find what went wrong.

## Audience

This framework exists for **human consumption** of long-running command output. The
distinguishing axis is *who runs the command*, not where it runs. Human-invoked procedural
commands (`vrg-release`, `vrg-finalize-pr`, `vrg-submit-pr`, and peers) get the full
rendering treatment. Agent-invoked runs — including anything executed inside the dev
container or VM, where no TTY exists — fall back to `PlainRenderer`, and that is by design:
agent transcripts do not need human-optimized live rendering, only complete, parseable output.

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
- Rich/GHA rendering for agent-invoked or containerized runs — these render plain by design
  (see Audience). Making `vrg-container-run` renderer-transparent (forwarding TTY-ness and
  `$GITHUB_ACTIONS` into the container) is an optional follow-up, pursued only if it proves
  cheap and worthwhile — it is not a requirement of this design.
- A run-but-tolerate escape hatch (demote a failing stage to `warn` while still running it).
  No current stage needs one: `fail_fast` overrides want *don't-run* (the gate already fired
  once and the human decided to proceed), and everything else is `fail_defer`, which already
  runs to completion and reports at the end. If a stage ever needs its failure output while
  the failure itself is pre-forgiven, a `tolerate_flag` field can be added to `Stage` without
  breaking anything.

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

### Relationship to existing machinery

This framework **replaces** the structured phase logging that issue #949 added to
`vrg-release`, rather than layering on top of it:

- **Phase runner.** `lib/release/orchestrator.py` currently iterates a bespoke
  `phases: list[tuple[str, Callable[[ReleaseContext], None]]]` with `=== Phase: name ===`
  entry/exit markers. `run_pipeline` subsumes this runner. The real phase names as of this
  writing are `prepare`, `merge-release`, `confirm-main`, `back-merge-bump`,
  `confirm-develop`, `promote`, `close-finalize`, and `consumer-refresh` — the migration maps
  these onto `Stage` entries (the final stage decomposition is settled during migration; the
  `STAGES` example below is illustrative, not the real list).
- **`--verbose`.** The #949 `--verbose` flag (full vs. summarized subprocess output) is
  subsumed by `--output-window` and the always-on log file. It is removed at migration.
- **`--skip-cd-docs`.** An ad-hoc stopgap for the blocking-failure behavior this framework
  eliminates: with `docs-deploy` as a `fail_defer` stage, the pipeline runs to completion and
  reports the failure at the end, which is the desired behavior. The flag is removed at
  migration. (`--skip-audit` survives, expressed as `skip_flag="skip_audit"` on the audit
  stage.)
- **Tracking-issue comments.** The per-phase `comment_phase_complete` / `comment_phase_failed`
  hooks remain `vrg-release`-local, invoked from inside the stage functions. The framework
  does not own them — they are stage side effects, not pipeline concerns.
- **Subprocess capture.** #949 gave `git.run()` / `github.run()` a capture-then-print-on-success
  model. For subprocesses executed inside a pipeline stage, that model is superseded by the
  streaming contract below (a captured-then-printed `docker build` would leave the rolling
  window empty for minutes). Outside pipelines, the #949 behavior is unchanged. The CI
  pollers (`gh pr checks --watch`, `gh run watch`) move to the streaming runner while
  **retaining** transient-failure retry via a streaming retry wrapper built on
  `lib/retry.py` — GitHub API flakiness makes retry essential, not optional.

---

## Subprocess Execution Contract

The rolling window, the log file, and the GHA group content all require observing subprocess
output **line-by-line while the subprocess runs**. The framework therefore owns subprocess
execution for pipeline stages:

- `progress.py` provides a progress-aware runner (working name `progress.run(cmd, ...)`) that
  spawns the child with piped stdout/stderr, pumps both streams concurrently, and feeds each
  line to the active renderer **and** the log file as it arrives.
- Stages route subprocess calls through this runner. `git.run()` and `github.run()` gain a
  pass-through so existing call sites participate in streaming when executing inside a
  pipeline stage, and keep their current behavior otherwise.
- On failure the runner raises `CalledProcessError` carrying the captured output (preserving
  the #949 guarantee that error objects carry their context).

**Print capture.** `run_pipeline` redirects `sys.stdout` and `sys.stderr` into the framework
while a stage executes, so bare `print()` calls anywhere in stage code — including deep
library calls — route through the active renderer and the log automatically. This is why
capture-then-print wrappers like `github.run()` need no modification to participate: their
printed output is captured at the redirection layer. Future adopters should not wrap plain
prints in framework calls; redirection already covers them.

**Accepted caveats** (stated here so the implementation does not re-litigate them):

- Children detect a pipe, not a TTY, and may block-buffer — window lines can arrive in bursts
  rather than smoothly. Accepted; no PTY allocation.
- ANSI escape sequences from children are passed through to the terminal renderer but stripped
  from the log file, so logs stay grep-able.

---

## Stage API

Each procedural command defines its stages as a declarative list at the top of its `run()`
function. The pipeline runner iterates the list in order. (The example below is illustrative;
`vrg-release`'s real stage list is settled during migration — see Relationship to existing
machinery.)

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
| `fn` | `Callable[[Any], None]` | yes | Function to call; receives the pipeline context; signals failure by raising |
| `mode` | `"warn" \| "fail_defer" \| "fail_fast"` | yes | Failure handling mode (see below) |
| `skip_flag` | `str \| None` | no | `fail_fast` stages only. If set, `--skip-<skip_flag>` skips the stage entirely |

### Failure contract

A stage fails **if and only if `fn` raises**. There are no sentinel return values and no
special exception class. The framework catches the exception, records `(stage, exception)`,
applies the stage's `mode`, writes the full traceback to the log file, and shows a one-line
cause in the terminal and final summary. `CalledProcessError` from the subprocess runner is
the common case and already carries the captured output.

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
After all stages complete, the final summary reports a `PipelineError` naming every deferred
failure; `run_pipeline` returns exit code 1 (it reports rather than raises, so commands can
simply `sys.exit(run_pipeline(...))` without error-handling boilerplate).

This is the correct mode for stages whose failure does not invalidate later stages. Example:
`build-images` failing does not prevent `publish-pypi` from running — the release can still
complete and the image build failure is reported at the end.

### `fail_fast`
The stage failed and continuing is meaningless or dangerous. Raise immediately, skip all
remaining stages, print the error and a pointer to the log. Exit code: 1.

Example: `audit` failing means there are known vulnerabilities in the release — proceeding
without addressing them is unsafe by default.

### Interrupts

`KeyboardInterrupt` is treated like a `fail_fast` on the active stage: the live display is
torn down cleanly, the active stage is marked `✗ interrupted`, the standard final summary is
printed (including the log path), and the process exits 130.

### Escape hatches

A `fail_fast` stage may declare a `skip_flag`. When the user passes `--skip-<skip_flag>` on
the command line, that stage is **not executed at all** — it renders as
`⚠ <name> — skipped via --skip-<skip_flag>` and is always listed in the final summary, so the
skipped check is never silent. Skipping means *don't run*: a `fail_fast` gate has already
fired (or is expected to), the human has seen it, and re-running it adds nothing.

`skip_flag` is only meaningful on `fail_fast` stages. `fail_defer` stages never block the
pipeline, so there is nothing to escape from — skipping them would be stage *selection*,
which is a different feature this framework does not provide. The rule is enforced in code:
declaring a `skip_flag` on a non-`fail_fast` stage raises an error at CLI parser
construction, so the mistake fails on the developer's first run.

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

**Detection ownership.** `progress.py` becomes the single owner of environment detection for
the codebase. `lib/output.py` currently has its own two-way model (`is_ci()` =
`not sys.stdout.isatty()`), which emits GitHub Actions workflow commands (`::error::`,
`::warning::`) whenever output is merely piped — including local pipes and agent-invoked runs.
As part of step 1 of the rollout, `output.py` is re-pointed at the shared detection helper and
`is_ci()` comes to mean *actually running under GitHub Actions* (`$GITHUB_ACTIONS == "true"`).
Call sites of `emit_error` / `emit_warning` are unchanged; only the detection underneath them
is fixed.

**Container and agent runs.** Per the Audience section, no TTY exists inside
`vrg-container-run` and `$GITHUB_ACTIONS` is not propagated into the container, so
containerized runs auto-detect to `PlainRenderer`. This is the intended behavior, not a gap.

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

**Retention.** Feature-worktree `.vergil/` directories die with their worktree, but the main
checkout's `.vergil/` lives for the lifetime of the clone — and that is exactly where
`vrg-release` (and post-merge commands like `vrg-finalize-pr`) run. To keep that directory
from accumulating logs indefinitely, the framework prunes on start: when creating a new log,
it deletes the oldest logs **for that command** beyond the most recent N. Default N=20 — a
named constant, no configuration surface. Recent logs are deliberately kept as forensic
history ("what happened in last week's release?"); pruning only bounds the count.

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
| `--skip-<stage>` | — | Skip a `fail_fast` stage entirely (only available if the stage declares a `skip_flag`). |

---

## Scope and Rollout

### Commands in scope (long-running, human-invoked procedural)

- `vrg-release` — first adopter; reference implementation
- `vrg-validate` — multiple sequential lint/check stages. Rich rendering applies when a human
  runs it directly on a TTY; its containerized path (`vrg-container-run -- vrg-validate`,
  the dominant agent context) renders plain by design
- `vrg-finalize-pr`, `vrg-submit-pr` — human-run PR workflow commands; candidates identified
  during review
- Others as identified during implementation

### Commands out of scope (atomic / quick, or agent-oriented)

- `vrg-commit`, `vrg-gh`, `vrg-git`, `vrg-version`, `vrg-changelog` (standalone), and similar
  commands that run a single operation and return quickly
- `vrg-container-run` — a host tool, but typically agent-invoked; treated as an agent tool
  (see Audience and Non-goals)

### Rollout order

1. Implement `vergil_tooling/lib/progress.py` with all three renderers and the `Stage` / `run_pipeline` API
2. Migrate `vrg-release` as the reference implementation — validates the full framework end-to-end
3. Migrate `vrg-validate` — second adopter, confirms the framework generalizes cleanly
4. Remaining long-running commands adopted incrementally

---

## Open Questions

None. All design decisions are resolved.

---

## Revision History

- **2026-06-05** — Initial draft from brainstorming session.
- **2026-06-05** — Pushback review (issue #1419 branch). Added: Audience section (human-invoked
  scope; agent/container runs render plain by design), Relationship to existing machinery
  (#949 phase runner, `--verbose`, `--skip-cd-docs` removal, tracking-comment hooks, capture
  model), Subprocess Execution Contract, stage failure contract (failure = raise),
  interrupt handling (summary + exit 130), skip semantics clarified to don't-run
  (`fail_fast` only; run-but-tolerate declared a non-goal), detection ownership consolidated
  with `lib/output.py`, log retention (prune on start, keep last 20 per command).
- **2026-06-05** — Alignment review against the implementation plan
  (`docs/plans/2026-06-05-progress-framework.md`). Added: print-capture paragraph
  (stdout/stderr redirection during stages), `fail_defer` reports-not-raises wording fix,
  skip-flag rule enforced at parser construction, CI pollers retain transient retry while
  streaming.
