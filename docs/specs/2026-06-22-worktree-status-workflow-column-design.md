# Design: `vrg-worktree-status` — WORKFLOW column for pr-workflow prep state

- **Issue:** [#1729](https://github.com/vergil-project/vergil-tooling/issues/1729)
- **Date:** 2026-06-22
- **Status:** Approved (brainstorm)

## Overview

`vrg-worktree-status` reports, per worktree, whether GitHub has a PR
(the `PR`/`STATE` columns) but not whether the **local PR handoff file**
(`.vergil/pr-workflow.json`) has been prepared for submission. When
several stalled worktrees all sit on `no-pr`, there is no way to tell
which ones an agent has actually prepared (via
`vrg-pr-workflow report-ready`) from which are still mid-implementation.

This change adds a single observability signal: a `WORKFLOW` column
showing each worktree's raw pr-workflow `status`, and a neutral
`N PR prepared.` count on the summary line. It lets a human scan the
table and pick which worktrees are ready to hand to `vrg-submit-pr`.

## Purpose

Surface the pr-workflow prep state that already gates `vrg-submit-pr`,
so the human running the submit step does not have to open each
`.vergil/pr-workflow.json` by hand to learn which worktrees are
submittable.

The "prepared" definition reuses the **same signal** `vrg-submit-pr`
already uses to build its ready set (`_ready_worktrees()` →
`pr_metadata` present), so what status *flags* as prepared and what
submit *accepts* stay consistent.

## Non-goals

- **No new behavior in `vrg-submit-pr`.** Batch submission of every
  prepared worktree already exists as `vrg-submit-pr --all` (issue
  #1673). This change does not add a `--select all` alias or otherwise
  touch submit-pr — one spelling is enough.
- **Observability-only.** The new column makes **no GitHub calls**; it
  reads the local file only. STATE/cruft classification, sort order,
  and the cruft summary are unchanged.
- No `--json` output, no non-zero exit based on prep state (YAGNI;
  consistent with the v1 status spec).

## Architecture

A thin extension of the existing `lib/worktrees.py` → `bin/
vrg_worktree_status.py` split. The library gathers the new signal and
exposes it on the status dataclass; the bin renders the column and
extends the summary. The pr-workflow file is read through the existing
loader so its schema stays single-sourced.

### `lib/worktrees.py` — gather the signal

- Extend the `WorktreeStatus` dataclass to carry the probe as **three
  distinct cases** (so the renderer can tell "no file" from "read
  error" without a magic string):
  - **Absent** — no `.vergil/pr-workflow.json`. Renders `-`.
  - **Loaded** — carries `workflow_status: str` (the raw `status`) and
    `pr_prepared: bool` (`pr_metadata` populated → the `vrg-submit-pr`
    gate).
  - **Error** — carries a `reason: str`. Renders `unknown` + a note.
  Concretely this can be a small tagged field (e.g. `workflow_status:
  str | None` for Absent/Loaded plus a `workflow_error: str | None`
  carrying the reason for the Error case, with `pr_prepared` always
  `False` outside the Loaded case). The exact encoding is a plan-level
  detail; the three cases and their renderings are the contract.
- In `gather_worktree_status(worktree, *, target)`, read the
  worktree's local pr-workflow file (each `Worktree` already carries an
  absolute `path`) via the existing `lib/pr_workflow/` loader
  (`WorkflowState` / `LocalFileTransport`). Do **not** re-parse the JSON
  by hand — reuse the loader so the schema definition is not
  duplicated.
- A genuinely absent file is the **normal** case: `workflow_status =
  None`, `pr_prepared = False`. No error.

### `bin/vrg_worktree_status.py` — render

- Add `WORKFLOW` to `_COLUMNS`, positioned **between `STATE` and
  `AHEAD`**. The dynamic column-width logic already in `_render_table`
  needs no change.
- Cell value, by probe case:
  - **Loaded** → the `workflow_status` string, rendered **verbatim**
    (e.g. `approved`, `reviewing`, `implementing`, `changes-requested`,
    `escalated`, `error`).
  - **Absent** → `-`, matching the dash convention `PR` and `DIRTY`
    already use for "nothing here".
  - **Error** → `unknown` (see Error handling below).
- Summary line: append a neutral, trailing sentence
  `N PR prepared.` where `N = count(status.pr_prepared)`. It keys off
  `pr_prepared` (`pr_metadata` present), **not** the status string —
  so a worktree in `reviewing`, `approved`, or `changes-requested`
  with metadata already written all count. Existing
  `active/stalled/cruft` counts and the "Run vrg-finalize-pr to clean
  cruft." line are untouched.

## Data flow

```
list_worktrees(repo_root)
  → for each Worktree:
      gather_worktree_status(wt, target=…)
        ├─ ahead / dirty / PR resolution   (unchanged)
        └─ read wt/.vergil/pr-workflow.json via pr_workflow loader   (NEW)
              ├─ no file      → workflow_status=None,  pr_prepared=False
              ├─ loaded ok    → workflow_status=state.status,
              │                 pr_prepared=(state.pr_metadata is not None)
              └─ read/parse error → workflow_status=<unknown sentinel>,
                                    note appended, pr_prepared=False
  → sort (unchanged) → _render_table (+WORKFLOW col) → summary (+prepared count)
```

## Error handling (no silent failures)

If `.vergil/pr-workflow.json` exists but is unreadable, malformed, or
fails to load, the column renders `unknown` and a **per-worktree note**
is emitted stating the worktree and the reason — mirroring how the
existing `UNKNOWN` PR-lookup state surfaces a reason in the notes
section. We never render `-` (which means "no file") over a real read
error, and we never swallow the exception. A missing file is the only
case that maps to `-`.

`pr_prepared` is `False` on any error path, so an unreadable file is
never counted as prepared.

## Testing

Extend `tests/vergil_tooling/test_vrg_worktree_status.py` (and
`tests/.../test_worktrees.py` for the gather-level signal):

1. **Status verbatim** — a worktree whose pr-workflow `status` is
   `approved` renders `approved` in the `WORKFLOW` column.
2. **No file → `-`** — a worktree with no `.vergil/pr-workflow.json`
   renders `-`.
3. **Read/parse error → `unknown` + note** — a malformed file renders
   `unknown` and emits a per-worktree note; the worktree is not counted
   as prepared.
4. **Prepared count** — the summary `N PR prepared.` reflects the
   number of worktrees with `pr_metadata` present (and excludes
   no-file and error worktrees).
5. **Column alignment** — mixed values (long `changes-requested`,
   short `-`) stay aligned under the dynamic-width renderer.

Tests mock the pr-workflow loader / status gathering the same way the
existing suite mocks `list_worktrees` and `gather_worktree_status`, so
no real `git`/`gh`/filesystem pr-workflow files are required.

## Out of scope / future

- A `--ready`/filter flag to show only prepared worktrees (YAGNI; the
  column already makes them scannable).
- Any change to `vrg-submit-pr` selectors.
