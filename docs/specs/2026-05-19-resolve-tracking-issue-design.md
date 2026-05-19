# Design: `vrg-resolve-tracking-issue`

**Issue:** [#858](https://github.com/vergil-project/vergil-tooling/issues/858)
**Date:** 2026-05-19
**Status:** Draft

## Summary

A Python CLI tool that deterministically extracts the release tracking
issue number from a merge commit on main. Consumed by the
`version-bump-pr` composite action in vergil-actions to link the
version bump PR to its tracking issue.

## Motivation

The `version-bump-pr` action currently discovers the tracking issue by
scanning open issues for a title matching `release: <version>`. This is
fragile and fails silently when the title format does not match exactly.
The tracking issue number is already present in the release PR body (as
`Ref #N`), enforced by `vrg-prepare-release` and validated by the
`vrg-pr-issue-linkage` CI gate. This tool extracts it from the merge
commit rather than scanning by title.

Downstream consumer: `vergil-project/vergil-actions#495`.

## Architecture

### Approach

Thin CLI tool using existing `lib/git` and `lib/github` wrappers.
Three sequential steps, each using established library functions.
No new abstractions beyond a shared linkage regex module.

### Data flow

```text
merge commit (HEAD or --commit <sha>)
  |
  +-- git log -1 --format=%s <commit>
  |     -> "Merge pull request #42 from org/feature/123-foo"
  |     -> extract PR number: 42
  |
  +-- gh api repos/{owner/repo}/pulls/42
  |     -> PR body text
  |     -> extract issue number from "Ref #123" pattern
  |
  +-- stdout: "123"
```

## Components

### `lib/linkage.py` (new shared module)

Extracts the `Ref #N` and auto-close regex patterns from
`vrg_pr_issue_linkage.py` into a shared location. Both
`vrg_pr_issue_linkage` and the new tool import from here.

```python
LINKAGE_RE: re.Pattern[str]
    # Matches "Ref #N" or "Ref owner/repo#N" at line start,
    # with capture group for the issue number.

AUTOCLOSE_RE: re.Pattern[str]
    # Matches forbidden auto-close keywords (close/fix/resolve).

def extract_tracking_issue(text: str) -> int | None:
    # Returns the issue number from the Ref #N match, or None
    # if no match. Raises ValueError if multiple Ref #N lines
    # are found — ambiguous input is rejected, not guessed.
```

`vrg_pr_issue_linkage.py` is refactored to import `LINKAGE_RE` and
`AUTOCLOSE_RE` from `lib/linkage.py`, removing its local definitions.
Behavior is preserved exactly.

### `bin/vrg_resolve_tracking_issue.py` (new CLI tool)

Standard `parse_args()` + `main()` pattern.

**Arguments:**
- `--commit` (optional, default: `HEAD`) — the merge commit to inspect.

**`_extract_pr_number(subject: str) -> int | None`:**
Regex on GitHub's merge commit subject format:
`Merge pull request #(\d+) from ...`. Returns the PR number or None.

**`main(argv)` orchestration:**

1. Parse args.
2. Get commit subject: `git.read_output("log", "-1", "--format=%s", commit)`.
3. Extract PR number. Exit 1 if not a merge commit.
4. Get current repo: `github.current_repo()` (existing wrapper, returns `"OWNER/REPO"`).
5. Fetch PR body: `github.read_json("api", f"repos/{repo}/pulls/{pr_num}")`, then read `["body"]`.
6. Extract tracking issue: `linkage.extract_tracking_issue(body)`. Exit 1 if not found.
7. Print issue number to stdout. Exit 0.

### Console script registration

```toml
vrg-resolve-tracking-issue = "vergil_tooling.bin.vrg_resolve_tracking_issue:main"
```

## Error handling

All errors print a diagnostic message to stderr. Exit codes follow the
three-state convention (vergil-tooling#373):

| Failure mode | Exit | Message |
|---|---|---|
| Commit is not a PR merge | 1 | `commit {sha} is not a merge commit (expected 'Merge pull request #N from ...' — squash and rebase merges are not supported)` |
| PR body is empty | 1 | `PR #{n} has no body` |
| No `Ref #N` in PR body | 1 | `PR #{n} body has no tracking issue linkage (expected 'Ref #N')` |
| Multiple `Ref #N` in PR body | 1 | `PR #{n} body has multiple tracking issue references (expected exactly one)` |
| git/gh subprocess failure | 2 | `failed to {action}: {error}` |

No fallbacks, no scanning, no guessing. Any failure exits non-zero.

### Merge strategy constraint

This tool requires the GitHub "merge commit" strategy (`--merge`),
which produces subjects in the form `Merge pull request #N from ...`.
Squash and rebase merges use different subject formats and are not
supported. `vrg-merge-when-green` defaults to `--merge`, so the
release workflow is compatible. If a repo configures a different
default merge strategy, the tool will fail loudly with a diagnostic
pointing at the format mismatch.

## Testing

### Unit tests: `test_vrg_resolve_tracking_issue.py`

Mock `git.read_output` and `github.read_output`/`github.read_json` at
the module boundary.

| Test case | Expected |
|---|---|
| Successful end-to-end extraction | stdout = issue number, exit 0 |
| Commit subject is not a merge | exit 1, diagnostic on stderr |
| PR body is empty | exit 1, diagnostic on stderr |
| PR body has no `Ref #N` | exit 1, diagnostic on stderr |
| PR body has multiple `Ref #N` | exit 1, diagnostic on stderr |
| gh API subprocess failure | exit 2, diagnostic on stderr |
| `--commit` argument forwarded to git | git called with correct sha |

### Unit tests: `test_linkage.py`

| Test case | Expected |
|---|---|
| `Ref #123` | returns 123 |
| `Ref: #456` | returns 456 |
| `- Ref #789` (list item) | returns 789 |
| `Ref org/repo#42` (cross-repo) | returns 42 |
| No match | returns None |
| Multiple `Ref` lines | raises ValueError |
| `vrg_pr_issue_linkage` preserved | existing behavior unchanged after refactor |

### Integration test (fixture-based)

Full `main()` invocation with a recorded commit message and mock API
response matching a real merge commit pattern from this repository.
No live API calls; deterministic in CI.

## Documentation

Update the CLI tool list in `CLAUDE.md` and relevant docs to include
`vrg-resolve-tracking-issue`.
