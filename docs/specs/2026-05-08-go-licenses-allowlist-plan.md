# Go License Allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a centralized Go license allowlist to `validate_commands.py` so `go-licenses check` enforces org-wide license policy.

**Architecture:** Add a `_GO_LICENSES_ALLOWLIST` module-level constant (comma-joined SPDX identifiers) and pass it via `--allowed_licenses=` in the Go audit registry entry. Mirrors the existing Python `_PIP_LICENSES_ALLOWLIST` pattern.

**Tech Stack:** Python, pytest

**Spec:** `docs/specs/2026-05-08-go-licenses-allowlist-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/standard_tooling/lib/validate_commands.py` | Modify (lines 59-68) | Add `_GO_LICENSES_ALLOWLIST` constant, update Go audit entry |
| `tests/standard_tooling/test_validate_commands.py` | Modify (lines 82-85) | Add allowlist assertion to existing Go audit test, add dedicated allowlist test |

---

### Task 1: Add failing test for Go license allowlist

**Files:**
- Modify: `tests/standard_tooling/test_validate_commands.py:82-85`

- [ ] **Step 1: Add a dedicated allowlist test**

Add a new test after `test_go_audit_commands` (line 85), mirroring `test_python_audit_pip_licenses_allowlist_intact` (lines 49-55):

```python
def test_go_audit_go_licenses_allowlist_intact() -> None:
    cmds = language_commands("go", CheckKind.AUDIT)
    go_licenses_cmd = [c for c in cmds if c[0] == "go-licenses"]
    assert len(go_licenses_cmd) == 1
    flag = go_licenses_cmd[0][-1]
    assert flag.startswith("--allowed_licenses=")
    licenses = flag.split("=", 1)[1].split(",")
    assert "MIT" in licenses
    assert "Apache-2.0" in licenses
    assert len(licenses) == 7
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py::test_go_audit_go_licenses_allowlist_intact -v`

Expected: FAIL — `go-licenses` command currently has no `--allowed_licenses` flag, so `go_licenses_cmd[0][-1]` is `"./..."` which does not start with `--allowed_licenses=`.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/standard_tooling/test_validate_commands.py
git commit -m "test: add failing test for Go license allowlist (#604)"
```

---

### Task 2: Add Go license allowlist constant and update registry

**Files:**
- Modify: `src/standard_tooling/lib/validate_commands.py:41-67`

- [ ] **Step 1: Add the `_GO_LICENSES_ALLOWLIST` constant**

Insert after `_PIP_LICENSES_ALLOWLIST` (after line 41), before `_REGISTRY`:

```python
_GO_LICENSES_ALLOWLIST = ",".join(
    [
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0",
        "ISC",
        "MIT",
        "MPL-2.0",
    ]
)
```

- [ ] **Step 2: Update the Go audit registry entry**

Change the Go `CheckKind.AUDIT` entry (line 67) from:

```python
CheckKind.AUDIT: [["govulncheck", "./..."], ["go-licenses", "check", "./..."]],
```

to:

```python
CheckKind.AUDIT: [
    ["govulncheck", "./..."],
    ["go-licenses", "check", "./...", f"--allowed_licenses={_GO_LICENSES_ALLOWLIST}"],
],
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py::test_go_audit_go_licenses_allowlist_intact -v`

Expected: PASS

- [ ] **Step 4: Run the full test suite**

Run: `st-docker-run -- uv run st-validate`

Expected: All checks pass (lint, typecheck, tests, audit).

- [ ] **Step 5: Commit**

```bash
git add src/standard_tooling/lib/validate_commands.py
git commit -m "feat: add Go license allowlist to centralized audit (#604)"
```

---

### Task 3: Update existing Go audit test assertion

**Files:**
- Modify: `tests/standard_tooling/test_validate_commands.py:82-85`

- [ ] **Step 1: Strengthen the existing `test_go_audit_commands` test**

The current test (lines 82-85) only checks that `go-licenses` appears somewhere in the joined command strings. Update it to also verify the allowlist flag is present:

```python
def test_go_audit_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.AUDIT))
    assert any("govulncheck" in c for c in joined)
    assert any("go-licenses" in c and "--allowed_licenses=" in c for c in joined)
```

- [ ] **Step 2: Run tests**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py -v`

Expected: All Go tests pass.

- [ ] **Step 3: Run full validation**

Run: `st-docker-run -- uv run st-validate`

Expected: All checks pass.

- [ ] **Step 4: Commit**

```bash
git add tests/standard_tooling/test_validate_commands.py
git commit -m "test: strengthen Go audit test to verify allowlist flag (#604)"
```
