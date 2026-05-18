# Repository Configuration Audit Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shared CLAUDE.md template, local filesystem audit library, and renamed CLI tool (`vrg-github-repo-config`) that validates consuming repos have the required configuration for vergil-tooling.

**Architecture:** Two phases executed sequentially. Phase 1 builds the template and audit library with no breaking changes. Phase 2 renames the CLI tool and integrates local checks.

**Tech Stack:** Python (vergil-tooling), pytest, `importlib.resources` for package data.

**Spec:** `docs/specs/2026-05-18-repo-config-audit-design.md`

---

## Phase 1 — Template and Local Audit Library

No breaking changes. The existing `vrg-github-config` command is
untouched in this phase.

### Task 1: Create the CLAUDE.md Consumer Template

**Requirement:** Spec Section 1 — shared template file containing the
four mandatory CLAUDE.md sections.

**Files:**
- Create: `src/vergil_tooling/data/claude_md_consumer.md`
- Edit: `pyproject.toml` (add `"data/*.md"` to package-data glob)

- [ ] **Step 1: Create the template file**

Create `src/vergil_tooling/data/claude_md_consumer.md` with the
exact content defined in the spec (sections 1–4: memory management,
parallel AI agent development, shell command policy, validation).
This file is the canonical source — the audit library reads it at
runtime.

- [ ] **Step 2: Add package-data glob**

In `pyproject.toml`, ensure `"data/*.md"` is included in the
package-data configuration so the template ships with the installed
package.

- [ ] **Step 3: Verify the template loads**

Write a quick smoke test or manually verify that
`importlib.resources` (or the chosen loading mechanism) can read
the template from the installed package.

### Task 2: Build the Local Audit Library (TDD)

**Requirement:** Spec Section 3 — all seven local filesystem checks,
implemented as `audit_local_config(repo_root: Path) -> ConfigDiff`.

**Files:**
- Create: `src/vergil_tooling/lib/repo_config.py`
- Create: `tests/vergil_tooling/test_repo_config.py`

Start by creating both files. The lib module needs the public entry
point signature and imports:

```python
from vergil_tooling.lib.github_config import ConfigDiff, DiffItem

def audit_local_config(repo_root: Path) -> ConfigDiff:
```

Each cycle below adds one check. Use `tmp_path` fixtures throughout
— no mocks, no API calls. All checks are pure filesystem operations.

#### Cycle 1: `_check_vergil_toml()` — field prefix `local.vergil_toml`

##### RED
- [ ] Write tests for: missing file, malformed TOML, missing
  required fields, valid file.
- Expected failure: no implementation yet, all tests fail.
- If any passes unexpectedly: the fixture setup is wrong or the
  test isn't actually calling the check.

##### GREEN
- [ ] Implement `_check_vergil_toml()`. Check file exists at repo
  root. If present, delegate to existing `read_config()` for field
  validation.
- Constraints: reuse `read_config()` — do not duplicate its
  validation logic.

##### REFACTOR
- [ ] Review: is the DiffItem output clear about what's missing
  vs. what's malformed? Adjust expected/actual messages if needed.

#### Cycle 2: `_check_githooks()` — field prefix `local.githooks_pre_commit`

##### RED
- [ ] Write tests for: missing `.githooks/pre-commit`, present.
- Expected failure: function not implemented.

##### GREEN
- [ ] Implement `_check_githooks()`. Check file exists.

##### REFACTOR
- [ ] Minimal — this is a simple existence check.

#### Cycle 3: `_check_claude_md()` — field prefix `local.claude_md`

##### RED
- [ ] Write tests for: missing file, file without template block,
  file with template block present (compliant), file with
  partial/modified template (non-compliant). Test that exact
  match is enforced — a single character difference must fail.
- Expected failure: function not implemented.
- If the partial-template test passes unexpectedly: the substring
  match is too loose.

##### GREEN
- [ ] Implement `_check_claude_md()`. Load template from package
  data. Read repo's CLAUDE.md. Check verbatim substring match.

##### REFACTOR
- [ ] Verify the template loading mechanism works correctly with
  `importlib.resources`. Ensure the template file from Task 1
  loads without path issues.

#### Cycle 4: `_check_claude_settings()` — field prefixes `local.claude_settings.*`

##### RED
- [ ] Write tests for: missing file, invalid JSON, not an object,
  missing marketplace config, wrong marketplace repo, plugin not
  enabled, missing deny rules, partial deny rules (only two of
  four), all four deny rules present.
- [ ] Include wrong-type edge cases for each nested field:
  marketplace not an object, source not an object, plugins not
  an object, permissions not an object, deny not a list.
- Expected failure: function not implemented.

##### GREEN
- [ ] Implement `_check_claude_settings()`. Check file exists, is
  valid JSON, is an object. Then check:
  - `extraKnownMarketplaces.vergil-marketplace` configured with
    source repo `vergil-project/vergil-claude-plugin`
    (field: `local.claude_settings.marketplace`)
  - `enabledPlugins.vergil@vergil-marketplace` is `true`
    (field: `local.claude_settings.plugin`)
  - `permissions.deny` contains all four required rules:
    `Bash(git *)`, `Bash(*/git *)`, `Bash(gh *)`, `Bash(*/gh *)`
    (field: `local.claude_settings.deny_rules`)

##### REFACTOR
- [ ] Look for repeated JSON path traversal patterns. If multiple
  sub-checks do similar nested-key lookups with type guards,
  extract a helper.

#### Cycle 5: `audit_local_config()` integration

##### RED
- [ ] Write integration tests: empty directory reports all checks
  as missing/failed, fully compliant directory passes all checks.
