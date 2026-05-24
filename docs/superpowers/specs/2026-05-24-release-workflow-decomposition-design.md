# Release Workflow Decomposition

**Date:** 2026-05-24
**Status:** Draft
**Issue:** #1069

## Motivation

The current `vrg-release` is a monolith that couples Git Flow branch
mechanics, changelog generation, version bumping, rolling-tag
promotion, and CD verification into a single orchestrator. This
coupling creates several problems:

1. **Non-standard Git Flow implementation.** The release branch merges
   `origin/main` with `-X ours` to reconcile diverged histories. This
   is a workaround — standard Git Flow relies on a reliable back-merge
   after each release so the histories never diverge. The `-X ours`
   strategy silently discards conflicting content from main, which
   means a hotfix merged to main could be quietly lost on the next
   release.

2. **Tightly coupled version bumping.** The version bump is delegated
   to a CI action (`version-bump-pr` in vergil-actions) and polled
   inline. Version bumping is an independent concern — the next
   version might be patch, minor, major, or a pre-release identifier,
   and that decision should not be baked into the release pipeline.

3. **Rolling-tag promotion is implicit.** The `vX.Y` rolling tag is
   force-updated inside the `cd-release.yml` workflow in
   vergil-actions. There is no way to release `vX.Y.Z` without
   promoting it to the default `vX.Y`, and no standalone tool to
   manage rolling tags.

4. **CD verification is incomplete.** The orchestrator verifies CD
   workflows on main but not on develop. Workflow failures after the
   back-merge+bump PR merges to develop go undetected.

5. **Workflow expectations are hardcoded.** Which CD jobs to watch is
   determined by `vergil.toml` flags (`publish.release`,
   `publish.docs`), duplicating information already expressed in the
   workflow YAML files themselves.

## Design Principles

Inherited from the original `vrg-release` design:

**Fully non-interactive.** No prompts, no confirmation gates. Each
tool runs start to finish. On failure, it stops immediately and
reports diagnostics.

**Stop and report — never stop and fix.** Every failure is surfaced,
never worked around.

**Trust the hard gates.** The existing retry layer in `lib/github.py`
handles transient API failures. Phase-level retry is not needed.

New principles for the decomposition:

**One tool, one domain.** Each independent tool owns a single concern
(versioning, changelog, tag promotion) and can be used standalone
outside of the release workflow.

**Standard Git Flow mechanics.** The release orchestrator follows the
canonical Git Flow pattern: branch from develop, merge to main, tag,
back-merge main to develop. No `-X ours`, no history-reconciliation
workarounds. Conflicts surface immediately.

**Branch owner commits.** Library functions and CLI tools generate
files but never commit. The tool that creates a branch for a task
owns the commits on that branch, writing messages in the context of
the operation it is performing. This matches the existing codebase
pattern (`prepare.py` creates the release branch and commits;
`repo_init.py` creates scaffold branches and commits) and keeps
commit authorship unambiguous.

**Known expectations over dynamic discovery.** CD verification
watches a known, stable set of job names — `docs` (both branches)
and `release` (main only). These expectations are constants in the
orchestrator, not derived from parsing workflow YAML conditionals.
Parsing `if:` expressions in Python is fragile and a losing battle
as conditions evolve; the known set changes rarely and a one-line
update is obvious when it does.

**Human tools vs. agent tools.** Tools that perform privileged
operations — force-updating tags, pushing to protected branches,
merging to main — are human tools. They run with human credentials
on the host. AI agents in the VM do not have the credentials to
execute them; failures surface naturally at the first unauthorized
operation. `vrg-release` and `vrg-promote` are human tools.
`vrg-version` and `vrg-changelog` can be used by either.

## Architecture

### Tool Decomposition

```
vrg-release [--no-promote] [minor | major]
│
├─ 1. vrg-version show            read current version
├─ 2. vrg-changelog               generate CHANGELOG.md + release notes
├─ 3. create PR to main           wait for CI, merge
├─ 4. verify CD on main           watch known jobs (docs, release)
├─ 5. back-merge + bump PR        single PR against develop:
│     ├─ merge main into branch   standard Git Flow, no -X ours
│     └─ vrg-version bump         commit on top of the merge
├─ 6. verify CD on develop        watch known jobs (docs)
├─ 7. vrg-promote                 update vX.Y tag (unless --no-promote)
└─ 8. finalize                    close tracking issue, cleanup
```

