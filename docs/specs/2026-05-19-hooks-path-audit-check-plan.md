# core.hooksPath Audit Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the repo audit tool to verify that `core.hooksPath` is configured to `.githooks` in local git config.

**Architecture:** Add a `subprocess.run(["git", "config", "core.hooksPath"], ...)` call inside the existing `_check_githooks()` function in `repo_config.py`. The check fires only when `.githooks/pre-commit` already exists on disk. Non-zero exit codes or wrong values produce a `DiffItem` with field `local.git_config.hooks_path`.

**Tech Stack:** Python 3.12+, subprocess (stdlib), pytest

---

## File Map

- **Modify:** `src/vergil_tooling/lib/repo_config.py` — add `subprocess` import and extend `_check_githooks()`
- **Modify:** `tests/vergil_tooling/test_repo_config.py` — add three new tests in `TestGithooks`, update `_write_compliant_repo()` and `TestIntegration`

---

### Task 1: Test — hooks_path not configured

Add a test for the case where `.githooks/pre-commit` exists but no git repo is initialized (so `git config core.hooksPath` fails).

**Files:**
- Modify: `tests/vergil_tooling/test_repo_config.py` (inside `TestGithooks`, after line 76)

- [ ] **Step 1: Write the failing test**

Add this test method to the `TestGithooks` class, after the existing `test_present` method:

```python
def test_hooks_path_not_configured(self, tmp_path: Path) -> None:
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "pre-commit").write_text("#!/bin/sh\n")
    diff = audit_local_config(tmp_path)
    fields = {i.field for i in diff.items}
    assert "local.git_config.hooks_path" in fields
    match = next(i for i in diff.items if i.field == "local.git_config.hooks_path")
    assert match.actual == "not configured"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_config.py::TestGithooks::test_hooks_path_not_configured -v`

Expected: FAIL — `local.git_config.hooks_path` is not in the diff because the check doesn't exist yet.

- [ ] **Step 3: Implement the check**

In `src/vergil_tooling/lib/repo_config.py`, add `subprocess` to the imports:

```python
import subprocess
```

Then extend `_check_githooks()` — the current function returns early when the file is missing. After the early return (when the file *does* exist), add the `core.hooksPath` check:

```python
def _check_githooks(repo_root: Path, items: list[DiffItem]) -> None:
    hook_path = repo_root / ".githooks" / "pre-commit"
    if not hook_path.is_file():
        items.append(
            DiffItem(
                field="local.githooks_pre_commit",
                expected="present",
                actual="missing",
            )
        )
        return

    result = subprocess.run(
        ["git", "config", "core.hooksPath"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    actual = result.stdout.strip()
    if actual != ".githooks":
        items.append(
            DiffItem(
                field="local.git_config.hooks_path",
                expected=".githooks",
                actual=actual or "not configured",
            )
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_config.py::TestGithooks::test_hooks_path_not_configured -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope repo-config --message "add core.hooksPath audit check (#825)"
```

---

### Task 2: Test — hooks_path wrong value

Add a test for the case where `core.hooksPath` is configured but set to the wrong value.

**Files:**
- Modify: `tests/vergil_tooling/test_repo_config.py` (inside `TestGithooks`)

- [ ] **Step 1: Write the failing test**

Add this test method to `TestGithooks`, after `test_hooks_path_not_configured`:

```python
def test_hooks_path_wrong_value(self, tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", "wrong/path"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "pre-commit").write_text("#!/bin/sh\n")
    diff = audit_local_config(tmp_path)
    fields = {i.field for i in diff.items}
    assert "local.git_config.hooks_path" in fields
    match = next(i for i in diff.items if i.field == "local.git_config.hooks_path")
    assert match.actual == "wrong/path"
```

This test also needs `subprocess` imported at the top of the test file. Add to the imports:

```python
import subprocess
```

- [ ] **Step 2: Run test to verify it passes**

This test should pass immediately because the implementation from Task 1 already handles wrong values.

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_config.py::TestGithooks::test_hooks_path_wrong_value -v`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
vrg-commit --type test --scope repo-config --message "add test for wrong core.hooksPath value"
```

---

### Task 3: Test — hooks_path correctly configured

Add a test for the happy path where `core.hooksPath` is set to `.githooks`.

**Files:**
- Modify: `tests/vergil_tooling/test_repo_config.py` (inside `TestGithooks`)

- [ ] **Step 1: Write the failing test**

Add this test method to `TestGithooks`, after `test_hooks_path_wrong_value`:

```python
def test_hooks_path_configured(self, tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    (tmp_path / ".githooks").mkdir()
    (tmp_path / ".githooks" / "pre-commit").write_text("#!/bin/sh\n")
    diff = audit_local_config(tmp_path)
    fields = {i.field for i in diff.items}
    assert "local.git_config.hooks_path" not in fields
```

- [ ] **Step 2: Run test to verify it passes**

This should also pass immediately — the implementation already handles the matching case.

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_config.py::TestGithooks::test_hooks_path_configured -v`

Expected: PASS

- [ ] **Step 3: Commit**

```bash
vrg-commit --type test --scope repo-config --message "add test for correctly configured core.hooksPath"
```

---

### Task 4: Update integration test

The existing `_write_compliant_repo()` helper scaffolds a fully compliant repo, but it doesn't `git init` or set `core.hooksPath`. The new check will cause `test_compliant_repo` to fail. Fix by initializing a git repo and setting the config.

**Files:**
- Modify: `tests/vergil_tooling/test_repo_config.py` (`_write_compliant_repo()` at line 262, and `TestIntegration` at line 272)

- [ ] **Step 1: Run the existing integration test to confirm it fails**

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_config.py::TestIntegration::test_compliant_repo -v`

Expected: FAIL — `local.git_config.hooks_path` will appear in the diff because `_write_compliant_repo()` doesn't set up git.

- [ ] **Step 2: Update `_write_compliant_repo()` to initialize git**

Replace the `_write_compliant_repo` function (line 262) with:

```python
def _write_compliant_repo(root: Path) -> None:
    """Scaffold a fully compliant repo structure."""
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    (root / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
    (root / ".githooks").mkdir()
    (root / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (root / "CLAUDE.md").write_text("# CLAUDE.md\n\n" + _TEMPLATE_TEXT + "\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(json.dumps(_MINIMAL_SETTINGS))
```

- [ ] **Step 3: Update `test_empty_directory_reports_all_missing`**

The empty-directory test (line 273) should also verify the new field is *not* reported for an empty directory — because `.githooks/pre-commit` doesn't exist, the conditional check should not fire. The existing assertions already cover this implicitly (the test checks for specific fields, not that no others appear), but add an explicit assertion to document the conditional behavior:

After the existing assertions in `test_empty_directory_reports_all_missing`, add:

```python
assert "local.git_config.hooks_path" not in fields
```

- [ ] **Step 4: Run the full integration test class**

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_config.py::TestIntegration -v`

Expected: PASS — both `test_empty_directory_reports_all_missing` and `test_compliant_repo` pass.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type test --scope repo-config --message "update integration tests for core.hooksPath check"
```

---

### Task 5: Full validation

Run the complete validation pipeline to confirm nothing is broken.

**Files:** None (validation only)

- [ ] **Step 1: Run all repo_config tests**

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_repo_config.py -v`

Expected: All tests pass, including the three new ones and the updated integration tests.

- [ ] **Step 2: Run full validation**

Run: `cd .worktrees/issue-825-hooks-path-audit && vrg-docker-run -- uv run vrg-validate`

Expected: All checks pass (lint, typecheck, tests, audit).
