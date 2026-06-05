# vrg-finalize-pr

**Installed as:** `vrg-finalize-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_finalize_pr.py`

Finalizes a pull request and reconciles local state afterwards. Has
two modes, keyed on whether a PR argument is given:

- **`vrg-finalize-pr <PR>`** — runs the pre-merge provenance check,
  merges the PR (or confirms it is already merged), then runs the
  cleanup below. This replaces a manual web merge followed by a
  separate cleanup step.
- **`vrg-finalize-pr`** (no PR) — cleanup only: switch to the target
  branch, fast-forward pull, delete merged local branches, and prune
  stale remote-tracking references. This is the release path.

Must be run from the **main worktree** — the cleanup removes
worktrees, which is unsafe when the calling shell's CWD is inside one.

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

### 1. Provenance Check and Merge (PR mode only)

Runs the pre-merge provenance check. Advisories are printed but do not
block. Violations abort the merge unless `--allow-provenance-violation`
is given. If the PR is already merged, the merge step is skipped.

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
| 0 | Finalization complete |
| 1 | Provenance violation, not run from main worktree, dirty working tree, failed validation, or failed CD run |
