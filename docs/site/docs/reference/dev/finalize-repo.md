# vrg-finalize-repo

**Installed as:** `vrg-finalize-repo` (Python console script)

**Source:** `src/vergil_tooling/finalize_repo.py`

Cleans up a repository after a PR merge: switches to the target
branch, fast-forward pulls, deletes merged local branches, and
prunes remote tracking references.

## Usage

```bash
vrg-finalize-repo [--target-branch BRANCH] [--dry-run]
```

## Arguments

| Argument | Description |
| -------- | ----------- |
| `--target-branch` | Branch to switch to (default: `develop`) |
| `--dry-run` | Show what would be done without making changes |

## Behavior

### 1. Switch to Target Branch

Checks out the target branch (default: `develop`).

### 2. Fast-Forward Pull

Fetches and fast-forward merges `origin/{target}`.

### 3. Delete Merged Branches

Deletes local branches that have been merged into the target branch.
Eternal branches are protected from deletion based on the
`branching_model`:

| Branching Model | Protected Branches |
| --------------- | ------------------ |
| `docs-single-branch` | `develop`, `gh-pages` |
| `library-release` | `develop`, `main`, `gh-pages` |
| `application-promotion` | `develop`, `release`, `main`, `gh-pages` |

### 4. Prune Remote References

Runs `git remote prune origin` to clean up stale remote-tracking
branches.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Finalization complete |
| 1 | Unrecognized branching model |
