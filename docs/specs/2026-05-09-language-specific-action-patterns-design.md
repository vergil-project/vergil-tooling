# Language-Specific Action Patterns

**Issue:** [#613](https://github.com/wphillipmoore/standard-tooling/issues/613)
**Date:** 2026-05-09

## Problem

`_ALLOWED_ACTION_PATTERNS` is a flat global list applied identically to
every managed repo. Every repo gets patterns it doesn't need (e.g., Go
repos get `ruby/*`, `pypa/*`) and language-specific patterns like
`swatinem/*` must either be added globally or managed manually.

When `st-github-config apply` runs, it strips any manually-added
patterns that aren't in the global list — which is how `swatinem/*` was
removed from `mq-rest-admin-rust` during the v1.5 CI workflow rollout.

## Design

### Data structure

Replace the single `_ALLOWED_ACTION_PATTERNS` list with two constants:

```python
_BASE_ACTION_PATTERNS = [
    "actions/*",
    "docker/*",
    "github/*",
    "wphillipmoore/*",
]

_LANGUAGE_ACTION_PATTERNS: dict[str, list[str]] = {
    "python": ["astral-sh/*", "pypa/*"],
    "ruby": ["ruby/*"],
    "rust": ["actions-rust-lang/*", "swatinem/*"],
}
```

- Base patterns apply to all repos regardless of language.
- Per-language patterns are keyed by `primary_language` values from
  `standard-tooling.toml`.
- Languages not in the dict (go, java, shell, none, claude-plugin) get
  only the base set.
- The merged result is sorted for determinism.

### Function signature

```python
def desired_actions_permissions(primary_language: str) -> DesiredActionsPermissions:
```

Takes `primary_language` as a plain string — only what the function
needs.

### Call site

In `compute_desired_state()`:

```python
actions_permissions=desired_actions_permissions(config.project.primary_language),
```

### Merge logic

```python
patterns = sorted(
    set(_BASE_ACTION_PATTERNS) | set(_LANGUAGE_ACTION_PATTERNS.get(primary_language, []))
)
```

### Tests

- Update `test_desired_actions_permissions` to verify base-only for a
  language with no extras (e.g., `go`), and to verify base + language
  patterns for a language with extras (e.g., `rust`).
- Update `test_compute_desired_state_includes_actions` since the
  function signature changed (the helper already takes a `language`
  parameter).

## Scope

This change touches:

- `src/standard_tooling/lib/github_config.py` — data constants,
  `desired_actions_permissions()`, `compute_desired_state()`
- `tests/standard_tooling/test_github_config_lib.py` — updated and new
  tests

No changes to config parsing, CLI entry points, or other modules.
