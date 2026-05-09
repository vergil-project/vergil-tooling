# Language-Specific Action Patterns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `_ALLOWED_ACTION_PATTERNS` into a base set and per-language sets so each repo only gets the action patterns relevant to its primary language.

**Architecture:** Replace the single `_ALLOWED_ACTION_PATTERNS` list with `_BASE_ACTION_PATTERNS` and `_LANGUAGE_ACTION_PATTERNS` dict. Thread `primary_language: str` into `desired_actions_permissions()` and its call site in `compute_desired_state()`.

**Tech Stack:** Python, pytest

---

### File Map

- **Modify:** `src/standard_tooling/lib/github_config.py` — data constants, function signature, call site
- **Modify:** `tests/standard_tooling/test_github_config_lib.py` — update and add tests

### Task 1: Update tests for the new signature and language-specific behavior

**Files:**
- Modify: `tests/standard_tooling/test_github_config_lib.py:84-91` (existing test)
- Modify: `tests/standard_tooling/test_github_config_lib.py:322-324` (existing test)

- [ ] **Step 1: Rewrite `test_desired_actions_permissions` to cover base-only and base+language cases**

Replace lines 84–91 with:

```python
def test_desired_actions_permissions_base_only() -> None:
    a = desired_actions_permissions("go")
    assert a.default_workflow_permissions == "read"
    assert a.can_approve_pull_request_reviews is False
    assert a.allowed_actions == "selected"
    assert a.patterns_allowed == [
        "actions/*",
        "docker/*",
        "github/*",
        "wphillipmoore/*",
    ]


def test_desired_actions_permissions_with_language_patterns() -> None:
    a = desired_actions_permissions("rust")
    assert a.patterns_allowed == [
        "actions-rust-lang/*",
        "actions/*",
        "docker/*",
        "github/*",
        "swatinem/*",
        "wphillipmoore/*",
    ]


def test_desired_actions_permissions_python() -> None:
    a = desired_actions_permissions("python")
    assert a.patterns_allowed == [
        "actions/*",
        "astral-sh/*",
        "docker/*",
        "github/*",
        "pypa/*",
        "wphillipmoore/*",
    ]
```

- [ ] **Step 2: Update `test_compute_desired_state_includes_actions`**

The `_st_config()` helper defaults to `language="python"`, so the test
at line 322–324 needs no signature changes — just verify it picks up
language-specific patterns. Replace with:

```python
def test_compute_desired_state_includes_actions() -> None:
    state = compute_desired_state(_st_config(), visibility="public")
    assert state.actions_permissions.allowed_actions == "selected"
    assert "pypa/*" in state.actions_permissions.patterns_allowed
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_github_config_lib.py::test_desired_actions_permissions_base_only tests/standard_tooling/test_github_config_lib.py::test_desired_actions_permissions_with_language_patterns tests/standard_tooling/test_github_config_lib.py::test_desired_actions_permissions_python tests/standard_tooling/test_github_config_lib.py::test_compute_desired_state_includes_actions -v`

Expected: FAIL — `desired_actions_permissions()` does not accept a positional argument yet.

### Task 2: Implement the data constants and function changes

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py:90-140` (constants and function)
- Modify: `src/standard_tooling/lib/github_config.py:320` (call site)

- [ ] **Step 1: Replace `_ALLOWED_ACTION_PATTERNS` with `_BASE_ACTION_PATTERNS` and `_LANGUAGE_ACTION_PATTERNS`**

Replace lines 90–100 with:

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

- [ ] **Step 2: Update `desired_actions_permissions()` to accept `primary_language`**

Replace lines 134–140 with:

```python
def desired_actions_permissions(primary_language: str) -> DesiredActionsPermissions:
    patterns = sorted(
        set(_BASE_ACTION_PATTERNS) | set(_LANGUAGE_ACTION_PATTERNS.get(primary_language, []))
    )
    return DesiredActionsPermissions(
        default_workflow_permissions="read",
        can_approve_pull_request_reviews=False,
        allowed_actions="selected",
        patterns_allowed=patterns,
    )
```

- [ ] **Step 3: Update the call site in `compute_desired_state()`**

Replace line 320:

```python
        actions_permissions=desired_actions_permissions(),
```

with:

```python
        actions_permissions=desired_actions_permissions(config.project.primary_language),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_github_config_lib.py -v`

Expected: All tests PASS.

### Task 3: Run full validation and commit

- [ ] **Step 1: Run full validation**

Run: `st-docker-run -- uv run st-validate`

Expected: All checks pass (lint, typecheck, tests, audit).

- [ ] **Step 2: Commit**

```bash
st-commit
```

Commit message: `feat(github-config): make allowed action patterns language-specific (#613)`
