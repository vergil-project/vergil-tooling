# Alignment Review: GitHub configuration enforcement

**Date:** 2026-05-04
**Commit:** b5214262bc1cd6cd43d62cd150823ecf06375e34

## Documents Reviewed

- **Intent:** docs/specs/2026-05-04-github-config-enforcement-design.md
- **Action:** docs/plans/2026-05-04-github-config-enforcement.md
- **Design:** none (spec serves as both)

## Source Control Conflicts

None — no conflicts with recent changes.

## Issues Reviewed

### [1] Apply safety gate is procedural, not coded into the tool
- **Category:** Design gap
- **Severity:** Important
- **Documents:** Spec "Safety gate" section vs Plan Tasks 12, 27-28
- **Issue:** The spec described a runtime safety gate ("apply refuses
  to act unless audit confirms CI workflows produce the expected
  check names"). The plan implemented it as a procedural sequence
  (Task 27 audits, then Task 28 applies). Discussion revealed the
  spec's language was wrong — `apply` is declarative and asserts the
  desired state unconditionally. The audit-first process during
  rollout is the mitigation, not a code-level gate.
- **Resolution:** Fix the spec — remove the "refuses to act"
  language. The plan's procedural approach (Tasks 27-28) is correct.
  The phased rollout (audit each repo before applying) is the
  documented mitigation, not a runtime check.

### [2] `list_project_repos` function referenced but not created
- **Category:** Missing coverage
- **Severity:** Important (initially)
- **Documents:** Plan Task 11 (line 1696)
- **Issue:** The CLI's `_resolve_repos()` calls
  `github.list_project_repos()`. Investigation revealed this function
  already exists in `lib/github.py` (line 81), shared with
  `st-ensure-label`.
- **Resolution:** False alarm — no change needed.

### [3] Task 30 container validation step contradicts host model
- **Category:** Design gap
- **Severity:** Moderate
- **Documents:** Plan Task 30, Step 4 (line 3566)
- **Issue:** Task 30 includes the validation command
  `st-docker-run -- uv run st-validate`, but by this point
  `st-validate` is host-orchestrated and calls `st-docker-run`
  internally. Running it inside a container would fail or produce
  recursive container invocations.
- **Resolution:** Corrected to `st-validate` (run directly on host).

### [4] No explicit task for updating interim check names
- **Category:** Missing coverage
- **Severity:** Minor
- **Documents:** Plan Task 6 note vs Phase 11 header
- **Issue:** Task 6 uses interim check names and notes they'll be
  updated when the registry is finalized. Phase 11 header mentions
  this but there's no dedicated step. The update is a mechanical
  find-and-replace in the derivation engine and its tests.
- **Resolution:** Added as an explicit step in Task 24 (first CI
  workflow refactor task, where the canonical names become real).

### [5] No task to create GitHub security reevaluation issue
- **Category:** Missing coverage
- **Severity:** Minor
- **Documents:** Spec "Reevaluation note" vs Plan
- **Issue:** The spec says a separate issue will be created to
  evaluate GitHub's security offerings. The plan had no step for this.
- **Resolution:** Added as a step in Task 31 (documentation updates).

## Unresolved Issues

None — all issues addressed.

## Alignment Summary

- **Requirements:** 18 identified, 18 covered, 0 gaps
- **Tasks:** 31 total, 31 in scope, 0 orphaned
- **Status:** Aligned (after corrections applied)
