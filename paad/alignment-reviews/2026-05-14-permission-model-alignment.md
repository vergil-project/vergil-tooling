# Alignment Review: Claude Code Permission Model

**Date:** 2026-05-14
**Commit:** 223a300f0c55bcdda8a02bf89cd8061b60658238

## Documents Reviewed

- **Intent:** docs/specs/2026-05-14-permission-model-design.md
- **Action:** docs/plans/2026-05-14-permission-model.md
- **Design:** none (spec serves as both intent and design)

## Source Control Conflicts

None — no conflicts with recent changes. Verified during the
preceding pushback review.

## Issues Reviewed

### [1] Plan puts new tools in `cli/` but existing convention is `bin/`

- **Category:** Design gap
- **Severity:** Important
- **Documents:** Plan Tasks 1-2 vs codebase convention
- **Issue:** Tasks 1 and 2 specified `src/vergil_tooling/cli/` for
  the new wrappers, but all 13 existing tools live in
  `src/vergil_tooling/bin/` and all pyproject.toml entry points
  reference `vergil_tooling.bin.*`. The `cli/` directory did not
  exist.
- **Resolution:** Changed to `src/vergil_tooling/bin/` to follow
  existing convention. Applied to plan.

### [2] Plan says `click` but project uses `argparse`

- **Category:** Design gap
- **Severity:** Important
- **Documents:** Plan Tech Stack header vs codebase dependencies
- **Issue:** The plan's Tech Stack line said "Python CLI tools
  (click)" but every existing tool uses `argparse` and `click` is
  not a project dependency. Introducing click would add a new
  dependency and create framework inconsistency.
- **Resolution:** Changed to `argparse`. The user noted that `click`
  is being considered for a future unified `vrg` CLI (v3.0 or
  similar milestone) but does not belong in this spec/plan cycle.
  Applied to plan.

### [3] Dependency graph text contradicts diagram

- **Category:** Ambiguity
- **Severity:** Minor
- **Documents:** Plan Implementation Notes (text vs ASCII diagram)
- **Issue:** The text said "Task 4 can begin as soon as Task 1
  produces a working vrg-git" but the diagram shows Task 4 after
  Tasks 1-3 converge.
- **Resolution:** Updated text to match the diagram: "Task 4 begins
  after Tasks 1-3 complete." Applied to plan.

## Unresolved Issues

None — all issues addressed.

## Alignment Summary

- **Requirements:** 7 spec sections, all covered by plan tasks
- **Tasks:** 11 total, all in scope, 0 orphaned
- **Status:** Aligned — ready for implementation
