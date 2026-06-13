# Claude marketplace ref as a version-derived reference

**Issue:** #1654
**Date:** 2026-06-13

## Table of Contents

- [Problem](#problem)
- [Goals and non-goals](#goals-and-non-goals)
- [The invariant](#the-invariant)
- [Overview](#overview)
- [Architecture](#architecture)
  - [Shared derivation helpers](#shared-derivation-helpers)
  - [Write side: VergilUpdater](#write-side-vergilupdater)
  - [Audit side: repo_config](#audit-side-repo_config)
  - [Self-detection of the marketplace source repo](#self-detection-of-the-marketplace-source-repo)
  - [Seeding new repos: repo_init](#seeding-new-repos-repo_init)
- [The moving-tag refresh gate](#the-moving-tag-refresh-gate)
- [Fleet sweep](#fleet-sweep)
- [Error handling](#error-handling)
- [Testing](#testing)
- [Open decision points](#open-decision-points)
- [Work breakdown](#work-breakdown)

## Problem

Each managed repo declares the vergil tooling line it tracks in `vergil.toml`:

```toml
[dependencies]
vergil = "v2.1"
```

That value is already treated as canonical by `vrg-update-deps`: the
`VergilUpdater` rewrites every reusable-workflow pin (`uses: vergil-*/...@vX.Y`)
to match it, in both bump and normalize modes. The Claude Code plugin is
delivered through a marketplace declared in each repo's `.claude/settings.json`,
and that declaration carries its own ref — but the ref was never wired into the
version-derivation machinery.

A sweep of every repo carrying a `vergil.toml` found that **all of them** declare
the marketplace with no `ref`:

```json
"extraKnownMarketplaces": {
  "vergil-marketplace": {
    "source": { "source": "github", "repo": "vergil-project/vergil-claude-plugin" }
  }
}
```

With no `ref`, Claude Code resolves the marketplace at the plugin repo's default
branch (`develop`) instead of the released line each repo's `vergil.toml` pins.
The plugin README documents `"ref": "main"` as the intended behavior, but the
scaffolding template (`src/vergil_tooling/data/claude_settings.json`) never
carried a ref, so the documented intent and the generated reality diverged. The
practical symptom is agents loading an unintended (and, on persistent installs,
stale) plugin version rather than the one the repo declares.

Two of the three version-assertion points (`vergil.toml`, workflow pins) were
kept in sync. The Claude marketplace ref was the unguarded third.

## Goals and non-goals

**Goals**

- Make the Claude marketplace `source.ref` a version-derived reference, written
  by the same `update_deps` machinery that maintains the workflow pins.
- Assert the correctness of every version-derived ref (workflow pins **and** the
  Claude ref) against `vergil.toml` in the `repo_config` audit.
- Exempt the marketplace source repo itself so plugin development tracks
  `develop`.
- Bring every repo carrying a `vergil.toml` into compliance.

**Non-goals**

- Changing how the workflow pins are derived or rewritten today (we extend the
  pattern, we do not alter it).
- Changing the plugin's own release process or the `vrg-promote` moving-tag
  mechanism.
- Freezing the plugin repo's own settings on release (see
  [Open decision points](#open-decision-points) — explicitly out of scope; the
  plugin repo stays on `develop` on every branch).

## The invariant

`vergil.toml [dependencies] vergil` is canonical. Every derived ref must equal
the version computed from it:

| Derived ref | Location | Status |
| --- | --- | --- |
| Reusable-workflow pins | `.github/workflows/*.yml` `uses: vergil-*/...@vX.Y` | existing |
| Claude marketplace ref | `.claude/settings.json` `extraKnownMarketplaces.vergil-marketplace.source.ref` | new |

**Exemption.** The marketplace source repo (`vergil-claude-plugin`) is hardwired
to `ref: "develop"`. It is the single repo exempt from the derived value, so that
plugin development dogfoods the latest in-progress plugin. This mirrors the
self-referential dogfooding pattern in `vergil-actions`, which uses relative refs
during development and a frozen tag for consumers.

## Overview

The change threads one additional derived ref through machinery that already
exists:

1. A small set of **shared derivation helpers** answers "given a repo, what is
   the expected version, and what should each derived ref be?" — including the
   self-repo exemption.
2. The **write side** (`VergilUpdater`) gains a JSON-aware
   `normalize_claude_ref`, invoked alongside the existing `normalize_refs`.
3. The **audit side** (`repo_config`) consumes the same helpers to assert every
   derived ref matches, emitting a `DiffItem` per mismatch.
4. **`repo_init`** seeds the Claude ref from the chosen vergil version, the same
   way it seeds workflow pins.
5. The **fleet sweep** is `vrg-update-deps` in normalize mode, run per repo.

## Architecture

### Shared derivation helpers

The knowledge of "where the refs live and what they should be" must be owned in
one place so the writer and the auditor cannot disagree. Today
`read_source_version`, `format_version`, the workflow-ref regex (`_REF_RE`), and
the source-line regex (`_SOURCE_RE`) live in
`update_deps/updaters/vergil_eco.py`. Because `repo_config` must not import from
`update_deps`, lift the reusable pieces into a neutral module (working name
`lib/vergil_refs.py`) that both sides import:

- `read_source_version(base) -> str` — the canonical `vX.Y` from `vergil.toml`.
- `format_version(raw) -> str` — normalize `2.1` / `v2.1` to `vX.Y`.
- `is_marketplace_source_repo(base) -> bool` — self-detection (see below).
- `expected_claude_ref(base) -> str` — `"develop"` for the self repo, else the
  derived `vX.Y`.
- `_REF_RE`, `_SOURCE_RE` — retained for the workflow-pin path.

`vergil_eco.py` re-imports from this module so its public surface is unchanged.

### Write side: VergilUpdater

Add `normalize_claude_ref(base, target, *, is_self) -> Path | None` to the write
path. Unlike the YAML workflow files (where regex substitution is safe), the
settings file is JSON and must be edited structurally:

1. Read and parse `.claude/settings.json` (skip cleanly if absent).
2. Navigate to `extraKnownMarketplaces.vergil-marketplace.source`.
3. Set `ref` to `"develop"` when `is_self`, else `target`.
4. Write back only if changed, preserving key order and formatting as closely as
   the existing settings conventions allow.

Wire it into both branches of `VergilUpdater.apply()`:

- **bump** — after `set_source_version` + `normalize_refs(base, target)`, call
  `normalize_claude_ref(base, target, is_self=...)`.
- **normalize** — after `normalize_refs(base, read_source_version(base))`, call
  `normalize_claude_ref` with the same target.

`changed` and the result summary account for the settings file alongside the
workflow files.

### Audit side: repo_config

`repo_config` already validates that `.claude/settings.json` contains at least
the template's `extraKnownMarketplaces` and `enabledPlugins` entries. Extend that
check so the **correctness of every version-derived ref** is asserted against
`vergil.toml`:

- The Claude marketplace `source.ref` must equal `expected_claude_ref(base)`.
- Each reusable-workflow pin must equal the derived `vX.Y` (the pre-existing gap
  — workflow pins are written on bump but never audited — is closed here, per the
  unified-guard decision).

Each mismatch is reported as a `DiffItem(field, expected, actual)` aggregated
into the existing `ConfigDiff`, so it surfaces through the normal audit/diff
output with no new reporting surface.

### Self-detection of the marketplace source repo

Detect the plugin repo by an intrinsic, filesystem-local signal: the presence of
`.claude-plugin/marketplace.json`. Only the marketplace source repo ships that
file. This needs no git calls and no per-repo configuration that could rot, and
there is exactly one such repo. `is_marketplace_source_repo(base)` encapsulates
the predicate; both the write side and the audit side consume it.

### Seeding new repos: repo_init

`repo_init` already stamps workflow pins from `ctx.vergil_version`. Because the
Claude ref is now derived rather than literal:

- The `claude_settings.json` template drops to a no-ref form (it cannot hardcode
  a version).
- After copying the template, `repo_init` injects `source.ref` from
  `ctx.vergil_version` — identical in spirit to how it writes the workflow pins.
  A repo initialized as the marketplace source repo receives `develop`.

## The moving-tag refresh gate

The derived ref is the moving `vX.Y` tag maintained by `vrg-promote`, matching
the workflow pins exactly. GitHub Actions resolves tags fresh on every run, so a
force-moved `vX.Y` works there; Claude Code keeps a **persistent** marketplace
clone, and git does not advance an already-fetched tag on a plain fetch.

**Gate (blocking on rollout):** verify that `claude plugin marketplace update`
picks up a force-moved `vX.Y` tag on a persistent install. If it does, ship as
designed. If it does not, the derived value becomes a `release/X.Y` **branch**
(branches fetch cleanly), and `expected_claude_ref` / the writer derive that form
instead — every other part of this design is unchanged. The smoke test must
complete before the fleet sweep.

## Fleet sweep

Because the write logic lands in the normalizer, the sweep is simply
`vrg-update-deps` in normalize mode run per repo:

- Each repo reads its own `vergil.toml` and writes the matching Claude ref (and
  corrects any drifted workflow pins in passing).
- Repos pinned to `v2.0` (e.g. the mq-rest-admin set) get `v2.0`; repos on `v2.1`
  get `v2.1`; the plugin repo gets `develop`.
- The change rides the normal per-repo PR flow; no bespoke sweep script.

## Error handling

- **Missing `vergil.toml` `[dependencies] vergil`** — `read_source_version`
  already raises `UpdateDepsError`; the audit reports it as a config defect
  rather than crashing.
- **Missing or malformed `.claude/settings.json`** — the writer skips cleanly
  (nothing to normalize); the audit reports the absence the same way it reports a
  missing template entry today.
- **Marketplace entry present but `source` missing/renamed** — treated as a
  reportable config defect, not a silent pass.
- No swallowed exceptions: a parse failure on the settings file surfaces as an
  explicit error, consistent with the project's no-silent-failure policy.

## Testing

- **Unit (helpers):** `expected_claude_ref` returns `develop` for the self repo
  and the derived `vX.Y` otherwise; `is_marketplace_source_repo` keys only on
  `.claude-plugin/marketplace.json`.
- **Unit (writer):** `normalize_claude_ref` sets the ref, is idempotent, edits
  JSON structurally (preserves sibling keys), and no-ops when absent.
- **Unit (updater):** bump and normalize both update the Claude ref alongside
  workflow pins; the self repo is driven to `develop`.
- **Unit (audit):** a drifted Claude ref and a drifted workflow pin each emit a
  `DiffItem`; a compliant repo emits none; the self repo passes only on
  `develop`.
- **Init:** a freshly initialized repo carries the seeded ref; a self-repo init
  carries `develop`.
- **Fixture coverage:** at least one consuming repo on `v2.0`, one on `v2.1`, and
  the self repo.

## Open decision points

1. **Unified guard scope.** This spec audits workflow pins **and** the Claude ref
   (closing the pre-existing unguarded-workflow-pin gap). Confirm this is desired
   versus auditing only the Claude ref.
2. **Moving tag vs. release branch.** Resolved by the
   [refresh gate](#the-moving-tag-refresh-gate); the spec assumes the moving tag
   and names the branch fallback.
3. **Plugin self-freeze.** Out of scope by decision: the plugin repo stays on
   `develop` on all branches; we do not freeze its own settings to `vX.Y` on
   release, because nothing consumes the plugin repo's own settings file.

## Work breakdown

1. Extract shared derivation helpers into `lib/vergil_refs.py`; re-point
   `vergil_eco.py` imports.
2. Add `is_marketplace_source_repo` and `expected_claude_ref`.
3. Implement `normalize_claude_ref` and wire it into both `VergilUpdater`
   branches.
4. Extend the `repo_config` audit to assert all derived refs; add `DiffItem`s.
5. Update the `claude_settings.json` template (drop the literal) and `repo_init`
   seeding.
6. Tests per the [Testing](#testing) section.
7. Run the [moving-tag refresh gate](#the-moving-tag-refresh-gate).
8. Fleet sweep via `vrg-update-deps` normalize, per repo, through the PR flow.
