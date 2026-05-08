# Expand st-github-config repo settings coverage

**Issue:** [#610](https://github.com/wphillipmoore/standard-tooling/issues/610)
**Date:** 2026-05-08

## Context

`st-github-config` manages 9 of the ~24 PATCH-able fields on the
`repos/{owner}/{repo}` endpoint. An audit of all non-archived repos
revealed 11 additional settings worth managing — some already
consistent across the fleet (merge commit formats), others drifting
(e.g. `allow_update_branch`). Of those 11, 8 are addressed in this
iteration; 3 are deferred (see below).

Issue #610 originally asked for managing `actions/permissions/selected-actions`,
but that endpoint is already managed. The `swatinem/*` pattern gap
that prompted the issue has been fixed. The scope expanded to a full
repo-settings audit.

## Decisions

### New settings to manage

All hardcoded unless noted otherwise.

| Setting | Value | Notes |
|---|---|---|
| `allow_forking` | `true` if public, `false` if private | Derived from repo visibility |
| `allow_update_branch` | `true` | Enables "Update branch" button on PRs |
| `has_downloads` | `false` | Legacy feature, no practical effect |
| `merge_commit_title` | `"MERGE_MESSAGE"` | Already consistent across fleet |
| `merge_commit_message` | `"PR_TITLE"` | Already consistent across fleet |
| `squash_merge_commit_title` | `"COMMIT_OR_PR_TITLE"` | Already consistent across fleet |
| `squash_merge_commit_message` | `"COMMIT_MESSAGES"` | Already consistent across fleet |
| `web_commit_signoff_required` | `true` | Intentional friction — pushes toward st-commit workflow |

### Settings deferred to a future iteration

| Setting | Reason |
|---|---|
| `has_pages` | Read-only in REST API (reflects Pages configuration state). Revisit after docs/publishing refactor — enabling Pages requires a separate `POST /repos/{owner}/{repo}/pages` call and may couple with standardized docs tooling. |
| `homepage` | Derived from repo name, but coupled to Pages configuration. Revisit alongside `has_pages` after docs/publishing refactor. |
| `has_discussions` | Needs verification that the REST PATCH endpoint accepts this field (may require GraphQL). Verify and revisit. |

### Settings left unmanaged

| Setting | Reason |
|---|---|
| `visibility` | Controlled per-repo at creation time |
| `is_template` | Not using GitHub templates |
| `description` | Inherently per-repo; will standardize later |
| `topics` | Inherently per-repo; not in use today |

## Design

### Dataclass: `DesiredRepoSettings`

Add 8 fields to the existing dataclass:

```python
@dataclass
class DesiredRepoSettings:
    # existing
    default_branch: str
    allow_auto_merge: bool
    delete_branch_on_merge: bool
    allow_merge_commit: bool
    allow_squash_merge: bool
    allow_rebase_merge: bool
    has_issues: bool
    has_projects: bool
    has_wiki: bool
    # new
    allow_forking: bool
    allow_update_branch: bool
    has_downloads: bool
    merge_commit_title: str
    merge_commit_message: str
    squash_merge_commit_title: str
    squash_merge_commit_message: str
    web_commit_signoff_required: bool
```

### Derivation: `desired_repo_settings(visibility)`

The function signature changes from no-args to
`desired_repo_settings(visibility: str)` because `allow_forking`
depends on visibility (public vs private).

`compute_desired_state()` gains a `visibility` parameter and threads
it through.

### Plumbing: `fetch_actual_state()` returns visibility

`fetch_actual_state()` currently returns `DesiredState`. It changes
to return a wrapper (e.g. a dataclass or named tuple) that includes
both the `DesiredState` and `visibility: str` extracted from the
same `repos/{repo}` API response. This avoids a redundant API call
— the visibility is already present in the response that
`fetch_actual_state()` reads.

### Fetch: `fetch_actual_state()`

Extend the existing `repo_data` extraction to read all 8 new fields
(plus `visibility`) from the `repos/{repo}` response. No new API
calls — these fields are already present in the response payload.

### Diff

No changes needed. `_diff_dataclass` iterates `dataclasses.fields()`
automatically, so new fields on the dataclass are included in the
diff without code changes.

### Apply: `_apply_repo_settings()`

Add the 8 new fields to the existing single PATCH body on
`repos/{repo}`. No new API calls.

### Tests

Extend existing test files (`test_github_config_lib.py`,
`test_github_config_cli.py`):

1. Update all `DesiredRepoSettings` fixtures with the new fields.
2. Update mock `repo_data` dicts with the new API keys.
3. Update callers of `compute_desired_state()` for the new signature.
4. Update `fetch_actual_state()` tests for the new return type
   (wrapper including `visibility`).
5. Add test cases for:
   - `allow_forking` is `True` when visibility is `"public"`,
     `False` when `"private"`.
   - Drift detection works for the new fields.
