# Alignment Review: VERGIL Rename

**Date:** 2026-05-11
**Commit:** e47c5e6 (pre-fix baseline; plan updated in same commit as this report)

## Documents Reviewed

- **Intent:** `docs/specs/2026-05-11-vergil-rename-design.md`
- **Action:** `docs/plans/2026-05-11-vergil-rename.md`

## Source Control Conflicts

None — no conflicts with recent changes. The recent docker
image-prefix refactor (`feat(docker): make image prefix configurable`)
is consistent with the design spec, which notes `[docker] image-prefix`
needs no rename.

## Issues Reviewed

### [1] Dependency key vs. project name — sed patterns can't distinguish them

- **Category:** Missing coverage
- **Severity:** Critical
- **Documents:** Design spec "Config file" section, Plan Tasks 7-8
- **Issue:** The design spec defines two rename targets using
  "standard-tooling": the *package name* (`standard-tooling` →
  `vergil-tooling`) and the *TOML dependency key* (`standard-tooling`
  → `vergil`). The plan's blanket sed `s/standard-tooling/vergil-tooling/g`
  would produce `vergil-tooling = "v1.4"` in test fixtures when the
  correct value is `vergil = "v2.0"`. Affected files: `test_config.py`
  (10+ refs), `test_docker_cache.py` (20+ refs).
- **Resolution:** Accepted. Added a new Task 7 Step 4 with targeted
  replacements for dependency-key patterns (`standard-tooling = "v1.4"`
  → `vergil = "v2.0"`, `dependencies["standard-tooling"]` →
  `dependencies["vergil"]`) that must run before the blanket
  project-name pattern.

### [2] Env var and function renames missing from test-targeted sed

- **Category:** Missing coverage
- **Severity:** Important
- **Documents:** Design spec "Environment variables" section, Plan Tasks 6-7
- **Issue:** Plan renames `ST_COMMIT_CONTEXT`, `ST_DOCKER_INSTALL_TAG`,
  and `st_install_tag` in source code (Task 6) but not in test files.
  The test-targeting sed (Task 7 Step 4, now Step 5) only handled
  command names. Files affected: `test_pre_commit_gate.py` (10 refs),
  `test_git.py` (7 refs), `test_config.py` (6 `st_install_tag` refs +
  2 `ST_DOCKER_INSTALL_TAG` refs), `test_st_commit.py` (1 ref).
- **Resolution:** Accepted. Added `ST_COMMIT_CONTEXT`,
  `ST_DOCKER_INSTALL_TAG`, and `st_install_tag` patterns to the
  lib/test sed command (now Task 7 Step 5) and the verification grep
  (now Task 7 Step 6).

### [3] `pyproject.toml` package-data section not addressed

- **Category:** Missing coverage
- **Severity:** Important
- **Documents:** Design spec "Python package" section, Plan Task 5
- **Issue:** `pyproject.toml` line 41 has
  `standard_tooling = ["data/*.json", ...]` under
  `[tool.setuptools.package-data]`. Plan updated `[project]` and
  `[project.scripts]` but not this section.
- **Resolution:** Accepted. Added `[tool.setuptools.package-data]`
  update to Task 5 Step 3.

### [4] `v2.0` tracking tag missing from most repo release tasks

- **Category:** Missing coverage
- **Severity:** Important
- **Documents:** Design spec "Versioning" section, Plan Tasks 2, 9, 10
- **Issue:** Only Task 3 (vergil-actions) mentions the `v2.0` tracking
  tag. Tasks 2, 9, and 10 only reference `v2.0.0`.
- **Resolution:** Dismissed — the release workflow's CD pipeline
  creates tracking tags automatically. No plan change needed.

### [5] Plugin namespace verification missing

- **Category:** Missing coverage
- **Severity:** Important
- **Documents:** Design spec "Risks → Claude Code plugin namespace",
  Plan Task 10
- **Issue:** Design says "The plan must account for how the namespace
  is configured and ensure skills resolve correctly under the new name."
  Plan changed `plugin.json` name but had no verification step.
  Consumer repos referencing `standard-tooling:publish` etc. also need
  namespace updates.
- **Resolution:** Accepted. Added Task 10 Step 9 (namespace
  verification) and a `standard-tooling:` grep to Task 11 Step G.

### [6] `[project.co-authors]` decision point not acknowledged

- **Category:** Partially covered requirement
- **Severity:** Minor
- **Documents:** Design spec "Config file → Full field inventory",
  Plan Task 8
- **Issue:** Design flags bot account names as a decision point. Plan
  was silent, making the decision implicit rather than explicit.
- **Resolution:** Accepted. Added a note to Task 8 Step 2 confirming
  co-author names stay as-is.

## Unresolved Issues

None — all issues were addressed or dismissed with rationale.

## Alignment Summary

- **Requirements:** 14 identified, 14 covered (6 gaps found and fixed)
- **Tasks:** 12 tasks, all in scope, no orphaned tasks
- **Status:** Aligned after fixes applied
