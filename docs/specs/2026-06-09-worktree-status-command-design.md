# Design: `vrg-worktree-status`

- **Issue:** [#1552](https://github.com/vergil-project/vergil-tooling/issues/1552)
- **Date:** 2026-06-09
- **Status:** Approved (brainstorm)

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

- **Not a cleanup tool.** Status observes; `vrg-finalize-pr` acts.
  There is no `--prune`/`--clean`. This keeps a single home for
  worktree removal (and for the squash-merge-aware logic being fixed
  there separately).
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

## Testing

- **Unit** (`classify_worktree`): one case per state, the
  `dirty` overlay on a `merged` worktree, and the
  `pr_lookup_failed` → `unknown` path. These need no git/gh.
- **Integration** (best effort): drive the bin against a temporary
  repo with a couple of synthetic worktrees to cover signal gathering
  and rendering; otherwise covered manually.
