# vrg-finalize-pr

**Installed as:** `vrg-finalize-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_finalize_pr.py`

Finalizes a pull request and reconciles local state afterwards. Has
two modes, keyed on whether a PR argument is given:

- **`vrg-finalize-pr <PR>`** — runs the pre-merge provenance check,
  waits for checks to go green, merges the PR (or confirms it is
  already merged), then runs the cleanup below. This replaces a
  manual web merge followed by a separate cleanup step.
- **`vrg-finalize-pr`** (no PR) — infers the PR from the
  `.worktrees/` worktrees and always confirms before acting (see
  *Choosing the PR* below). With no candidates, cleanup-only after
  confirmation: switch to the target branch, fast-forward pull,
  delete merged local branches, and prune stale remote-tracking
  references.

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
  fast when stdin is not a TTY.

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
vrg-finalize-pr [PR] [--target-branch BRANCH] [--strategy {merge,squash,rebase}]
                [--allow-provenance-violation] [--dry-run]
```

## Arguments

| Argument | Description |
| -------- | ----------- |
| `PR` | PR number or URL to merge and finalize. Omit for cleanup-only (release path). |
| `--target-branch` | Branch to switch to (default: `develop`) |
| `--strategy` | Merge strategy when a PR is given (default: `squash`) |
| `--allow-provenance-violation` | Proceed despite provenance violations (conscious human override) |
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
sweep below cannot see it.

### 2. Switch to Target Branch

Checks out the target branch (default: `develop`).

### 3. Fast-Forward Pull

Fetches (`--tags --force`) and fast-forward merges `origin/{target}`.

### 4. Delete Merged Branches and Worktrees

Deletes local branches merged into the target branch. If a merged
branch is still checked out in a canonical `.worktrees/` worktree, that
worktree is removed first (skipped if it has uncommitted changes).
Cached container images for the branch are cleaned. Eternal branches
are protected based on the `branching_model`:

| Branching Model | Protected Branches |
| --------------- | ------------------ |
| `docs-single-branch` | `develop`, `gh-pages` |
| `library-release` | `develop`, `main`, `gh-pages` |
| `application-promotion` | `develop`, `release`, `main`, `gh-pages` |

### 5. Prune Remote References

Runs `git remote prune origin` to clean up stale remote-tracking
branches.

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

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Finalization complete, or declined at a confirmation prompt |
| 1 | Provenance violation, unmergeable PR (draft, conflicts, failed checks, stuck behind), not run from main worktree, non-interactive stdin in inference mode, dirty working tree, failed validation, or failed CD run |
