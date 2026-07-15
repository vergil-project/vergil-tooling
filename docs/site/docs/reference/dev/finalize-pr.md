# vrg-finalize-pr

**Installed as:** `vrg-finalize-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_finalize_pr.py`

Finalizes a pull request and reconciles local state afterwards. Has
three modes:

- **`vrg-finalize-pr <PR>`** — runs the pre-merge provenance check,
  waits for checks to go green, merges the PR (or confirms it is
  already merged), then runs the cleanup below. This replaces a
  manual web merge followed by a separate cleanup step.
- **`vrg-finalize-pr`** (no PR) — interactive: infers the PR from the
  `.worktrees/` worktrees and always confirms before acting (see
  *Choosing the PR* below). With no candidates, cleanup-only after
  confirmation: switch to the target branch, fast-forward pull,
  delete merged local branches, and prune stale remote-tracking
  references.
- **`vrg-finalize-pr --cleanup-only`** — non-interactive release path:
  skips inference and merge entirely, never reads stdin, and runs only
  the cleanup. This is what `vrg-release` invokes during its
  close-finalize phase (issue #1448). Mutually exclusive with a PR
  argument.

Must be run from the **main worktree** — the cleanup removes
worktrees, which is unsafe when the calling shell's CWD is inside one.

## Choosing the PR

- `vrg-finalize-pr <pr-url-or-number>` — no prompts; the explicit
  argument is the confirmation. This is the scriptable path
  (`vrg-submit-pr` prints the URL to pass here).
- `vrg-finalize-pr` (no arguments) — infers candidates by mapping each
  `.worktrees/` worktree's branch to its open PR, and **always
  confirms before acting**: one candidate asks `Finalize PR #N?`;
  several present a menu; none asks before running cleanup-only.
  Worktrees without an open PR are listed with the reason they were
  skipped. Inference mode requires an interactive terminal and fails
  fast unless both stdin and stdout are TTYs — a captured stdout would
  make the prompts invisible (issue #1448).
- `vrg-finalize-pr --cleanup-only` — no prompts and no merge; the
  scriptable path for callers (like `vrg-release`) that only want the
  cleanup.

## Waiting for green

When the PR's checks are not finished, `vrg-finalize-pr` waits for
them and merges automatically once everything is green and current.
Doomed outcomes abort immediately rather than after the wait: a draft
PR, merge conflicts, a failed check (named in the error), or a branch
still behind after five update attempts. A branch that is merely
behind the target is updated automatically and the wait restarts.

After the merge, the PR's own branch and worktree are cleaned up
explicitly (a squash merge hides them from `git branch --merged`),
followed by the usual sweep, pull, and prune.

## Usage

```bash
vrg-finalize-pr [PR | --cleanup-only] [--target-branch BRANCH]
                [--strategy {merge,squash,rebase}]
                [--allow-provenance-violation] [--clean-dirty] [--dry-run]
```

## Arguments

| Argument | Description |
| -------- | ----------- |
| `PR` | PR number or URL to merge and finalize. Omit to infer interactively. |
| `--cleanup-only` | Skip PR inference and merge; run cleanup without prompting or reading stdin (non-interactive release path). Mutually exclusive with `PR`. |
| `--target-branch` | Branch to switch to (default: `develop`) |
| `--strategy` | Merge strategy when a PR is given (default: `squash`) |
| `--allow-provenance-violation` | Proceed despite provenance violations (conscious human override) |
| `--clean-dirty` | Opt-in: clear a merged/closed worktree whose only dirt is untracked build/validation output, after showing what will be deleted and confirming. Never touches modified tracked files or a reused-branch straggler with unmerged commits (issue #2348). |
| `--dry-run` | Show what would be done without making changes |

## Behavior

### 1. Provenance Check, Wait, and Merge (PR mode only)

Runs the pre-merge provenance check first, so violations surface
before any waiting. Advisories are printed but do not block.
Violations abort the merge unless `--allow-provenance-violation`
is given. If the PR is already merged, the merge step is skipped and
finalize proceeds straight to cleanup. Otherwise the wait-for-green
loop runs (see *Waiting for green*) and the PR is merged with the
selected strategy. `--dry-run` skips the wait and prints what it
would do.

### 1a. Explicit-Target Cleanup (PR mode only)

The just-merged PR branch and its `.worktrees/` worktree are deleted
by name. The default squash strategy rewrites history onto the
target, so the branch is never an ancestor and the merged-branch
sweep below cannot see it. The branch's PR-workflow relay ref
(`refs/vergil/pr-workflow/<branch>`, pushed by `report-ready`) is
deleted alongside it, so a cloud-handoff ref never outlives its
branch (issue #2369). Deleting a relay ref that was never pushed is a
no-op.

### 2. Switch to Target Branch

Checks out the target branch (default: `develop`).

### 3. Fast-Forward Pull

Fetches (`--tags --force`) and fast-forward merges `origin/{target}`.

### 4. Delete Merged Branches and Worktrees

Deletes local branches merged into the target branch. If a merged
branch is still checked out in a canonical `.worktrees/` worktree, that
worktree is removed first (skipped if it has uncommitted changes).
Cached container images for the branch are cleaned.

Two guards keep this sweep from racing parallel agent sessions in
their creation-to-first-commit window (issue #1445), since
`git branch --merged` classifies a branch just created from the
target's tip as merged:

- **Zero-commit branches are skipped.** A branch whose tip equals the
  target's tip carries no merged work, so deleting it saves nothing —
  and it is exactly what an in-flight issue branch looks like before
  its first commit.
- **Merge evidence is required.** A branch is only swept when a closed
  or merged PR exists for its head; ancestry alone cannot distinguish
  a merged branch from one created off an older target tip. Branches
  skipped for lack of evidence are listed with the reason and left for
  manual cleanup.

Both guards gate the worktree removal as strictly as the branch
deletion. The explicit-target cleanup (step 1a) is unaffected — the
just-merged PR branch has merge evidence by construction.

#### Stuck merged worktrees are surfaced, not buried

A merged/closed worktree the sweep cannot remove — its tree is dirty,
or its branch name was reused after a same-named PR merged
(issue #1719) — is neither live work nor removable cruft. Rather than leaving
the skip reason inside the collapsed progress-stage log (where a clean
`✓ cleanup` summary hides it), finalize reports every such worktree
**prominently after the pipeline**, with the reason, so finishing with
stuck worktrees is impossible to miss (issue #2348).

#### `--clean-dirty`: guarded clear of untracked-only dirt

The common Mac case is a merged worktree whose only "dirt" is
un-gitignored build or validation output. `--clean-dirty` offers an
opt-in path to clear exactly that, after the pipeline, on real stdio:

- Only a **merged/closed** worktree whose **every** uncommitted path is
  **untracked** is a candidate. A reused-branch straggler classifies as
  `no-pr`/`draft` (its merged verdict withheld, issue #1719), so it is
  never in a clearable state and its unmerged commits are never at risk.
- A worktree with **modified tracked files** is refused — those are
  never discarded — and left surfaced as needing attention.
- Each removal **shows exactly what will be deleted** (the untracked
  paths) and **requires confirmation** before `git worktree remove
  --force` discards the output and deletes the branch.

The default guards are unchanged: without `--clean-dirty`, anything with
tracked modifications or unmerged commits is protected, as before.

> **Tip:** the cleaner fix is to gitignore your validation output so the
> worktree is never dirtied in the first place — then it sweeps
> automatically with no `--clean-dirty` needed. That is repo-specific
> configuration, out of scope for finalize itself.

Eternal branches are protected based on the `branching_model`:

| Branching Model | Protected Branches |
| --------------- | ------------------ |
| `docs-single-branch` | `develop`, `gh-pages` |
| `library-release` | `develop`, `main`, `gh-pages` |
| `application-promotion` | `develop`, `release`, `main`, `gh-pages` |

### 5. Prune Remote References

Runs `git remote prune origin` to clean up stale remote-tracking
branches. As a swept safety net, any PR-workflow relay ref
(`refs/vergil/pr-workflow/*`) whose branch no longer exists on origin
is pruned too, so a relay ref stranded by a branch deleted outside
finalize (or by an earlier failed run) is never left behind
(issue #2369).

### 6. Working-Tree Cleanliness Gate

Fails if the target branch's working tree is not clean, so leftover
files are cleaned up before the next issue.

### 7. Post-Finalization Validation

Runs canonical validation via `vrg-container-run` to catch problems on
the target branch before the next PR is created.

### 8. CD Workflow Check

Inspects the most recent CD workflow run on the target branch and fails
if it did not succeed. Docs publishing is async and used to fail
silently (issue #303).

## Batch mode (comma-list / `--all`)

A comma-separated PR argument (`vrg-finalize-pr 123,124,125`) or `--all`
(every open PR in `.worktrees/`) finalizes several PRs as a single
serialized batch (issue #1673). Each item runs
`vrg-finalize-pr <pr> --skip-post-checks` (merge + cleanup, deferring
the post-merge checks); after every item merges, one end-of-batch
`vrg-finalize-pr --cleanup-only` runs validation and the CD check, then
— with `--release`/`--install` — a single `vrg-release`.

Unlike `vrg-submit-pr`'s batch, these PRs are already open, so each one
that lands behind another will `update-branch` and re-run its gate
(the normal serialized-merge cost). For zero-waste CI, submit the batch
with `vrg-submit-pr --all --finalize` instead, which rebases each branch
before opening its PR.

The batch asks **one** confirmation up front (skipped with `--yes`) and
is **fail-fast**: the first failure stops it and prints a
`merged` / `failed` / `not started` summary. The single-PR modes are
unchanged.

The `--skip-post-checks` flag (used internally by the batch) finalizes a
PR but skips the validation and CD-check stages and never chains a
release — the batch runs those once at the end.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Finalization complete, or declined at a confirmation prompt |
| 1 | Provenance violation, unmergeable PR (draft, conflicts, failed checks, stuck behind), not run from main worktree, non-TTY stdin or stdout in inference mode, dirty working tree, failed validation, failed CD run, or a batch item failed |
