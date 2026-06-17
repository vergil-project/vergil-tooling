# Batch PR pipeline: submit → finalize → release in one pass

- **Issue:** [#1673](https://github.com/vergil-project/vergil-tooling/issues/1673)
- **Status:** Design (approved in brainstorming; pending implementation plan)
- **Date:** 2026-06-17

## Problem

We increasingly submit many small PRs in rapid succession. The single-PR
pipeline — `vrg-submit-pr` → `vrg-finalize-pr` → `vrg-release` (optionally
`--install`) — is smooth, but there is no way to push a *batch* through it.

When several PRs are open at once and finalized in parallel, they race: the
first to merge advances `develop`, which forces every other PR `BEHIND`. Each
must update its branch and re-run its CI gate; the next merge invalidates the
rest again. With N PRs this produces a cascade of redundant gate runs where
only one run per PR is ever load-bearing. On GitHub-hosted runners this is
merely wasteful; once CI moves to self-hosted Forgejo, runner capacity is
finite and the waste is no longer free.

Merging into a single shared branch is inherently serial — `develop` cannot be
updated in parallel — so the work is single-threaded regardless. Attempting to
parallelize only spends CI on runs that are immediately invalidated.

## Goal

Make the whole `submit → finalize → release → install` pipeline work on a
**batch** of PRs exactly as it works for one today, optimized so that each
expensive CI gate runs **exactly once**. If the work is effectively
single-threaded, embrace single-threading rather than parallelize into waste.

## Non-goals / out of scope

- **Merge queues.** Rejected after research (see below). They optimize
  throughput by *spending more* CI, and the migration target (Forgejo) has no
  merge queue.
- **Flipping the default to `--no-finalize`.** The human floated making finalize
  the default for single-PR runs. That changes long-standing single-PR muscle
  memory and deserves its own decision; this spec leaves the current default
  (no finalize unless `--finalize`/`--release`/`--install`) alone.
- **Cross-PR dependency detection / stacked-PR ordering.** Items are processed
  in selection order; a dependency is satisfied by ordering the dependency
  first (the rebase-on-`develop` step picks up its merged changes). Unresolvable
  overlap surfaces as a rebase or merge conflict, which stops the batch.

## Why not a merge queue

Research (2026-06-17), data separated from judgment:

**Data.**

- Forgejo has **no merge queue** — it is an open, unimplemented feature request
  ([forgejo#5102](https://codeberg.org/forgejo/forgejo/issues/5102), no
  milestone as of 2026-06-02). Forgejo today offers only auto-merge.
- Gitea (Forgejo's upstream) is the same: auto-merge only, no merge
  queue/train.
- GitHub's merge queue builds speculative merge commits (tests A against base,
  B against base+A, …) and **runs CI twice** — once on the PR branch, again on
  the `merge_group` branch — which by GitHub's own docs *increases* total CI
  minutes.

**Judgment.**

- A merge queue is a *throughput* optimization that spends more CI to merge
  faster. Our goal is the inverse: minimize expensive gate runs, accepting
  single-threading. It optimizes the wrong axis for us.
- Building on GitHub's merge queue would lock us to GitHub precisely as we
  migrate to Forgejo, against this repo's forge-agnostic design.
- A single-threaded batch orchestrator inside `vrg` tooling rides the same
  `gh`/forge abstraction we already use, so it works identically on GitHub and
  Forgejo and runs N PRs in exactly N gate runs.

## Architecture

Two entry points share one orchestrator.

- **Optimal path — `vrg-submit-pr` batch.** Selecting ≥2 ready worktrees runs
  the fully-lazy loop: rebase each branch on the latest `develop` → open its PR
  → gate once → merge, one at a time. Zero wasted CI by construction. This is
  the primary path.
- **Convenience path — `vrg-finalize-pr` batch.** Open PRs ad hoc, then hand
  `vrg-finalize-pr` a comma-separated list (or `--all` open PRs in
  `.worktrees/`) to serialize the merges. Because those PRs are already open,
  this path *does* incur the `BEHIND` → update → re-run CI cost — inherent to
  having opened them up front. It is the "drain these safely" tool, not the
  zero-waste one. The existing `pr_merge.wait_and_merge` already handles the
  `BEHIND` updates.

Both delegate to a new shared module **`lib/pr_workflow/batch.py`**: an ordered,
fail-fast serial loop taking a per-item step callback (submit-batch does
rebase + submit + merge; finalize-batch does merge-only), running
`vrg-release`/`--install` **once at the end**, and printing a state report.

**Single-PR invocations of both commands are unchanged.** One selected item is
the special case that flows through today's code. The `≥2` branch is the only
new path.

**Identity gate is unchanged.** Batch is human-only, exactly like `vrg-submit-pr`
today; agents remain blocked.

## Selection & invocation

### `vrg-submit-pr`

- **Interactive (TTY, no selection flags):** the existing single-select menu
  over *ready* worktrees becomes a **checkbox multi-select**. The
  ready / in-flight / not-ready classification (`_choose_submit_worktree`) is
  reused unchanged; only the picker widget changes.
- **Non-interactive:** `--all` selects every ready worktree; `--select <tokens>`
  takes a comma-separated list where each token matches a worktree by **issue
  number** or **directory name** (e.g. `--select 1673,1681` or
  `--select issue-1673-foo`). Unmatched or ambiguous tokens are a hard error
  that names them — no silent skips.
- **Branching:** 0 ready → today's error; 1 selected → today's single-PR path,
  untouched; ≥2 → batch orchestrator.

### `vrg-finalize-pr`

- The `pr` positional accepts a **comma-separated list** of PR numbers/URLs;
  `--all` finalizes every open PR found in `.worktrees/`.
- Interactive inference with >1 PR becomes a checkbox multi-select (today it is
  single-choice disambiguation).

### Cascade flags

`--finalize` / `--release` / `--install` / `--yes` / `--dry-run` keep their
current meaning and apply to the whole batch. `--release`/`--install` trigger
the single end-of-batch release (normalization stays install ⇒ release ⇒
finalize).

## The per-item pipeline (core mechanism)

For a **submit-batch**, each item runs this sequence in order:

1. **Rebase on latest `develop`** *(new — the efficiency win).*
   `git fetch origin develop`, then rebase the item's branch onto
   `origin/develop` inside its worktree. This guarantees the gate runs against
   the final state, so each expensive CI run is load-bearing and is never
   re-triggered by a later merge. A rebase conflict stops the batch.
2. **Submit.** Push `--force-with-lease`, create the PR, `record_submission`
   (reuses `_push_branch` / `_create_pr`).
3. **Finalize.** Provenance check → `wait_and_merge` (squash) → cleanup (switch
   to `develop`, fast-forward sync, delete merged branch + worktree, prune).
   Reuses the existing finalize stages.

**finalize-batch** is the same loop minus steps 1–2: merge-only per item
(relying on `wait_and_merge`'s `BEHIND` → update handling).

### Deferred validation

A single `vrg-finalize-pr` today runs container validation + the CD check after
its merge. Running that after every item would mean N container builds per
batch. Instead the batch runs **merge + cleanup per item, but container
validation and the CD check once, after the final merge.** Rationale: each PR
already passed its own gate before merging, so per-item local re-validation is
largely redundant; the single end-of-batch run is the integration check across
all merged changes.

### Release once

After all items merge and the end-of-batch validation passes, if
`--release`/`--install` was given, `vrg-release [--install]` runs **exactly
once** — one version bump shipping all N changes. It does not run if any item
failed.

## Interaction model (design invariant)

**One confirmation up front, then the batch runs to completion unattended.**
This is a governing invariant, not a convenience:

- The orchestrator shows the **full plan up front** (the ordered list of what it
  will rebase / submit / finalize, and whether it will release) and takes
  **one** confirmation. `--yes` skips it; `--dry-run` prints the plan and exits.
- Per-item steps run with their individual confirmations **pre-suppressed** (the
  orchestrator threads `assume_yes` through), so no "Submit this PR?" /
  "Finalize PR #N?" prompt fires mid-batch.
- A fail-fast stop is a **terminal halt that reports state** — it does *not*
  prompt for retry/skip/abort.
- The only two ways the batch ever pauses are the single up-front confirm and
  `--dry-run`. Everything else is hands-off.

## Error handling & resumability

**Fail-fast.** The batch stops at the first failure of any item: rebase
conflict, gate red (`wait_and_merge` `MergeAbortError`), merge conflict, or
provenance violation. No later items start; `vrg-release` does not run.

**State on stop is always clean and reportable.**

- Items already merged are fully done — branches and worktrees cleaned up.
- The failed item is left where it failed: a rebase failure means its PR was
  never opened; a gate failure means its PR is open and red.
- Unstarted items are untouched.

The summary names all three buckets — *merged*, *failed (with reason)*, *not
started* — so the stop point is unambiguous.

**Resumability falls out of the selection scan; no state file, no `--resume`.**
Re-running `vrg-submit-pr --all --finalize` after a fix:

- skips merged items (their worktrees no longer exist);
- skips the failed item *if its PR is already open* (the scan classifies it
  "in-flight" and excludes it — finish that one via pr-watch or a
  finalize-batch);
- re-includes the failed item if it never opened (rebase failure) once fixed;
- picks up the not-started items.

The deferred end-of-batch validation and release simply run at the end of
whatever the re-run completes.

## Components

1. **`vrg-submit-pr`** — multi-select checkbox UI, `--all` / `--select` flags;
   the `≥2` branch delegates to the orchestrator.
2. **`vrg-finalize-pr`** — comma-separated PR list + `--all`; the multi-item
   branch delegates to the orchestrator (merge-only per item).
3. **`lib/pr_workflow/batch.py`** *(new)* — the shared serial loop: ordered
   items, per-item step callback, fail-fast, single up-front confirm,
   release-once-at-end, deferred end-of-batch validation, state-report summary.
4. **`lib/worktrees.py`** — extend selection with a checkbox multi-select
   (`select_worktrees`) alongside the existing single `select_worktree`.
5. **Auto-rebase helper** *(new)* — fetch `origin/develop` + rebase a branch
   inside its worktree; used by submit-batch per item.

## Testing

- **Orchestrator loop (unit):** with a fake per-item step — success runs all
  items in order; first failure stops the rest; `vrg-release` is called exactly
  once on full success and never when any item failed; the up-front confirm
  fires once and no per-item prompt fires.
- **Selection (unit):** `--all` picks all ready; `--select` matching by issue
  number and by worktree name; unmatched token errors and names the token;
  0/1/≥2 branching (1 → single-PR path untouched).
- **Lazy rebase (unit):** clean rebase proceeds; rebase conflict raises and
  stops the batch with the correct report bucket.
- **Deferred validation (unit):** container-validation + CD-check invoked once
  after the final merge, not per item.
- **State report (unit):** merged / failed-with-reason / not-started buckets are
  accurate at the stop point.
- **Regression:** single-PR `vrg-submit-pr` and `vrg-finalize-pr` behavior is
  unchanged.
- **`--dry-run`:** prints the full ordered plan and makes no changes.

All tests run under the existing suite via `vrg-container-run -- vrg-validate`.