### Module Layout

```
src/vergil_tooling/
  bin/
    vrg_release.py                existing — refactored to thin orchestrator
    vrg_version.py                existing — extended with part argument
    vrg_changelog.py              new CLI entry point
    vrg_promote.py                new CLI entry point
  lib/
    version.py                    existing — extended with minor/major bump
    changelog.py                  new — git-cliff wrapper
    promote.py                    new — rolling-tag management
    release/
      orchestrator.py             existing — refactored to call independent tools
      preflight.py                existing — simplified (version override validation only)
      prepare.py                  existing — simplified (no -X ours merge)
      merge.py                    existing — unchanged
      bump.py                     existing — rewritten to use lib/version.py
      confirm.py                  existing — rewritten to use known job expectations
      finalize.py                 existing — unchanged
      tracking.py                 existing — unchanged
      context.py                  existing — updated with new phase fields
      handoff.py                  existing — unchanged
```

## Independent Tools

### vrg-version

Version management across all repository types. This tool extends
the existing `lib/version.py` module, which already provides
`show()` and `bump()` (patch-only). The extension adds minor/major
bump support and a CLI entry point.

**Version discovery:** Every managed repository has a plain-text
`VERSION` file at the repo root. This is the **canonical** version
source — language-independent, trivially readable. The
language-specific manifest (`pyproject.toml`, `Cargo.toml`, etc.)
is a secondary copy. `vrg-version show` reads `VERSION` and
cross-checks the manifest; `vrg-version bump` writes both.

**CLI interface:**

```
vrg-version show                  print current version to stdout
vrg-version bump                  bump patch (default)
vrg-version bump minor            bump to next minor
vrg-version bump major            bump to next major
```

Pre-release version support (`rc`, `beta`, `dev`) is a planned
future extension and is not part of this design.

**Entry point:**

```toml
vrg-version = "vergil_tooling.bin.vrg_version:main"
```

**Behavior:**

- `show` reads the version from the `VERSION` file and cross-checks
  against the language-specific manifest (determined by
  `vergil.toml` `primary_language` or auto-detection). Supports
  `pyproject.toml`, `package.json`, `pom.xml`, `Cargo.toml`,
  `version.rb`, `version.go`, and `plugin.json`. Raises
  `VersionSyncError` if the two sources disagree. Also accepts an
  optional `--ref` argument to read the version at a specific git
  ref.

- `bump` modifies the version in the `VERSION` file, updates the
  language-specific manifest to match, and regenerates the lockfile
  if applicable (e.g., `uv lock` for Python). Neither the CLI nor
  the library function commits — the caller (orchestrator or human
  via `vrg-commit`) owns the commit.

- Exit code 0 on success, 1 on failure. `show` outputs only the
  version string (no decorations) for scripting use.

**Library:**

`lib/version.py` already exposes `show()` and `bump()`. This
design extends `bump()` with an optional `part` parameter:

```python
def show(repo_root: Path, *, ref: str | None = None) -> str: ...
def show_major_minor(repo_root: Path, *, ref: str | None = None) -> str: ...
def bump(repo_root: Path, part: str = "patch") -> str: ...
```

The `part` parameter accepts `"patch"` (default, backward
compatible), `"minor"`, or `"major"`.

### vrg-changelog

Changelog and per-release notes generation via git-cliff.

**CLI interface:**

```
vrg-changelog                     generate CHANGELOG.md + releases/vX.Y.Z.md
vrg-changelog --changelog-only    generate only CHANGELOG.md
vrg-changelog --notes-only        generate only releases/vX.Y.Z.md
```

**Entry point:**

```toml
vrg-changelog = "vergil_tooling.bin.vrg_changelog:main"
```

**Behavior:**

- Reads the current version via `lib/version.py` to determine the
  boundary tag (`develop-vX.Y.Z`).

- Generates `CHANGELOG.md` using the project's `cliff.toml`
  configuration (shipped in `vergil_tooling.configs`).

- Generates `releases/vX.Y.Z.md` using the `cliff-release-notes.toml`
  configuration.

- Normalizes trailing newlines on generated files.

