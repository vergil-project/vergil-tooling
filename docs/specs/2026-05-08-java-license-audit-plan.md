# Java License Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add license policy enforcement flags to the centralized Java audit command so `st-validate --check audit` blocks on license violations.

**Architecture:** Add a `_MAVEN_LICENSES_ALLOWLIST` pipe-joined constant to `validate_commands.py` and pass it with `-Dlicense.failIfWarning=true` and `-Dlicense.excludedScopes=test` in the Java audit registry entry. Mirrors the existing Go and Python allowlist patterns.

**Tech Stack:** Python, pytest

---

### Task 1: Add failing test for Java audit allowlist flag

**Files:**
- Modify: `tests/standard_tooling/test_validate_commands.py:126-129`

- [ ] **Step 1: Update `test_java_audit_commands` to assert the `-D` flags**

Replace the existing test at line 126:

```python
def test_java_audit_commands() -> None:
    joined = _joined(language_commands("java", CheckKind.AUDIT))
    assert any("dependency:tree" in c for c in joined)
    assert any("license-maven-plugin" in c for c in joined)
    assert any("-Dlicense.failIfWarning=true" in c for c in joined)
    assert any("-Dlicense.includedLicenses=" in c for c in joined)
    assert any("-Dlicense.excludedScopes=test" in c for c in joined)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/standard_tooling/test_validate_commands.py::test_java_audit_commands -v`

Expected: FAIL — the current Java audit command has no `-D` flags.

---

### Task 2: Add failing allowlist integrity test

**Files:**
- Modify: `tests/standard_tooling/test_validate_commands.py` (add after `test_java_audit_commands`)

- [ ] **Step 1: Add `test_java_audit_maven_licenses_allowlist_intact`**

Add this test immediately after `test_java_audit_commands`:

```python
def test_java_audit_maven_licenses_allowlist_intact() -> None:
    cmds = language_commands("java", CheckKind.AUDIT)
    license_cmd = [c for c in cmds if any("license-maven-plugin" in arg for arg in c)]
    assert len(license_cmd) == 1
    flag = [arg for arg in license_cmd[0] if arg.startswith("-Dlicense.includedLicenses=")]
    assert len(flag) == 1
    licenses = flag[0].split("=", 1)[1].split("|")
    assert "MIT License" in licenses
    assert "Apache-2.0" in licenses
    assert len(licenses) == 9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/standard_tooling/test_validate_commands.py::test_java_audit_maven_licenses_allowlist_intact -v`

Expected: FAIL — no `-Dlicense.includedLicenses=` argument exists yet.

---

### Task 3: Add the allowlist constant and update the Java audit entry

**Files:**
- Modify: `src/standard_tooling/lib/validate_commands.py:54` (add constant after `_GO_LICENSES_ALLOWLIST`)
- Modify: `src/standard_tooling/lib/validate_commands.py:89-92` (update Java audit entry)

- [ ] **Step 1: Add `_MAVEN_LICENSES_ALLOWLIST` constant**

Add the following after the `_GO_LICENSES_ALLOWLIST` block (after line 54):

```python
_MAVEN_LICENSES_ALLOWLIST = "|".join(
    [
        "Apache 2.0",
        "Apache-2.0",
        "The Apache License, Version 2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0-or-later",
        "ISC",
        "MIT License",
        "MPL-2.0",
    ]
)
```

- [ ] **Step 2: Update the Java audit registry entry**

Replace the current Java `CheckKind.AUDIT` value (lines 90-92):

```python
        CheckKind.AUDIT: [
            ["./mvnw", "dependency:tree", "-B", "-q"],
            [
                "./mvnw",
                "org.codehaus.mojo:license-maven-plugin:add-third-party",
                "-Dlicense.excludedScopes=test",
                "-Dlicense.failIfWarning=true",
                f"-Dlicense.includedLicenses={_MAVEN_LICENSES_ALLOWLIST}",
                "-B",
            ],
        ],
```

- [ ] **Step 3: Run the two new/updated tests to verify they pass**

Run: `uv run pytest tests/standard_tooling/test_validate_commands.py::test_java_audit_commands tests/standard_tooling/test_validate_commands.py::test_java_audit_maven_licenses_allowlist_intact -v`

Expected: Both PASS.

- [ ] **Step 4: Run the full test suite to check for regressions**

Run: `uv run pytest tests/standard_tooling/test_validate_commands.py -v`

Expected: All tests PASS. No other language entries are affected.

- [ ] **Step 5: Commit**

```
git add src/standard_tooling/lib/validate_commands.py tests/standard_tooling/test_validate_commands.py
```

Commit message:
```
feat(validate): add Java license allowlist to centralized audit (#600)
```

---

### Task 4: Run full validation

- [ ] **Step 1: Run `st-validate` in the dev container**

Run: `st-docker-run -- uv run st-validate`

Expected: All checks pass (lint, typecheck, test, audit).

- [ ] **Step 2: Commit any fixups if needed**

If `st-validate` flags formatting or lint issues, fix and commit separately:

```
fix(validate): address lint/format issues from st-validate
```
