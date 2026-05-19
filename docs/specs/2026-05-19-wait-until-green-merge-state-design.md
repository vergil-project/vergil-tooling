# vrg-wait-until-green: merge-state awareness

**Issue:** [#806](https://github.com/vergil-project/vergil-tooling/issues/806)
**Date:** 2026-05-19

## Problem

`vrg-wait-until-green` reports "All checks passed" and exits 0 when CI
checks are green but the PR is blocked by branch protection (required
reviews, merge restrictions, etc.). The break condition (`merge_state_status
!= "BEHIND"`) treats BLOCKED, DIRTY, UNSTABLE, and UNKNOWN as success.

## Design

### Exit code semantics

Exit 0 only for CLEAN. All other non-BEHIND states exit 1 with a message
that acknowledges CI passed but explains why the PR is not mergeable.

| `mergeStateStatus` | Exit | Message |
|---|---|---|
| CLEAN | 0 | "All checks passed." |
| BLOCKED | 1 | "All checks passed, but PR is not mergeable (BLOCKED)." + review context |
| DIRTY | 1 | "All checks passed, but PR is not mergeable (DIRTY)." |
| UNSTABLE | 1 | "All checks passed, but PR is not mergeable (UNSTABLE)." |
| UNKNOWN | 1 | "All checks passed, but PR is not mergeable (UNKNOWN)." |

For BLOCKED, query `reviewDecision` to provide additional context. Only
print the review line when `reviewDecision` is actionable
(`REVIEW_REQUIRED` or `CHANGES_REQUESTED`). If `reviewDecision` is
`APPROVED`, empty, or null, omit the review line — the blocker is
something other than reviews (signed commits, deployment gates, etc.).
In all BLOCKED cases, append a generic hint: "Check branch protection
settings."

### Changes

**`src/vergil_tooling/lib/github.py`** — add `merge_status(pr) -> dict`
helper that returns `{"mergeStateStatus": ..., "reviewDecision": ...}` from
a single `gh pr view --json mergeStateStatus,reviewDecision` call.

**`src/vergil_tooling/bin/vrg_wait_until_green.py`** — after the loop
breaks, call `merge_status()` instead of printing success unconditionally.
If CLEAN, print success and exit 0. Otherwise, print a diagnostic message
and exit 1.

**`tests/vergil_tooling/test_vrg_wait_until_green.py`** — update
`test_main_succeeds_for_non_behind_states` so only CLEAN is success.
Add tests for BLOCKED with/without review context. Add tests for DIRTY,
UNSTABLE, UNKNOWN exiting non-zero.

### Scope exclusions

- `vrg-merge-when-green` shares the same loop but is not in scope. It will
  fail at the merge step anyway if the PR is blocked. Can be addressed
  separately.
- No shared loop extraction. The duplication is small and a refactor is not
  warranted by this bug fix.
