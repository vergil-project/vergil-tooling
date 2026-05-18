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

### Task 2: Build the Local Audit Library

**Files:**
- Create: `src/vergil_tooling/lib/repo_config.py`

- [ ] **Step 1: Implement `audit_local_config()`**

Create `src/vergil_tooling/lib/repo_config.py` with the public
entry point:

```python
def audit_local_config(repo_root: Path) -> ConfigDiff:
```

Import `ConfigDiff` and `DiffItem` from
`vergil_tooling.lib.github_config`.

- [ ] **Step 2: Implement `_check_vergil_toml()`**

Check that `vergil.toml` exists at the repo root. If present,
delegate to the existing `read_config()` for field validation.
Field prefix: `local.vergil_toml`.

- [ ] **Step 3: Implement `_check_githooks()`**

Check that `.githooks/pre-commit` exists.
Field prefix: `local.githooks_pre_commit`.

- [ ] **Step 4: Implement `_check_claude_md()`**

Read the template from package data. Read the repo's `CLAUDE.md`.
Check whether the template text appears as a verbatim contiguous
substring. Field prefix: `local.claude_md`.

- [ ] **Step 5: Implement `_check_claude_settings()`**

Check `.claude/settings.json` exists, is valid JSON, is an object.
Then check:
- `extraKnownMarketplaces.vergil-marketplace` configured with
  source repo `vergil-project/vergil-claude-plugin`
  (field: `local.claude_settings.marketplace`)
- `enabledPlugins.vergil@vergil-marketplace` is `true`
  (field: `local.claude_settings.plugin`)
- `permissions.deny` contains all four required rules:
  `Bash(git *)`, `Bash(*/git *)`, `Bash(gh *)`, `Bash(*/gh *)`
  (field: `local.claude_settings.deny_rules`)

### Task 3: Write Tests for the Local Audit Library

**Files:**
- Create: `tests/vergil_tooling/test_repo_config.py`

- [ ] **Step 1: Write `vergil.toml` tests**

Test cases: missing file, malformed TOML, missing required fields,
valid file. Use `tmp_path` fixtures — no mocks, no API calls.

- [ ] **Step 2: Write `.githooks` tests**

Test cases: missing, present.

- [ ] **Step 3: Write `CLAUDE.md` tests**

Test cases: missing file, file without template block, file with
template block present (compliant), file with partial/modified
template (non-compliant). Test that exact match is enforced — a
single character difference must fail.

- [ ] **Step 4: Write `.claude/settings.json` tests**

Test cases: missing file, invalid JSON, not an object, missing
marketplace config, wrong marketplace repo, plugin not enabled,
missing deny rules, partial deny rules, all four deny rules
present. Include wrong-type edge cases for each nested field
(marketplace not an object, source not an object, plugins not an
object, permissions not an object, deny not a list).

- [ ] **Step 5: Write integration tests**

Test cases: empty directory reports all missing, fully compliant
repo passes.

- [ ] **Step 6: Run validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

All tests must pass. 100% coverage on `repo_config.py`.

---

## Phase 2 — Rename CLI and Integrate

### Task 4: Create `vrg-github-repo-config`

**Files:**
- Create: `src/vergil_tooling/bin/vrg_github_repo_config.py`
- Edit: `pyproject.toml` (add entry point)

- [ ] **Step 1: Create the new CLI module**

Copy `src/vergil_tooling/bin/vrg_github_config.py` to
`src/vergil_tooling/bin/vrg_github_repo_config.py`. Apply these
changes:
- Import `audit_local_config` from `vergil_tooling.lib.repo_config`
- Drop `--owner`/`--project` flags
- `audit`/`diff` modes: run local checks first via
  `audit_local_config(Path.cwd())`, print results, then run
  GitHub API checks
- `apply` mode: run local checks (report only), then run GitHub
  API apply. Return 1 if local issues remain after applying
  GitHub fixes.

- [ ] **Step 2: Add entry point**

In `pyproject.toml`, add:
```
vrg-github-repo-config = "vergil_tooling.bin.vrg_github_repo_config:main"
```

### Task 5: Delete `vrg-github-config`

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

### Task 6: Write Tests for the New CLI

**Files:**
- Create: `tests/vergil_tooling/test_vrg_github_repo_config.py`

- [ ] **Step 1: Write argument parsing tests**

Test cases: `audit`/`diff`/`apply` with `--repo`, `--config` flag,
no arguments (defaults), missing command fails. Verify
`--owner`/`--project` are not accepted.

- [ ] **Step 2: Write local + GitHub combined audit tests**

Test cases: both compliant returns 0, local non-compliant returns
1, GitHub non-compliant returns 1, `diff` always returns 0.
Use mocks for GitHub API calls.

- [ ] **Step 3: Write apply mode tests**

Test cases: all compliant does nothing, non-compliant applies
GitHub fixes, apply reports legacy protection removal, apply
returns 1 when local issues remain.

- [ ] **Step 4: Write helper function tests**

Test `_resolve_repos`, `_fetch_remote_config`,
`_load_local_config`, `_audit_repo`, `_apply_repo`.

- [ ] **Step 5: Run validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

All tests must pass. 100% coverage on both new files.

### Task 7: Update vergil-tooling's CLAUDE.md

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

Run `vrg-github-repo-config audit --local-only` (or the equivalent
test) from vergil-tooling and confirm the CLAUDE.md check passes.

### Task 8: Final Validation

- [ ] **Step 1: Full validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

All checks must pass. 100% coverage across all new and modified
files.

- [ ] **Step 2: Verify no stale references**

Grep the entire repo for `vrg-github-config`, `vrg_github_config`,
and `test_vrg_github_config`. Zero hits expected.