- Expected failure: public entry point doesn't call individual
  checks yet.

##### GREEN
- [ ] Wire all four check functions into `audit_local_config()`.
  Return a single `ConfigDiff` combining all results.

##### REFACTOR
- [ ] Review composition: are the checks cleanly independent? Does
  the ConfigDiff aggregate correctly? Ensure no check short-circuits
  the others.

#### Phase 1 validation

- [ ] Run `vrg-docker-run -- uv run vrg-validate`. All tests must
  pass. 100% coverage on `repo_config.py`.

---

## Phase 2 — Rename CLI and Integrate

### Task 3: Create `vrg-github-repo-config` (TDD)

**Requirement:** Spec Section 2 — renamed CLI tool combining local
filesystem checks with existing GitHub API checks.

**Files:**
- Create: `src/vergil_tooling/bin/vrg_github_repo_config.py`
- Create: `tests/vergil_tooling/test_vrg_github_repo_config.py`
- Edit: `pyproject.toml` (add entry point)

Start by copying `src/vergil_tooling/bin/vrg_github_config.py` to
the new path and adding the entry point in `pyproject.toml`:

```
vrg-github-repo-config = "vergil_tooling.bin.vrg_github_repo_config:main"
```

Then apply changes through TDD cycles. Use mocks for GitHub API
calls — only local audit logic is tested against real filesystems.

#### Cycle 1: Argument parsing

##### RED
- [ ] Write tests for: `audit`/`diff`/`apply` with `--repo`,
  `--config` flag, no arguments (defaults), missing subcommand
  fails. Verify `--owner`/`--project` are rejected.
- Expected failure: copied CLI still accepts `--owner`/`--project`.

##### GREEN
- [ ] Drop `--owner`/`--project` flags from the argument parser.
  Keep `--repo` and `--config`.

##### REFACTOR
- [ ] Clean up any dead code left from the project-wide scanning
  logic (imports, helper functions that only served `--owner`/
  `--project`).

#### Cycle 2: Combined local + GitHub audit

##### RED
- [ ] Write tests for: both compliant returns 0, local
  non-compliant returns 1, GitHub non-compliant returns 1, `diff`
  always returns 0.
- Expected failure: local checks not yet wired into audit flow.

##### GREEN
- [ ] Import `audit_local_config` from
  `vergil_tooling.lib.repo_config`. In `audit`/`diff` modes, run
  local checks first via `audit_local_config(Path.cwd())`, print
  results, then run GitHub API checks. Follow the output format
  in spec Section 4 (local results before GitHub,
  `compliant`/`NON-COMPLIANT` labels with field-level diffs).

##### REFACTOR
- [ ] Review output formatting. Ensure local and GitHub results
  are visually distinct and the combined exit code logic is clean.

#### Cycle 3: Apply mode

##### RED
- [ ] Write tests for: all compliant does nothing, non-compliant
  applies GitHub fixes, apply returns 1 when local issues remain
  after GitHub fixes are applied.
- Expected failure: apply mode doesn't check local config yet.

##### GREEN
- [ ] In `apply` mode: run local checks (read-only report), then
  run GitHub API apply. Return exit code 1 if local issues remain.

##### REFACTOR
- [ ] Look for shared logic between audit/diff/apply modes that
  can be consolidated. Check that helper functions
  (`_resolve_repos`, `_fetch_remote_config`, `_audit_repo`,
  `_apply_repo`) are clean after the changes.

#### CLI validation

- [ ] Run `vrg-docker-run -- uv run vrg-validate`. All tests must
  pass. 100% coverage on both new files.

### Task 4: Delete `vrg-github-config`

**Files:**
- Delete: `src/vergil_tooling/bin/vrg_github_config.py`
- Delete: `tests/vergil_tooling/test_vrg_github_config.py`
- Edit: `pyproject.toml` (remove old entry point)

- [ ] **Step 1: Remove the old entry point from `pyproject.toml`**

Delete the `vrg-github-config` line from `[project.scripts]`.

- [ ] **Step 2: Delete the old source file**

Remove `src/vergil_tooling/bin/vrg_github_config.py`.

- [ ] **Step 3: Delete the old test file**

Remove `tests/vergil_tooling/test_vrg_github_config.py`.

- [ ] **Step 4: Sweep for stale references**

Search the codebase for any remaining references to
`vrg-github-config` or `vrg_github_config` and update them.

### Task 5: Update vergil-tooling's CLAUDE.md

**Requirement:** Spec Section 1 (vergil-tooling exception) — template
block inserted verbatim, followed by the `uv run` override note.

**Files:**
- Edit: `CLAUDE.md`

- [ ] **Step 1: Insert the template block**

Add the verbatim template block near the top of `CLAUDE.md`, after
the introductory line. The four sections (memory management,
parallel AI agent development, shell command policy, validation)
replace the existing equivalent sections.

- [ ] **Step 2: Add the vergil-tooling override**

Immediately after the validation section, add:

> **Note:** This repository uses
> `vrg-docker-run -- uv run vrg-validate` because it runs its own
> unreleased code rather than the pre-installed version.

- [ ] **Step 3: Verify self-audit passes**

Run `vrg-github-repo-config audit` from vergil-tooling and confirm
the CLAUDE.md template check passes alongside the other local and
GitHub checks.

### Task 6: Final Validation

- [ ] **Step 1: Full validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

All checks must pass. 100% coverage across all new and modified
files.

- [ ] **Step 2: Verify no stale references**

Grep the entire repo for `vrg-github-config`, `vrg_github_config`,
and `test_vrg_github_config`. Zero hits expected.
