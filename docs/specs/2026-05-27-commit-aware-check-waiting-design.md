# Commit-aware check waiting

**Issue:** [#1195](https://github.com/vergil-project/vergil-tooling/issues/1195)
**Date:** 2026-05-27

## Context

`vrg-wait-until-green` wraps `gh pr checks --watch --fail-fast` to
block until a PR's CI checks pass. Three related problems make this
unreliable:

1. **GHAS stale check causes early exit.** GitHub Advanced Security
   checks (CodeQL, Semgrep) evaluate existing open alerts immediately
   on push (~7s), before the CI analysis job uploads fresh SARIF
   (~1-2 min). `--fail-fast` exits on the stale failure before the
   check self-corrects.

2. **`_checks_registered` is satisfied by stale checks.** After a
   new push, `_checks_registered` calls `gh pr checks` which can
   return checks from the previous commit. The code starts watching
   before the new commit's checks exist.

3. **No commit-scoping.** Nothing in the current code pins check
   queries to a specific HEAD SHA.

All three were observed during PR workflow automation
(vergil-tooling#1192, vergil-tooling#1194).

## Decisions

### Drop `--fail-fast`

The `--fail-fast` flag on `gh pr checks --watch` is the direct cause
of problem #1. Removing it makes the watch block until all checks
reach a terminal state (pass, fail, or cancelled), then exit non-zero
if any failed.

**Trade-off:** a genuine failure in one check will not surface until
all checks complete (~5-8 min worst case). This is acceptable — the
agent needs the full picture to decide what to fix, and fast feedback
on genuine failures is not a requirement.

### SHA-pinned check registration

Replace the PR-scoped `_checks_registered` with a commit-scoped REST
API query. Before starting the watch, resolve the PR's HEAD SHA and
poll the GitHub REST API for check runs on that specific commit.

**New function — `head_sha`:**

```python
def head_sha(pr: str) -> str:
    """Return the HEAD commit SHA for a PR."""
    return read_output(
        "pr", "view", pr, "--json", "headRefOid", "--jq", ".headRefOid"
    )
```

**Modified `_checks_registered`:**

Current signature: `_checks_registered(pr: str) -> bool`
New signature: `_checks_registered(repo: str, sha: str) -> bool`

Calls the GitHub REST API via `_run_with_retry` (the same internal
path used by `write_json`, `delete`, and other direct API callers in
`github.py` — not through `vrg-gh`, which denies `api`):

```python
_run_with_retry(
    ("gh", "api", f"repos/{repo}/commits/{sha}/check-runs",
     "--jq", ".total_count"),
    ...
)
```

Returns `True` when the count is greater than zero.

### Updated `wait_for_checks` flow

```
1. repo = current_repo()
2. sha  = head_sha(pr)
3. Poll _checks_registered(repo, sha) until true or timeout
4. run("pr", "checks", pr, "--watch")   # no --fail-fast
```

Step 4 still uses `gh pr checks --watch` (PR-scoped). By step 3,
checks for the current commit are registered, so the watch reflects
the correct data. The `--watch` flag blocks until all checks reach a
terminal state.

The function signature does not change — `sha` is resolved internally
so callers are unaffected.

### No changes to `vrg-wait-until-green`

The outer loop in `vrg-wait-until-green` calls `wait_for_checks(pr)`
on each iteration. After an `update_branch()` call pushes a new merge
commit, the next `wait_for_checks` call re-resolves the HEAD SHA
automatically. No changes needed to the outer loop, conflict
detection, or retry logic.

## Files changed

| File | Change |
|---|---|
| `src/vergil_tooling/lib/github.py` | New `head_sha()`, modified `_checks_registered()` (SHA-pinned REST API), modified `wait_for_checks()` (drop `--fail-fast`, resolve SHA) |
| `src/vergil_tooling/bin/vrg_wait_until_green.py` | No changes |
| `tests/vergil_tooling/test_github.py` | Tests for `head_sha`, updated `_checks_registered` tests, updated `wait_for_checks` tests |
| `tests/vergil_tooling/test_vrg_wait_until_green.py` | Existing tests pass unchanged (signature unchanged) |

## What does NOT change

- `vrg-wait-until-green` outer loop (branch update logic, conflict
  detection, max retries)
- `_checks_registered` timeout behavior (poll interval and timeout
  parameters)
- Error reporting (`GitHubAPIError` on timeout, `CalledProcessError`
  on check failure). The timeout message includes the SHA for
  debuggability: `no checks reported for {sha[:8]} after
  {poll_timeout}s — GitHub may be experiencing delays`
- `gh pr checks --watch` as the blocking mechanism (we remove
  `--fail-fast` but keep the watch)

## Problem-to-fix mapping

| Problem | Fix |
|---|---|
| GHAS stale check causes early exit | Drop `--fail-fast` — wait for all checks to settle |
| `_checks_registered` satisfied by stale checks | SHA-pinned REST API query — only sees current commit's checks |
| No commit-scoping | `head_sha()` + SHA-pinned registration polling |
