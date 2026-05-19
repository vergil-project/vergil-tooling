# vrg-git Conditional Policy Relaxation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `--force-with-lease` push on non-protected branches and `branch -D` when the remote tracking ref is gone, while maintaining all existing safety denials.

**Architecture:** Two new private helper functions (`_is_protected_branch`, `_is_upstream_gone`) in `vrg_git.py` that call git to inspect runtime state. `_check_denied_flags` gains custom paths for `push` and `branch` that call these helpers before falling through to the generic deny logic. All other subcommands are unchanged.

**Tech Stack:** Python 3.12, pytest, `subprocess.run` (stdlib only — no `lib.git` import)

**Spec:** `docs/specs/2026-05-19-vrg-git-conditional-policy-design.md`

**Worktree:** `/Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy/`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/bin/vrg_git.py` | Modify (lines 56-98) | Add `_is_protected_branch`, `_is_upstream_gone`, update `_check_denied_flags` |
| `tests/vergil_tooling/test_vrg_git.py` | Modify (lines 178-205) | Update existing tests, add new conditional-allow/deny tests |
| `docs/site/docs/guides/permission-model.md` | Modify (lines 45, 52) | Update denied-flags table to reflect conditional behavior |

---

### Task 1: Add `_is_protected_branch` helper and tests

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_git.py` (insert after line 62, before `_log_path`)
- Modify: `tests/vergil_tooling/test_vrg_git.py` (insert new section)

- [ ] **Step 1: Write tests for `_is_protected_branch`**

Add these tests after the existing imports in `test_vrg_git.py`, in a new section before the flag deny tests (before line 175):

```python
# -- helper: _is_protected_branch --------------------------------------------


def test_is_protected_branch_develop() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="develop\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is True


def test_is_protected_branch_main() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="main\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is True


def test_is_protected_branch_release() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="release/2.0.22\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is True


def test_is_protected_branch_feature() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="feature/827-force-push\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is False
```

Note: add `import subprocess` to the test file imports (alongside the existing `from unittest.mock import patch`).

- [ ] **Step 2: Run the tests — expect FAIL**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py::test_is_protected_branch_develop -v`

Expected: `ImportError` — `_is_protected_branch` does not exist yet.

- [ ] **Step 3: Implement `_is_protected_branch`**

Insert in `vrg_git.py` after line 61 (after the `_FLAG_DENY` dict), before `_log_path`:

```python
_PROTECTED_BRANCHES: set[str] = {"develop", "main"}
_PROTECTED_PREFIXES: tuple[str, ...] = ("release/",)


def _is_protected_branch() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S603, S607
        capture_output=True,
        text=True,
        check=False,
    )
    branch = result.stdout.strip()
    if branch in _PROTECTED_BRANCHES:
        return True
    return any(branch.startswith(p) for p in _PROTECTED_PREFIXES)
```

- [ ] **Step 4: Run the tests — expect PASS**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -k "is_protected_branch" -v`

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy
vrg-git add src/vergil_tooling/bin/vrg_git.py tests/vergil_tooling/test_vrg_git.py
vrg-commit --type feat --scope vrg-git --message "add _is_protected_branch helper (#827)"
```

---

### Task 2: Add `_is_upstream_gone` helper and tests

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_git.py` (insert after `_is_protected_branch`)
- Modify: `tests/vergil_tooling/test_vrg_git.py` (insert new section)

- [ ] **Step 1: Write tests for `_is_upstream_gone`**

Add a new section in `test_vrg_git.py` after the `_is_protected_branch` tests:

```python
# -- helper: _is_upstream_gone ------------------------------------------------


def test_is_upstream_gone_true() -> None:
    vv_output = (
        "  develop                  abc1234 [origin/develop] latest commit\n"
        "  feature/123-foo          def5678 [origin/feature/123-foo: gone] old commit\n"
        "* feature/827-force-push   ghi9012 [origin/feature/827-force-push] current\n"
    )
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/123-foo") is True


def test_is_upstream_gone_active_upstream() -> None:
    vv_output = (
        "  feature/123-foo abc1234 [origin/feature/123-foo] some commit\n"
    )
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/123-foo") is False


def test_is_upstream_gone_no_upstream() -> None:
    vv_output = "  feature/123-foo abc1234 some commit with no tracking\n"
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/123-foo") is False


def test_is_upstream_gone_branch_not_found() -> None:
    vv_output = "  develop abc1234 [origin/develop] latest commit\n"
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/nonexistent") is False
```

