# vrg-release: structured phase logging, --verbose flag, and subprocess capture fix

**Issue:** #949
**Date:** 2026-05-21

## Overview

Three coordinated changes to `vrg-release` and the shared subprocess
wrappers:

1. **Subprocess capture fix** — `git.run()` and `github.run()` gain
   output capture so that `CalledProcessError` carries stdout/stderr.
   Output is still printed by default; no caller behavior changes.

2. **Structured phase logging** — the release orchestrator prints
   entry/exit markers around each phase, always active regardless
   of verbosity.

3. **`--verbose` flag** — controls whether noisy subprocess output
   (CI polling, workflow watching) is displayed in full or
   summarized to one-line status updates. Implemented via bespoke
   wrappers inside the release modules, not by modifying the
   shared library contract.

## 1. Subprocess capture fix (generic)

### Problem

`git.run()` and `github.run()` call `subprocess.run()` with
`check=True` but without `capture_output`. When a command fails,
`CalledProcessError` has empty stdout/stderr — the error context
flowed to the terminal and is lost to the application.

This affects 21 `git.run()` call sites and 13 `github.run()` call
sites. Four additional direct `subprocess.run()` calls have the
same problem:

- `release/prepare.py` — two `git-cliff` calls
- `release/preflight.py` — `uv lock`
- `lib/version.py` — lockfile maintenance commands

### Design

**`git.run()`** (`lib/git.py`):

- Add `capture_output=True, text=True` to the `subprocess.run()` call.
- After a successful return, print `result.stdout` to stdout and
  `result.stderr` to stderr (preserving current visible behavior).
- On failure, `CalledProcessError` now carries stdout/stderr. No
  other change — `check=True` still raises as before.

**`github.run()`** (`lib/github.py`):

- Same treatment: add `capture_output=True, text=True` to the
  `_run_with_retry()` call inside `run()`.
- Print stdout/stderr after successful return.
- The retry logic in `_run_with_retry` can now inspect captured
  output for retry-relevant signals (e.g., rate-limit headers in
  stderr).

**4 direct subprocess calls:**

- `release/prepare.py:67,79` — `git-cliff` invocations: add
  `capture_output=True, text=True`, print output on success.
- `release/preflight.py:296` — `uv lock`: same.
- `lib/version.py:179` — lockfile maintenance: same.

### Behavioral guarantee

From the caller's perspective, nothing changes. Output appears on
the terminal at the same time it always did (modulo negligible
buffering). The only difference is that error objects now carry
the output.

## 2. Structured phase logging (orchestrator)

### Design

The orchestrator (`lib/release/orchestrator.py`) wraps each phase
call with entry/exit markers. Always active, independent of
`--verbose`.

**Entry marker:**

```
=== Phase: prepare ===
```

**Exit marker (success):**

```
=== prepare: done (12s) ===
```

**Exit marker (failure):**

```
=== prepare: FAILED (8s) ===
```

Implementation: `time.monotonic()` before/after each `phase_fn(ctx)`
call. The markers are printed by the orchestrator loop, not by the
phase functions themselves.

The existing `print()` calls within each phase module continue
unchanged — they appear as content between the phase markers.

Preflight is treated as a phase too. The orchestrator (or `main()`)
wraps the `preflight()` call with the same markers.

## 3. --verbose flag and bespoke release wrappers

### CLI argument

Add `--verbose` / `-v` to `vrg_release.py`'s `parse_args()`:

```python
parser.add_argument(
    "-v", "--verbose",
    action="store_true",
    default=False,
    help="Show full subprocess output (default: summarized).",
)
```

### Threading

Add `verbose: bool = False` to `ReleaseContext`. Set from
`args.verbose` in `main()`.

### Bespoke wrappers

Two noisy operations need verbose-aware handling:

1. **CI check polling** — `gh pr checks <url> --watch --fail-fast`
   (called via `github.wait_for_checks()` in `merge.py`).
2. **Workflow watching** — `gh run watch --exit-status <run_id>`
   (called via `github.run()` in `confirm.py`).

For each, create a release-specific wrapper that:

- Calls `subprocess.run()` directly with `capture_output=True`.
- In **verbose mode** (`ctx.verbose`): prints the full captured
  stdout/stderr.
- In **non-verbose mode**: prints a one-line status summary.
  For CI polling: `Waiting for checks on <url>... done.` or
  `Waiting for checks on <url>... FAILED.`
  For workflow watching: `Watching <workflow>... done.` or
  `Watching <workflow>... FAILED.`

These wrappers live in a new module `lib/release/subprocess.py`.
They bypass `github.run()` / `github.wait_for_checks()` for
these specific commands because they need verbose-awareness that
the shared library does not provide.

The pre-registration polling loop in `github.wait_for_checks()`
(the `while not _checks_registered(pr)` loop) is quiet already
— it just sleeps. The noisy part is the final
`gh pr checks --watch` call. The bespoke wrapper replaces only
that final call.

### What changes per module

- **`merge.py`** — `wait_and_merge()` gains a `verbose` parameter.
  Replaces `github.wait_for_checks(pr_url)` with the bespoke
  CI-polling wrapper. The merge call itself (`github.merge()`)
  stays as-is.
- **`confirm.py`** — `_watch_workflow()` replaces the
  `github.run("run", "watch", ...)` call with the bespoke
  workflow-watching wrapper.
- **`orchestrator.py`** — `merge_release()` passes `ctx.verbose`
  to `wait_and_merge()`.

### Default behavior

Without `--verbose`, the release workflow is quieter during CI
polling and workflow watching. All other output (phase markers,
phase-internal `print()` calls, error details) remains unchanged.

With `--verbose`, behavior matches what users see today — full
subprocess output streams to terminal.

## Scope boundaries

- The generic verbosity-control problem (letting callers of
  `git.run()` / `github.run()` control output level) is out of
  scope. That requires reworking the caller contract across 34+
  call sites.
- The capture fix applies to the shared wrappers. The verbose
  flag applies only to `vrg-release`. These two changes are
  decoupled.

## Testing

- **Capture fix**: existing tests for `git.run()` and
  `github.run()` need updating to expect `capture_output=True`
  in subprocess mock calls. Tests should verify that
  `CalledProcessError` carries stdout/stderr.
- **Phase logging**: test that the orchestrator prints entry/exit
  markers with timing. Mock `time.monotonic()` for deterministic
  output.
- **Verbose flag**: test both modes for the bespoke wrappers.
  Verbose prints full output; non-verbose prints summary.
  Test that `ReleaseContext.verbose` defaults to `False`.
- **CLI**: test that `parse_args(["-v"])` sets `verbose=True`.
