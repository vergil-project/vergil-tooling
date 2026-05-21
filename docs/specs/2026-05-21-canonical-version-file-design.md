# Canonical VERSION File Design

**Issue:** #970
**Date:** 2026-05-21
**Status:** Draft

## Problem

When `vrg-github-repo-init` bootstraps a new repository, the VERSION
file is not created during initialization. The first PR fails the
`version-bump` CI check because `vrg-version show` crashes with
`FileNotFoundError`.

The deeper problem: version discovery is language-dependent. The
version source varies by `primary-language` (pyproject.toml for
Python, Cargo.toml for Rust, plugin.json for Claude plugins, etc.),
which means the version file can't exist until language scaffolding
is done. But CI needs a version before that happens — a
chicken-and-egg.

Additionally, every generic tool that needs the repo version must
understand the language-specific version file format, creating
unnecessary coupling between language ecosystems and cross-cutting
tooling.

## Solution

Establish a universal `VERSION` file at the repo root as the
canonical version source for every vergil-managed repository.
Language-specific files continue to carry the version (their
ecosystems require it), but `vrg-version` enforces that both agree.

This breaks the chicken-and-egg: the init wizard creates `VERSION`
during bootstrap before any language scaffolding. Generic tooling
always reads `VERSION` — no language-specific parsing needed.

## The VERSION file convention

Every vergil-managed repository has a `VERSION` file at the repo
root. It contains exactly one line: a bare semver string (e.g.,
`2.0.28`). No prefix, no trailing content.

**Relationship to language-specific files:** Languages that embed
version in their own manifest (pyproject.toml, Cargo.toml,
plugin.json, etc.) continue to do so. The `VERSION` file is the
source of truth for vergil tooling. `vrg-version` enforces that
both locations agree.

**Repos where VERSION is the only location:** For `shell` and
`none` primary-language repos, VERSION is the sole version source.
No dual-write, no cross-check — behavior is unchanged from today.

**Removed:** The `version-file` override in vergil.toml is
eliminated. VERSION is always at the repo root; language-specific
files are always at their ecosystem-standard locations.

## `vrg-version` changes

### `show` subcommand

1. Read `VERSION` from the repo root (or via `git show <ref>:VERSION`
   for a specific git ref).
2. Read `primary-language` from vergil.toml.
3. If the language has a separate version file (python, rust, java,
   ruby, go, claude-plugin), read that file and compare.
4. If they disagree: hard error with both values printed —
   "VERSION contains X but <language-file> contains Y".
5. If the language is `shell` or `none`: skip cross-check.
6. Return the version from VERSION.

### `bump` subcommand

1. Read current version from `VERSION`.
2. Increment patch.
3. Write new version to `VERSION`.
4. If the language has a separate version file, update it too (using
   existing `_write_version` logic for language-specific formats).
5. Run lockfile maintenance (existing behavior).
6. Return new version.

### Discovery simplification

The primary read path always reads `VERSION` at the repo root. No
language-based discovery needed for the read. Language-specific file
discovery (`_discover_version_file`) is retained only for the
cross-check and for writing the language file during bump.

The `version-file` override in vergil.toml is removed from the
code path entirely.

## vergil.toml parser: unrecognized-key warnings

The vergil.toml parser (`config.py`) gains schema-aware validation
for unrecognized keys. The parser already knows the valid sections
and fields — this change makes that knowledge actionable.

**Behavior:** After parsing, compare each section's keys against the
known set. Emit a warning to stderr for any unrecognized key:
`vergil.toml: unrecognized key 'version-file' in [project]`.

**Known keys per section:**

- Top-level: `project`, `dependencies`, `markdownlint`, `ci`,
  `publish`
- `[project]`: `repository-type`, `versioning-scheme`,
  `branching-model`, `release-model`, `primary-language`
- `[dependencies]`: `vergil` (additional keys allowed without
  warning — repos may declare other dependencies)
- `[markdownlint]`: `ignore`
- `[ci]`: `versions`, `integration-tests`
- `[publish]`: `release`, `docs`, `consumer-refresh`

Unrecognized top-level sections also trigger a warning.

The `version-file` key is removed from the known set. Any repo still
carrying it will see a warning on the next tool run, prompting
cleanup without requiring a coordinated rollout.

## Init wizard changes

`vrg-github-repo-init` (`src/vergil_tooling/bin/vrg_github_repo_init.py`
+ `src/vergil_tooling/lib/repo_init.py`) is updated.

**Prompting:** The initial version question (default: `0.1.0`) is
collected during the existing interactive question-gathering phase,
alongside repository-type, primary-language, etc. All user input is
gathered before any automated work begins. Once the automation
starts, the user's next interaction is success or failure — no
mid-process questions.

**Sequencing (all automated, no prompts):**

1. Bootstrap main branch with vergil.toml, CI workflows, docs, etc.
   (existing behavior).
2. Switch to develop.
3. Write `VERSION` with the version collected earlier.
4. Commit on develop.

Main has no VERSION file. The version-divergence CI check skips the
main-side comparison. The first PR passes cleanly.

## Rollout

**vergil-tooling:** Fix the stale VERSION file (currently `2.0.4`,
should match pyproject.toml) as part of this implementation. Same
repo, same PR.

**All other repos:** `vrg-version`'s new sync enforcement surfaces
mismatches when each repo is next worked on. The fix is always
trivial — update VERSION to match the language file. No coordinated
rollout needed.

## Files changed

All changes are in vergil-tooling:

| File | Change |
|------|--------|
| `src/vergil_tooling/lib/version.py` | `show` reads VERSION, cross-checks language file, hard error on mismatch. `bump` writes both. Remove `version-file` override handling. |
| `src/vergil_tooling/lib/config.py` | Add unrecognized-key warnings. Remove `version-file` from known schema. |
| `src/vergil_tooling/lib/repo_init.py` | Add initial version prompt to question-gathering phase. Write VERSION on develop after main is bootstrapped. |
| `VERSION` | Update from `2.0.4` to match pyproject.toml. |
| `tests/` | Cover sync enforcement, dual-write bump, unrecognized-key warnings, init wizard VERSION creation. |
