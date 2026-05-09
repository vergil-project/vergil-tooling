# Pushback Review: 610 repo settings coverage design

**Date:** 2026-05-08
**Spec:** `docs/specs/2026-05-08-610-repo-settings-coverage-design.md`
**Commit:** a5b2798806ca96b4db245da6b0da6d48d979395e

## Source Control Conflicts

None — no conflicts with recent changes. The github_config module
has not been modified in the last 50 commits.

## Issues Reviewed

### [1] `has_pages` is read-only in the REST API

- **Category:** Feasibility
- **Severity:** Serious
- **Issue:** `has_pages` is a response-only field on `PATCH
  /repos/{owner}/{repo}` — it reflects whether GitHub Pages is
  configured, not a toggleable setting. Including it in
  `DesiredRepoSettings` would create unfixable diff noise: the diff
  detects drift, but the PATCH silently ignores the field. Enabling
  Pages requires a separate `POST /repos/{owner}/{repo}/pages` call
  with a source configuration.
- **Resolution:** Deferred to a future iteration. A docs/publishing
  refactor is imminent and will likely introduce tooling that couples
  with Pages enablement. Revisiting after that work lands avoids
  doing it twice.

### [2] `homepage` is coupled to Pages configuration

- **Category:** Feasibility
- **Severity:** Moderate
- **Issue:** `homepage` was derived from the repo name
  (`https://{owner}.github.io/{name}/`), but this only makes sense
  for repos with GitHub Pages configured. For repos without Pages,
  the homepage would point to a 404. Additionally, applying a
  derived homepage would overwrite any custom homepage already set on
  a repo.
- **Resolution:** Deferred alongside `has_pages` — both are
  Pages-coupled and benefit from the upcoming docs/publishing
  refactor. Removing `homepage` also eliminates the need for a
  `repo` parameter on `desired_repo_settings()`.

### [3] Visibility plumbing is underspecified

- **Category:** Omission
- **Severity:** Moderate
- **Issue:** The spec described `visibility` as already available at
  the CLI level, but `fetch_actual_state()` consumes the raw API
  response internally and returns only `DesiredState`. The
  `visibility` field is present in the API response but discarded —
  not surfaced to the caller. The spec needed to specify how
  `visibility` flows from `fetch_actual_state()` to
  `compute_desired_state()`.
- **Resolution:** `fetch_actual_state()` returns a wrapper (dataclass
  or named tuple) that includes both `DesiredState` and
  `visibility: str`. Avoids a redundant API call.

### [4] `has_discussions` — unverified REST API writability

- **Category:** Feasibility
- **Severity:** Moderate
- **Issue:** Same class of concern as `has_pages`. The
  `has_discussions` field appears in the repository response, but it
  is unclear whether the REST PATCH endpoint accepts it. GitHub
  Discussions were added later and API write support may only exist
  via GraphQL.
- **Resolution:** Deferred. Verify writability before including in a
  future iteration.

### [5] `web_commit_signoff_required` fleet-wide friction

- **Category:** Ambiguity
- **Severity:** Minor
- **Issue:** Enabling this across the fleet adds a signoff prompt to
  all web-based commits. The spec noted the friction but did not
  state the motivation.
- **Resolution:** Confirmed intentional. The friction is by design —
  pushes contributors toward the `st-commit` workflow.

## Unresolved Issues

None — all issues were addressed.

## Summary

- **Issues found:** 5
- **Issues resolved:** 5
- **Unresolved:** 0
- **Spec status:** Ready for implementation (updated in place)
- **Scope change:** 11 new fields reduced to 8. Three fields
  (`has_pages`, `homepage`, `has_discussions`) deferred to future
  iterations.
