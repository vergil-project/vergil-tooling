# Alignment Review: vrg-release

**Date:** 2026-05-20
**Commit:** 087956ff

## Documents Reviewed

- **Intent:** `docs/specs/2026-05-20-vrg-release-design.md`
- **Action:** `docs/plans/2026-05-20-vrg-release-plan.md`
- **Design:** same as intent (combined spec)

## Source Control Conflicts

None — no conflicts with recent changes. Recent commits to
`lib/github.py` confirmed that all helper functions referenced by the
plan (`create_pr`, `mergeable`, `wait_for_checks`, `merge_state_status`,
`update_branch`, `merge`) already exist. Similarly, `lib/git.py` has
`ref_exists`, `current_branch`, `repo_root`.

## Issues Reviewed

### [1] Unhandled CalledProcessError from subprocess calls

- **Category:** missing coverage
- **Severity:** critical
- **Documents:** plan orchestrator and CLI entry point vs. spec failure model
- **Issue:** Phase modules call `git.run()`, `github.run()`, etc. which
  raise `CalledProcessError` on failure. The orchestrator only caught
  `ReleaseError`, so subprocess failures would bypass tracking issue
  comments and produce raw tracebacks.
- **Resolution:** Orchestrator catches broad `Exception`, wraps
  non-`ReleaseError` into `ReleaseError` with command/stderr extraction.
  CLI entry point gets a parallel catch-all.

### [2] Empty phase details in orchestrator completion comments

- **Category:** missing coverage
- **Severity:** important
- **Documents:** plan orchestrator vs. spec tracking issue design
- **Issue:** `comment_phase_complete(ctx, phase_name, "")` was called
  with empty details for every phase, making the tracking issue useless
  as an operational log.
- **Resolution:** Added `_phase_details(ctx, phase)` function that
  builds details from ctx fields populated by each phase. Orchestrator
  passes these to completion comments.

### [3] Tracking tests check wrong location for phase markers

- **Category:** missing coverage (test bug)
- **Severity:** important
- **Documents:** plan Task 4 tests
- **Issue:** Tests asserted phase marker strings appeared in
  `github.run` call args, but the body is written to a temp file passed
  via `--body-file`. Tests would always fail.
- **Resolution:** Tests now capture temp file content via a
  `NamedTemporaryFile` wrapper and assert on the captured body text.

### [4] Bump PR timeout test will hang

- **Category:** missing coverage (test bug)
- **Severity:** important
- **Documents:** plan Task 8 tests
- **Issue:** `test_merge_bump_times_out` patched `time.sleep` but not
  `time.monotonic`. With sleep mocked (instant), real monotonic time
  never reaches the 300-second deadline — the test runs millions of
  iterations.
- **Resolution:** Test now patches `time.monotonic` with
  `side_effect=[0.0, 301.0]` to simulate immediate timeout.

### [5] release_merge_sha set to literal "merged"

- **Category:** missing coverage
- **Severity:** minor
- **Documents:** plan orchestrator vs. spec ReleaseContext
- **Issue:** Spec says to populate `release_merge_sha` but the
  orchestrator sets it to the string `"merged"` instead of the actual
  SHA. The field is never used in the summary.
- **Resolution:** Accepted as-is for v1. Low value — getting the real
  SHA requires an extra API call after merge.

### [6] Task 15 not in spec

- **Category:** out of scope
- **Severity:** minor
- **Documents:** plan Task 15 vs. spec
- **Issue:** CLAUDE.md update task doesn't trace to a spec requirement.
- **Resolution:** Accepted — reasonable documentation maintenance.

## Pre-review fix (self-review)

Before the alignment review, a self-review caught that `prepare.py` was
missing the `create_tracking_issue(ctx)` call. Without it, no tracking
issue would ever be created. Fixed by adding the call at the top of
`prepare()`, updating imports, and adding an ordering test.

## Alignment Summary

- **Requirements:** 28 checked, 27 covered, 1 minor gap (merge SHA)
- **Tasks:** 15 total, 14 in scope, 1 reasonable out-of-scope addition
- **Status:** aligned (after fixes)