- [ ] **Step 2: Run the tests — expect FAIL**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py::test_is_upstream_gone_true -v`

Expected: `ImportError` — `_is_upstream_gone` does not exist yet.

- [ ] **Step 3: Implement `_is_upstream_gone`**

Insert in `vrg_git.py` after `_is_protected_branch`:

```python
def _is_upstream_gone(branch_name: str) -> bool:
    result = subprocess.run(
        ["git", "branch", "-vv"],  # noqa: S603, S607
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        stripped = line.lstrip("* ").strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if parts and parts[0] == branch_name:
            return ": gone]" in line
    return False
```

The function strips the leading `*` (current branch marker) and whitespace, extracts the first token as the branch name, and checks whether `[gone]` appears anywhere in that line.

- [ ] **Step 4: Run the tests — expect PASS**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -k "is_upstream_gone" -v`

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy
vrg-git add src/vergil_tooling/bin/vrg_git.py tests/vergil_tooling/test_vrg_git.py
vrg-commit --type feat --scope vrg-git --message "add _is_upstream_gone helper (#845)"
```

---

### Task 3: Wire `--force-with-lease` conditional into `_check_denied_flags`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_git.py` (lines 80-98, the `_check_denied_flags` function)
- Modify: `tests/vergil_tooling/test_vrg_git.py` (update existing test, add new tests)

- [ ] **Step 1: Write tests for conditional force-with-lease behavior**

Update the existing test and add new ones. The existing `test_push_force_with_lease_denied` (line 202) needs to be replaced with conditional tests. Add this new section after the existing push tests:

```python
# -- push --force-with-lease (conditional) ------------------------------------


def test_push_force_with_lease_allowed_on_feature_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch(
            "vergil_tooling.bin.vrg_git._is_protected_branch", return_value=False,
        ),
    ):
        mock_run.return_value.returncode = 0
        rc = main(["push", "--force-with-lease"])
    assert rc == 0


def test_push_force_with_lease_denied_on_develop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "vergil_tooling.bin.vrg_git._is_protected_branch", return_value=True,
    ):
        rc = main(["push", "--force-with-lease"])
    assert rc != 0
    assert "protected branch" in capsys.readouterr().err.lower()


def test_push_force_with_lease_denied_on_release(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "vergil_tooling.bin.vrg_git._is_protected_branch", return_value=True,
    ):
        rc = main(["push", "--force-with-lease"])
    assert rc != 0
    assert "protected branch" in capsys.readouterr().err.lower()
```

Also remove the old `test_push_force_with_lease_denied` test (line 202-203) since it asserts unconditional denial which is no longer correct.

The existing `test_push_force_denied` and `test_push_force_short_denied` tests remain unchanged — `-f` and `--force` are still unconditionally denied.

- [ ] **Step 2: Run the new tests — expect FAIL**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py::test_push_force_with_lease_allowed_on_feature_branch -v`

Expected: FAIL — `_check_denied_flags` still unconditionally denies `--force-with-lease`.

- [ ] **Step 3: Update `_check_denied_flags` for push**

In `_check_denied_flags`, replace the generic loop (lines 95-98) with a push-aware path. The new logic:

```python
def _check_denied_flags(subcmd: str, args: list[str]) -> str | None:
    denied_flags = _FLAG_DENY.get(subcmd, set())
    if not denied_flags:
        return None

    if subcmd == "checkout":
        after_separator = False
        for arg in args:
            if arg == "--":
                after_separator = True
                continue
            if after_separator and arg in denied_flags:
                return f"checkout -- {arg} is denied by vrg-git."
        return None

    if subcmd == "push":
        for arg in args:
            if arg in ("-f", "--force"):
                return f"push {arg} is denied by vrg-git."
            if arg == "--force-with-lease":
                if _is_protected_branch():
                    return "push --force-with-lease is denied on a protected branch."
        return None

    for arg in args:
        if arg in denied_flags:
            return f"{subcmd} {arg} is denied by vrg-git."
    return None
```

The push path explicitly checks `-f`/`--force` first (always denied), then `--force-with-lease` (conditionally denied based on branch). The generic loop at the bottom handles all other subcommands (branch, rebase) unchanged.

- [ ] **Step 4: Run all push-related tests — expect PASS**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -k "push" -v`

Expected: all push tests pass (force denied, force-short denied, force-with-lease conditional, normal allowed).

- [ ] **Step 5: Run full test suite — expect PASS**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy
vrg-git add src/vergil_tooling/bin/vrg_git.py tests/vergil_tooling/test_vrg_git.py
vrg-commit --type feat --scope vrg-git --message "allow --force-with-lease on non-protected branches (#827)"
```

---

### Task 4: Wire `branch -D` conditional into `_check_denied_flags`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_git.py` (the `_check_denied_flags` function)
- Modify: `tests/vergil_tooling/test_vrg_git.py` (update existing test, add new tests)

- [ ] **Step 1: Write tests for conditional branch -D behavior**

Replace the existing `test_branch_force_delete_denied` (line 178) with conditional tests. Add this new section:

```python
# -- branch -D (conditional) -------------------------------------------------


def test_branch_force_delete_allowed_when_upstream_gone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch(
            "vergil_tooling.bin.vrg_git._is_upstream_gone", return_value=True,
        ),
    ):
        mock_run.return_value.returncode = 0
        rc = main(["branch", "-D", "feature/123-foo"])
    assert rc == 0


def test_branch_force_delete_denied_when_upstream_active(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "vergil_tooling.bin.vrg_git._is_upstream_gone", return_value=False,
    ):
        rc = main(["branch", "-D", "feature/123-foo"])
    assert rc != 0
    assert "denied" in capsys.readouterr().err.lower()


def test_branch_force_delete_denied_no_branch_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["branch", "-D"])
    assert rc != 0
    assert "denied" in capsys.readouterr().err.lower()
```

Remove the old `test_branch_force_delete_denied` (line 178-180) since it asserts unconditional denial.

The existing `test_branch_force_flag_denied` (line 183) and `test_branch_safe_delete_allowed` (line 187) remain unchanged — `--force` is still unconditionally denied, and `-d` is still allowed.

- [ ] **Step 2: Run the new tests — expect FAIL**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py::test_branch_force_delete_allowed_when_upstream_gone -v`

Expected: FAIL — `_check_denied_flags` still unconditionally denies `-D`.

- [ ] **Step 3: Update `_check_denied_flags` for branch**

Add a branch-specific path before the generic loop. The full updated function now handles push, branch, checkout, and generic:

```python
def _check_denied_flags(subcmd: str, args: list[str]) -> str | None:
    denied_flags = _FLAG_DENY.get(subcmd, set())
    if not denied_flags:
        return None

    if subcmd == "checkout":
        after_separator = False
        for arg in args:
            if arg == "--":
                after_separator = True
                continue
            if after_separator and arg in denied_flags:
                return f"checkout -- {arg} is denied by vrg-git."
        return None

    if subcmd == "push":
        for arg in args:
            if arg in ("-f", "--force"):
                return f"push {arg} is denied by vrg-git."
            if arg == "--force-with-lease":
                if _is_protected_branch():
                    return "push --force-with-lease is denied on a protected branch."
        return None

    if subcmd == "branch":
        for arg in args:
            if arg == "--force":
                return "branch --force is denied by vrg-git."
            if arg == "-D":
                idx = args.index("-D")
                if idx + 1 >= len(args):
                    return "branch -D is denied by vrg-git."
                branch_name = args[idx + 1]
                if not _is_upstream_gone(branch_name):
                    return (
                        f"branch -D is denied (upstream is not gone"
                        f" for {branch_name})."
                    )
        return None

    for arg in args:
        if arg in denied_flags:
            return f"{subcmd} {arg} is denied by vrg-git."
    return None
```

The branch path checks `--force` (always denied), then `-D` (extracts the branch name from the next argument, checks `_is_upstream_gone`). If `-D` has no following argument, it is denied.

- [ ] **Step 4: Run all branch-related tests — expect PASS**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -k "branch" -v`

Expected: all branch tests pass (force denied, safe delete allowed, -D conditional).

- [ ] **Step 5: Run full test suite — expect PASS**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy
vrg-git add src/vergil_tooling/bin/vrg_git.py tests/vergil_tooling/test_vrg_git.py
vrg-commit --type feat --scope vrg-git --message "allow branch -D when upstream is gone (#845)"
```

---

### Task 5: Update permission model documentation

**Files:**
- Modify: `docs/site/docs/guides/permission-model.md` (lines 45, 52)

- [ ] **Step 1: Update the denied flags table**

Change line 45 from:
```
| `branch` | `-D`, `--force` | Safe delete (`-d`) allowed |
```
to:
```
| `branch` | `--force`; `-D` conditional | `-d` allowed; `-D` allowed when upstream is `[gone]` |
```

Change line 52 from:
```
| `push` | `--force`, `-f`, `--force-with-lease` | Normal push only |
```
to:
```
| `push` | `--force`, `-f`; `--force-with-lease` conditional | `--force-with-lease` allowed on non-protected branches |
```

- [ ] **Step 2: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy
vrg-git add docs/site/docs/guides/permission-model.md
vrg-commit --type docs --scope permission-model --message "update denied-flags table for conditional push and branch policies (#827, #845)"
```

---

### Task 6: Full validation

- [ ] **Step 1: Run full validation pipeline**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-827-force-push-policy && vrg-docker-run -- uv run vrg-validate`

Expected: all checks pass (lint, typecheck, tests, audit, common checks).

- [ ] **Step 2: Fix any failures and re-run**

If any check fails, fix the issue and re-run validation. Commit fixes as a separate commit with appropriate type/scope.