- Neither the CLI nor the library functions commit — the caller
  (orchestrator or human via `vrg-commit`) owns the commit.

- Standalone use case: regenerating the changelog after retroactive
  tag corrections or cliff config changes. The human commits
  afterward via `vrg-commit`.

**Library:**

`lib/changelog.py` exposes:

```python
def generate_changelog(repo_root: Path, version: str) -> None: ...
def generate_release_notes(repo_root: Path, version: str) -> Path: ...
```

### vrg-promote

Rolling-tag management — extracted from the `cd-release.yml` workflow
in vergil-actions.

**CLI interface:**

```
vrg-promote                       promote current release to vX.Y
vrg-promote v2.0.34               promote a specific version
vrg-promote --dry-run             show what would happen
```

**Entry point:**

```toml
vrg-promote = "vergil_tooling.bin.vrg_promote:main"
```

**Behavior:**

- Parses the version to extract the `vX.Y` prefix.

- Force-updates the `vX.Y` tag to point at the same commit as the
  `vX.Y.Z` tag. Pushes the tag to origin.

- Uses `subprocess.run(["git", ...])` directly for tag operations
  — not `lib/git.py` or `vrg-git`. Tag force-update and force-push
  are privileged operations that only human credentials support.

- `--dry-run` prints the tag operation without executing it.

- Standalone use case: promoting a specific older patch after testing,
  or re-promoting after a reverted release.

- **This is a human tool.** It requires credentials that can
  force-push tags. AI agents in the VM cannot execute it.

**Library:**

`lib/promote.py` exposes:

```python
def promote(version: str, *, dry_run: bool = False) -> None: ...
```

**vergil-actions change:** The inline rolling-tag logic in
`cd-release.yml` is removed. Repositories that want automatic
promotion on every release add `vrg-promote` to their CD workflow
or let `vrg-release` handle it via the orchestrator.

## Orchestrator: vrg-release

### CLI Interface

```
vrg-release                       release current version (patch bump after)
vrg-release minor                 bump to next minor before releasing
vrg-release major                 bump to next major before releasing
vrg-release --no-promote          skip rolling-tag promotion
```

**Entry point:** unchanged.

```toml
vrg-release = "vergil_tooling.bin.vrg_release:main"
```

### Phase Sequence

#### Phase 0: Preflight

Unchanged from current design, plus:

- If `minor` or `major` argument is provided, preflight validates
  the argument but **does not bump or commit**. The version bump
  happens on the release branch in Phase 1 (after branching).
  This eliminates the current bug where `_apply_version_override`
  commits directly to develop.

**Removed:** The current `_apply_version_override` function in
preflight commits a version bump directly to the local develop
branch. This violates the branch protection model and the
"branch owner commits" principle. The override argument is stored
in `ReleaseContext` and acted on by Phase 1.

#### Phase 1: Prepare

Simplified from current design:

1. Create tracking issue.
2. Create `release/X.Y.Z` branch from develop.
3. If `minor` or `major` was requested, call `vrg-version bump <part>`
   on the release branch to set the release version.
4. Call `vrg-changelog` to generate changelog and release notes.
5. Push branch. Create PR to main.
6. Return to develop.

**Removed:** The `-X ours` merge of `origin/main` into the release
branch. This was a workaround for diverged histories. With a reliable
back-merge after each release, the histories do not diverge and this
step is unnecessary.

If the merge to main later conflicts because the histories have
diverged (e.g., due to a manual hotfix that was not back-merged),
the conflict surfaces immediately in Phase 2 rather than being
silently auto-resolved. This is the correct behavior — the human
must reconcile the conflict.

#### Phase 2: Merge Release

Unchanged. Wait for CI checks on the release PR, handle behind-base
updates, merge to main.

#### Phase 3: Verify CD on Main

Rewritten to use known job expectations:

1. Wait for the CD workflow run triggered by the merge to main.
2. Verify expected jobs succeeded: `docs` and `release`.
3. Verify release artifacts: tag exists, GitHub Release created,
   develop boundary tag exists.

The expected job names are constants in the orchestrator, not
derived from parsing workflow YAML. Replaces the current
`confirm.py` which used `vergil.toml` flags.

#### Phase 4: Back-merge and Bump

Replaces the current `bump.py` which polled for a CI-created PR:

