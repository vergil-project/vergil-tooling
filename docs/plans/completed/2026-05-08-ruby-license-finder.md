# Ruby license_finder Centralization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `license_finder` license compliance checking to the centralized Ruby AUDIT registry, with a shipped decisions file and `{configs}` placeholder expansion.

**Architecture:** Ship a static `license_finder.yml` decisions file under `src/standard_tooling/configs/ruby/`. Add `{configs}` placeholder expansion to `language_commands()` so registry entries can reference package-shipped config files at runtime. Add the `license_finder` command to the Ruby AUDIT entry using this placeholder.

**Tech Stack:** Python 3.12+, importlib.resources, license_finder (Ruby gem in dev-ruby container)

**Spec:** `docs/superpowers/specs/2026-05-08-ruby-license-finder-design.md`
**Issue:** [#603](https://github.com/wphillipmoore/standard-tooling/issues/603)

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Create | `src/standard_tooling/configs/ruby/__init__.py` | Empty — makes `configs/ruby` a Python subpackage for `importlib.resources` |
| Create | `src/standard_tooling/configs/ruby/license_finder.yml` | Static decisions file with five permitted Ruby licenses |
| Modify | `src/standard_tooling/lib/validate_commands.py` | Add `{configs}` expansion to `language_commands()`, add license_finder to Ruby AUDIT |
| Modify | `pyproject.toml:40-41` | Add `"configs/ruby/*.yml"` to package-data globs |
| Modify | `tests/standard_tooling/test_validate_commands.py:153-155` | Update Ruby audit tests, add placeholder expansion tests |

---

### Task 1: Ship the decisions file

**Files:**
- Create: `src/standard_tooling/configs/ruby/__init__.py`
- Create: `src/standard_tooling/configs/ruby/license_finder.yml`
- Modify: `pyproject.toml:40-41`

- [ ] **Step 1: Create the `configs/ruby/` subpackage**

Create an empty `__init__.py` to make `configs/ruby/` a Python subpackage (matching the existing `configs/__init__.py` pattern):

```
src/standard_tooling/configs/ruby/__init__.py  (empty file)
```

- [ ] **Step 2: Create the decisions file**

Create `src/standard_tooling/configs/ruby/license_finder.yml` with the five permitted licenses from mq-rest-admin-ruby:

```yaml
---
- - :permit
  - MIT
  - :who:
    :why:
    :versions: []
    :when: 2026-03-01 20:49:16.258089464 Z
- - :permit
  - Simplified BSD
  - :who:
    :why:
    :versions: []
    :when: 2026-03-01 20:49:17.028709111 Z
- - :permit
  - New BSD
  - :who:
    :why:
    :versions: []
    :when: 2026-03-01 20:49:17.705662790 Z
- - :permit
  - ruby
  - :who:
    :why:
    :versions: []
    :when: 2026-03-01 20:49:18.316100224 Z
- - :permit
  - GPL-3.0-or-later
  - :who:
    :why:
    :versions: []
    :when: 2026-03-01 20:49:18.971984481 Z
```

- [ ] **Step 3: Add package-data glob for the new subdirectory**

In `pyproject.toml`, change line 41 from:

```toml
standard_tooling = ["data/*.json", "configs/*.yaml", "configs/*.toml"]
```

to:

```toml
standard_tooling = ["data/*.json", "configs/*.yaml", "configs/*.toml", "configs/ruby/*.yml"]
```

- [ ] **Step 4: Verify the file is discoverable via importlib.resources**

Run from the project root (with the dev venv active):

```bash
python -c "from importlib.resources import files; p = files('standard_tooling.configs') / 'ruby' / 'license_finder.yml'; print(p); assert p.read_text()"
```

Expected: prints the resolved path and exits 0 (no assertion error).

- [ ] **Step 5: Commit**

```
feat(configs): add license_finder decisions file for Ruby audit

Closes: #603 (partial)
```

Stage: `src/standard_tooling/configs/ruby/__init__.py`, `src/standard_tooling/configs/ruby/license_finder.yml`, `pyproject.toml`

---

### Task 2: Add `{configs}` placeholder expansion to `language_commands()`

**Files:**
- Modify: `src/standard_tooling/lib/validate_commands.py:1-10,99-109`
- Modify: `tests/standard_tooling/test_validate_commands.py`

- [ ] **Step 1: Write the failing test for placeholder expansion**

Add to `tests/standard_tooling/test_validate_commands.py`, at the bottom of the edge-cases section:

```python
def test_configs_placeholder_is_resolved() -> None:
    """Commands containing {configs} must resolve to a real path."""
    cmds = language_commands("ruby", CheckKind.AUDIT)
    for cmd in cmds:
        for arg in cmd:
            assert "{configs}" not in arg, f"Unresolved placeholder in: {arg}"
```

- [ ] **Step 2: Run the test to verify it passes (baseline)**

```bash
st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py::test_configs_placeholder_is_resolved -v
```

Expected: PASS — the current Ruby AUDIT entry has no `{configs}` placeholders yet, so the assertion holds vacuously. This test becomes meaningful after Task 3 adds the placeholder to the registry.

- [ ] **Step 3: Add the import and expansion logic to `language_commands()`**

In `src/standard_tooling/lib/validate_commands.py`, add the `importlib.resources` import at the top (after the existing `from enum import Enum`):

```python
from importlib.resources import files
```

Then replace the `language_commands()` function (lines 99-108) with:

```python
def language_commands(language: str, kind: CheckKind) -> list[list[str]]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is not in the registry or
    has no entry for the given check kind.

    Any argument containing ``{configs}`` is expanded to the resolved
    path of the ``standard_tooling.configs`` package directory.
    """
    lang_entry = _REGISTRY.get(language)
    if lang_entry is None:
        return []
    cmds = lang_entry.get(kind, [])
    if not cmds:
        return []
    configs_dir = str(files("standard_tooling.configs"))
    return [
        [arg.replace("{configs}", configs_dir) for arg in cmd]
        for cmd in cmds
    ]
```

- [ ] **Step 4: Run full test suite to confirm nothing broke**

```bash
st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py -v
```

Expected: all existing tests PASS. The expansion is a no-op for commands without `{configs}`.

- [ ] **Step 5: Write a test that verifies expansion produces a real path**

Add to `tests/standard_tooling/test_validate_commands.py`, in the edge-cases section:

Add `from pathlib import Path` to the imports at the top of the test file, then add:

```python
def test_configs_placeholder_resolves_to_existing_directory() -> None:
    """The resolved {configs} path must point to a real directory."""
    cmds = language_commands("ruby", CheckKind.AUDIT)
    license_finder_cmds = [c for c in cmds if c[0] == "license_finder"]
    if not license_finder_cmds:
        return
    decisions_arg = license_finder_cmds[0][1]
    path = decisions_arg.split("=", 1)[1]
    assert Path(path).exists(), f"Resolved path does not exist: {path}"
```

Note: this test only becomes meaningful after Task 3 adds the `license_finder` entry. It will be skipped (early return) until then.

- [ ] **Step 6: Run tests**

```bash
st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```
feat(validate): add {configs} placeholder expansion to language_commands()
```

Stage: `src/standard_tooling/lib/validate_commands.py`, `tests/standard_tooling/test_validate_commands.py`

---

### Task 3: Add `license_finder` to the Ruby AUDIT registry

**Files:**
- Modify: `src/standard_tooling/lib/validate_commands.py:79-85`
- Modify: `tests/standard_tooling/test_validate_commands.py:153-155`

- [ ] **Step 1: Update the existing Ruby audit test**

In `tests/standard_tooling/test_validate_commands.py`, replace the existing `test_ruby_audit_commands` (lines 153-155):

```python
def test_ruby_audit_commands() -> None:
    joined = _joined(language_commands("ruby", CheckKind.AUDIT))
    assert any("bundle-audit" in c for c in joined)
    assert any("license_finder" in c for c in joined)
```

- [ ] **Step 2: Write a test for the license_finder decisions file argument**

Add after `test_ruby_audit_commands`:

```python
def test_ruby_audit_license_finder_decisions_file() -> None:
    cmds = language_commands("ruby", CheckKind.AUDIT)
    license_finder_cmds = [c for c in cmds if c[0] == "license_finder"]
    assert len(license_finder_cmds) == 1
    decisions_arg = license_finder_cmds[0][1]
    assert decisions_arg.startswith("--decisions-file=")
    path = decisions_arg.split("=", 1)[1]
    assert path.endswith("ruby/license_finder.yml")
    assert "{configs}" not in decisions_arg
```

- [ ] **Step 3: Run the new tests to verify they fail**

```bash
st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py::test_ruby_audit_commands tests/standard_tooling/test_validate_commands.py::test_ruby_audit_license_finder_decisions_file -v
```

Expected: FAIL — `license_finder` is not in the registry yet.

- [ ] **Step 4: Add the license_finder command to the Ruby AUDIT entry**

In `src/standard_tooling/lib/validate_commands.py`, replace line 84:

```python
        CheckKind.AUDIT: [["bundle", "exec", "bundle-audit", "check", "--update"]],
```

with:

```python
        CheckKind.AUDIT: [
            ["bundle", "exec", "bundle-audit", "check", "--update"],
            ["license_finder", "--decisions-file={configs}/ruby/license_finder.yml"],
        ],
```

- [ ] **Step 5: Run the full test suite**

```bash
st-docker-run -- uv run pytest tests/standard_tooling/test_validate_commands.py -v
```

Expected: all PASS — including the new tests and the placeholder expansion tests from Task 2.

- [ ] **Step 6: Commit**

```
feat(validate): add license_finder to Ruby audit registry
```

Stage: `src/standard_tooling/lib/validate_commands.py`, `tests/standard_tooling/test_validate_commands.py`

---

### Task 4: Full validation

- [ ] **Step 1: Run the full validation pipeline**

```bash
st-docker-run -- uv run st-validate
```

Expected: all checks pass (lint, typecheck, test, audit).

- [ ] **Step 2: Commit any fixups if needed**

If validation surfaces formatting or lint issues, fix and commit.
