# Design: worktree hygiene — status command + squash-merge-aware finalize sweep

- **Issue:** [#1552](https://github.com/vergil-project/vergil-tooling/issues/1552)
- **Date:** 2026-06-09
- **Status:** Approved (brainstorm)

## Overview

Two coupled deliverables addressing worktree hygiene:

1. **`vrg-worktree-status`** — a read-only command that lists every
   canonical `.worktrees/` worktree with its derived lifecycle state,
   so removable cruft is visible at a glance.
2. **Squash-merge-aware `vrg-finalize-pr` straggler sweep** — fixes the
   structural bug that orphaned the cruft in the first place: the
   sweep's candidate set comes from `git branch --merged`, which is
   blind to squash-merged branches.

They share one source of truth — `classify_worktree` — so what status
*flags* as cruft and what finalize *removes* are computed by the same
pure function.

## Purpose

Provide a read-only command that lists every canonical `.worktrees/`
worktree with its derived lifecycle state, so removable cruft (merged
PRs whose local worktree was never cleaned up) is obvious at a glance
and legitimate in-flight work is clearly distinguished.

This reproduces, on demand, the manual triage that surfaced the need:
of 10 stale worktrees, 7 were orphaned merged PRs and 3 were live
work. Several active worktrees at once is normal and fine — the goal
is to make the *cruft* among them visible, not to discourage parallel
work.

## Non-goals

- **`vrg-worktree-status` is not a cleanup tool.** Status observes;
  `vrg-finalize-pr` acts. There is no `--prune`/`--clean` on the status
  command — worktree removal stays in one home (the finalize sweep,
  fixed below).
- No `--json`, no non-zero exit on cruft, no offline/`--no-remote`
  fast path in v1. Each is easy to add later if a concrete need
  appears (YAGNI).

## Architecture

A thin CLI over the existing `lib/worktrees.py` helpers. Classification
is a **pure function** in the lib so it is unit-testable without
spawning `git`/`gh`; the bin gathers signals, calls the classifier,
and renders.

### `lib/worktrees.py` — classification (pure)

Add:

- A `WorktreeState` enum: `OPEN_PR`, `NO_PR`, `DRAFT`, `MERGED`,
  `CLOSED`, `UNKNOWN`.
- A `WorktreeStatus` dataclass: the `Worktree`, the resolved
  `WorktreeState`, `pr_number: int | None`, `ahead: int`,
  `dirty: bool`, and `detail: str | None` (reason for `UNKNOWN`).
- `classify_worktree(worktree, *, pr, pr_lookup_failed, ahead, dirty,
  detail=None) -> WorktreeStatus` — pure: takes already-gathered
  signals, returns the verdict. No I/O.

`pr` carries the resolved PR info (number + state) or `None`.
`pr_lookup_failed` is distinct from `pr is None`: a failed lookup must
never be read as "no PR."

### `bin/vrg_worktree_status.py` — signal gathering + render

- Enumerate canonical worktrees via `worktrees.list_worktrees(root)`.
- Per worktree, gather:
  - **local, instant:** commits ahead of `develop`
    (`git rev-list --count develop..<branch>`), dirty flag
    (`git -C <wt> status --porcelain`).
  - **remote, authoritative:** PR state via the existing
    `github` helpers (`pr_for_branch` for an open PR;
    `closed_pr_for_branch` + state for merged/closed). One `gh`
    call per worktree.
- Call `classify_worktree(...)` for each.
- Render a table sorted so live work is listed first and cruft groups
  at the bottom, followed by a one-line summary.
- New console script entry: `vrg-worktree-status`.

The command is runnable from anywhere in the repo (it is read-only and
needs no main-worktree guard, unlike `vrg-finalize-pr`).

## Lifecycle states

| State | Verdict | Signal |
|---|---|---|
| `open-pr` | keep — in review | open PR for the branch |
| `no-pr` | keep — stalled before handoff | no PR + commits ahead of develop |
| `draft` | keep — just created | no PR + 0 commits ahead |
| `merged` | **cruft — removable** | PR merged |
| `closed` | **cruft — removable** (abandoned) | PR closed, not merged |
| `dirty` | **never removable** (overlay) | uncommitted changes in the tree |
| `unknown` | attention | detached worktree, or a `gh` lookup failed |

### Classification precedence

1. If the PR lookup **failed**, the state is `unknown` with the error
   in `detail`. (No silent downgrade to `no-pr`.)
2. Otherwise resolve by PR: open → `open-pr`; merged → `merged`;
   closed-not-merged → `closed`.
3. No PR: `no-pr` if `ahead > 0`, else `draft`.
4. `dirty` is an **overlay**, not a separate row state: it is reported
   alongside whatever the lifecycle state is, and forces the
   removability verdict to "keep" even for `merged`/`closed` (there is
   uncommitted work to rescue).

## Error handling

No silent failures. A failed per-worktree `gh` lookup yields
`unknown` with the captured reason rather than a misleading `no-pr`.
Enumeration or `git` failures surface as errors, not empty output.

## Output

Plain table to stdout. Example (illustrative):

```
WORKTREE                          BRANCH                            PR     STATE     AHEAD  DIRTY
issue-1534-pr-workflow-oracle     feature/1534-pr-workflow-oracle   #1544  open-pr   2      -
issue-1543-vrg-mechanization-spec feature/1543-vrg-mechanization-…  -      no-pr     1      -
issue-1470-finalize-tty-stream    feature/1470-finalize-tty-stream  #1471  merged    2      -
…

10 worktrees — 2 active, 1 stalled (no-pr), 7 cruft (removable). Run vrg-finalize-pr to clean cruft.
```

Always exits 0.

## Fix: squash-merge-aware `vrg-finalize-pr` straggler sweep

### The bug

`_stage_cleanup` in `bin/vrg_finalize_pr.py` enumerates straggler
candidates with `git.merged_branches(target)` — i.e.
`git branch --merged develop`. A squash merge rewrites the branch's
work onto the target as a single new commit, so the feature-branch tip
is never an ancestor of the target and `git branch --merged` never
lists it. Squash is the default feature-PR strategy, so the sweep is
blind to the common case. The only branch ever cleaned is the explicit
just-merged PR branch (`ctx.merged_branch`), which is set only by the
`merge` stage — so PRs merged outside `vrg-finalize-pr <PR>` (web/`gh`
merge, or the release `--cleanup-only` path) leave their worktree and
branch orphaned, and no later finalize run sweeps them up.

The bug is the **candidate set**, not the guard.
`github.closed_pr_for_branch` already returns merged *or* closed PRs
and is exactly the right merge-evidence test — it just never sees
squash-merged branches.

### The change

Widen the candidate set to the **union** of:

1. ancestry-merged branches (`git branch --merged` — existing; catches
   merge-commit/rebase merges and branches whose worktree is already
   gone), and
2. branches checked out in canonical `.worktrees/` worktrees
   (`worktrees.list_worktrees` — new; catches squash-merged worktree
   branches),

deduped. Every candidate passes through the **existing, unchanged
guards**:

- **eternal-branch guard** — never touch `develop`/`main`/etc.
- **zero-commit guard** — tip == target → skip (in-flight/just-created
  branch with no work).
- **PR-merge-evidence guard** — `closed_pr_for_branch` is None → skip
  (no merged/closed PR yet: open-PR or no-PR branches).
- **dirty guard** (in `_delete_branch_and_worktree`) — uncommitted
  changes → skip.

This only *widens what is considered*, never *loosens what is
deleted*: the guards that decide removal are untouched, including the
parallel-agent race guards from #1445.

### Shared removability decision

The worktree-branch arm of the sweep consumes `classify_worktree`
(the same pure function backing `vrg-worktree-status`) to decide
removability: a worktree is swept only when its state is `merged` or
`closed` and it is not dirty. This guarantees `vrg-worktree-status`'s
"cruft" verdict and `vrg-finalize-pr`'s removal set are identical by
construction. The non-worktree ancestry arm keeps its current
guard-based path (there is no worktree to classify).

### Safety check against today's state

Applied to the 10 current worktrees, the guards keep all live work:
#1534 and #1547 (open PRs → no closed/merged PR → skipped), #1543 (no
PR → skipped). Only the 7 with merged PRs, commits ahead, and clean
trees are swept. The 7 stragglers are intentionally being left in
place as a live integration test: after this fix ships, the next
`vrg-finalize-pr` run should clean exactly those 7 and nothing else.

## Testing

- **Unit** (`classify_worktree`): one case per state, the
  `dirty` overlay on a `merged` worktree, and the
  `pr_lookup_failed` → `unknown` path. These need no git/gh.
- **Unit** (sweep candidate set): the union/dedup of ancestry-merged
  and worktree branches, and that each guard (eternal, zero-commit,
  no-merge-evidence, dirty) excludes the right candidates — using
  fakes for the git/gh boundaries.
- **Integration** (best effort): drive `vrg-worktree-status` against a
  temporary repo with synthetic worktrees to cover signal gathering
  and rendering; otherwise covered manually. The end-to-end finalize
  behavior is covered by the live 7-straggler integration test
  described above.