1. Fetch main.
2. Create branch from main (e.g., `release/post-X.Y.Z`).
3. Call `vrg-version bump` to bump the patch version. This creates
   a commit on top of the branch.
4. Create PR targeting develop.
5. Wait for CI checks, merge.

This is a single PR that carries both the back-merge (main's history
flowing into develop, including changelog and release notes) and the
version bump (develop moves to the next version).

**Branch naming:** `release/post-X.Y.Z` (where X.Y.Z is the version
just released). This distinguishes it from the release branch
(`release/X.Y.Z`) and the old bump branch naming
(`release/bump-version-X.Y.Z`).

**Merge strategy:** Standard merge, no `-X ours`. If conflicts arise
(e.g., from an un-back-merged hotfix), `vrg-release` stops and
reports the conflict. The human resolves it.

#### Phase 5: Verify CD on Develop

New phase. Same mechanics as Phase 3, but for develop:

1. Wait for the CD workflow run triggered by the merge to develop.
2. Verify expected jobs succeeded: `docs`.

When dev-release publication is added later, `release` (or a
dedicated publish job) will be added to the develop expectations.

This closes the gap where develop-side CD failures (e.g., docs
deployment) went undetected.

#### Phase 6: Promote

New phase, conditional on `--no-promote`:

1. Call `vrg-promote` to force-update the `vX.Y` rolling tag.
2. Push the tag to origin.

Skipped when `--no-promote` is passed.

#### Phase 7: Finalize

Unchanged. Close tracking issue with summary, run
`vrg-finalize-repo`, display consumer-refresh template.

### What Changes in vergil-actions

1. **`version-bump-pr` action**: No longer needed. The bump PR is
   created by `vrg-release` itself. The action can be deprecated and
   removed.

2. **Rolling-tag logic in `cd-release.yml`**: Extracted to
   `vrg-promote`. The workflow no longer force-updates the `vX.Y`
   tag — that responsibility moves to the orchestrator (or standalone
   `vrg-promote` invocation).

3. **`cd-release.yml` simplification**: With version-bump-pr and
   rolling-tag logic removed, the workflow focuses solely on tagging
   and GitHub Release creation.

### What Changes in vergil.toml

The `publish.release` and `publish.docs` flags are no longer used
for CD verification — expected jobs are now known constants. These
flags may still be useful for other purposes (e.g., controlling
whether `vrg-release` creates a GitHub Release at all), but they
no longer drive workflow watching.

## Hotfix Flow

This design makes hotfixes straightforward by following standard
Git Flow:

1. Branch from main: `hotfix/X.Y.Z`
2. Fix the bug, commit.
3. Merge to main (via PR with CI gate).
4. Back-merge main to develop (via PR with CI gate) — standard
   merge, no `-X ours`.

The back-merge in step 4 uses the same mechanics as the release
back-merge. Because the histories are kept in sync by the reliable
back-merge after each release, hotfix merges do not encounter
diverged-history conflicts.

A `vrg-hotfix` tool is not part of this design but could be added
later following the same decomposition principles.

## Migration Path

The decomposition is implemented in two phases: independent tool
work (steps 1–3) followed by a coordinated cutover (step 4).

1. **Extend `lib/version.py` + update `vrg-version` CLI** — add
   `part` parameter to existing `bump()`, update CLI entry point.
   No conflicts with existing callers (default is `"patch"`).
2. **`lib/changelog.py` + `vrg-changelog`** — extract from
   `prepare.py`, which becomes a caller.
3. **`lib/promote.py` + `vrg-promote`** — new, standalone tool.

Steps 1–3 are independently releasable and do not affect the
current release workflow.

4. **Big-bang cutover** — ship simultaneously:
   - Refactor `vrg-release` orchestrator: rewire phases to call
     the new tools, remove `-X ours`, replace bump-PR polling with
     orchestrator-driven back-merge+bump, add develop CD
     verification, add `--no-promote` flag.
   - Update vergil-actions: remove `version-bump-pr` action from
     `cd-release.yml`, remove inline rolling-tag logic.
   - All managed repos receive the vergil-actions update and the
     new vergil-tooling version together.

The big-bang cutover is necessary because the old
`version-bump-pr` action and the new orchestrator-driven
back-merge+bump cannot coexist — both would attempt to create
bump PRs after a release.
