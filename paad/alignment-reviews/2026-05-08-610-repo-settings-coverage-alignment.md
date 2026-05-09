# Alignment Review: repo settings coverage (#610)

**Date:** 2026-05-08
**Commit:** ff3215f

## Documents Reviewed

- **Intent:** `docs/specs/2026-05-08-610-repo-settings-coverage-design.md`
- **Action:** `docs/plans/2026-05-08-610-repo-settings-coverage.md`
- **Design:** none (design is embedded in the spec)

## Source Control Conflicts

None — no conflicts with recent changes.

## Issues Reviewed

### [1] Missing drift detection test for new fields

- **Category:** Missing coverage
- **Severity:** Important
- **Documents:** Spec test requirement ("Drift detection works for
  the new fields") had no corresponding step in the plan.
- **Issue:** The plan fixed existing diff test callers for the new
  signatures but never added a test verifying that drift on any of
  the 8 new fields is actually detected by `compute_diff`. The
  existing `test_diff_detects_repo_setting_mismatch` only covers
  `allow_auto_merge`.
- **Resolution:** Added a drift detection test to Task 3 (Steps 5-6)
  that mutates `merge_commit_title` and `web_commit_signoff_required`
  on the actual state and asserts those fields appear in the diff.
  The test is expected to pass immediately since the diff mechanism
  is generic.

## Unresolved Issues

None — all issues were addressed.

## Alignment Summary

- **Requirements:** 14 total, 14 covered, 0 gaps
- **Tasks:** 7 total, 7 in scope, 0 orphaned
- **Status:** Aligned (plan updated in place)
