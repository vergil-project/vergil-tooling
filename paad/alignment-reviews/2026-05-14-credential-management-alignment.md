# Alignment Review: Credential Management

**Date:** 2026-05-14
**Commit:** c6decf9

## Documents Reviewed

- **Intent:** docs/specs/2026-05-14-credential-management-design.md
- **Action:** docs/plans/2026-05-14-credential-management.md
- **Design:** none (spec serves as both intent and design)

## Source Control Conflicts

None — no conflicts with recent changes.

## Issues Reviewed

### [1] Spec Section 5 changes have no owner
- **Category:** Missing coverage
- **Severity:** Important
- **Documents:** Spec Section 5 (github.py integration) has no corresponding plan task
- **Issue:** Mechanized tools (`vrg-merge-when-green`, `vrg-prepare-release`) must
  be updated to set `GH_TOKEN` per-phase. Neither this plan nor the permission model
  plan covered these changes.
- **Resolution:** Expand the permission model plan's Task 2 scope to include
  mechanized tool updates. Ship in the same PR as `vrg-gh`. This plan's Task 4
  cross-reference updated to note the expanded scope.

### [2] Missing task — close #761 as won't-fix
- **Category:** Missing coverage
- **Severity:** Minor
- **Documents:** Spec Section 3 and Section 7 reference closing #761
- **Issue:** No plan task covers closing #761.
- **Resolution:** Issue #761 is already closed. References removed from the spec's
  "What Gets Updated" table. "What Gets Retired" table updated to note the issue
  is already closed.

### [3] Missing follow-on issue for vrg-credential-audit
- **Category:** Missing coverage
- **Severity:** Minor
- **Documents:** Spec Section 8 lists four deferred items; Task 6 creates issues
  for three
- **Issue:** The fourth deferred item (whether `vrg-credential-audit` survives in
  modified form) had no tracking.
- **Resolution:** Folded into Task 6 Step 1 (token expiration monitoring issue).
  The expiration monitoring issue now includes evaluating whether
  `vrg-credential-audit` should be revived as the implementation vehicle.

### [4] CLAUDE.md update not tracked
- **Category:** Missing coverage
- **Severity:** Minor
- **Documents:** Spec Section 7 "What Gets Updated" lists vergil-tooling CLAUDE.md
- **Issue:** No plan task covers updating CLAUDE.md to describe credential selection.
- **Resolution:** Removed from the spec's "What Gets Updated" table. Credential
  selection is enforced mechanically by `vrg-gh`, not by CLAUDE.md instructions.
  Adding it to CLAUDE.md would clutter the file without providing enforcement value.

### [5] #777 not referenced in the plan
- **Category:** Missing coverage
- **Severity:** Minor
- **Documents:** Spec Section 6 references #777; Plan Task 1 does not
- **Issue:** Implementer reading only the plan would not know about the related
  env-var passthrough cleanup issue.
- **Resolution:** Added a note to Task 1 referencing #777.

## Alignment Summary

- **Requirements:** 9 spec sections, 7 covered by plan tasks, 2 correctly
  deferred (identity model unchanged, token strategy describes existing state)
- **Tasks:** 7 total, 7 in scope, 0 orphaned
- **Status:** Aligned after fixes
