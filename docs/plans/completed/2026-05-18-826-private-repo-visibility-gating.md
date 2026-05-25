# Private Repo Visibility Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vrg-github-repo-config` handle private repos gracefully by skipping GHAS-gated security features instead of crashing with HTTP 422.

**Architecture:** Thread `visibility` into `desired_security_settings()` so it returns `None` for GHAS-gated fields on private repos. Extend `ConfigDiff` with a `skipped` list so the diff engine records (rather than silently ignores) `None` desired values. The apply path omits `None` fields from API calls. The CLI renders skipped fields as informational output.

**Tech Stack:** Python 3.12, dataclasses, pytest, unittest.mock

---

### Task 1: Make `desired_security_settings` visibility-aware

**Files:**
- Modify: `src/vergil_tooling/lib/github_config.py:127-133`
- Test: `tests/vergil_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing test for public repo (preserves existing behavior)**

In `tests/vergil_tooling/test_github_config_lib.py`, update the existing test to pass the new parameter:

```python
def test_desired_security_settings_public() -> None:
    s = desired_security_settings(visibility="public")
    assert s.secret_scanning == "enabled"  # noqa: S105
    assert s.secret_scanning_push_protection == "enabled"  # noqa: S105
    assert s.vulnerability_alerts is False
    assert s.dependabot_security_updates == "disabled"
```

- [ ] **Step 2: Write failing test for private repo**

```python
def test_desired_security_settings_private_skips_ghas_features() -> None:
    s = desired_security_settings(visibility="private")
    assert s.secret_scanning is None
    assert s.secret_scanning_push_protection is None
    assert s.vulnerability_alerts is False
    assert s.dependabot_security_updates == "disabled"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py::test_desired_security_settings_public tests/vergil_tooling/test_github_config_lib.py::test_desired_security_settings_private_skips_ghas_features -v`

Expected: Both fail because `desired_security_settings()` does not accept a `visibility` parameter.

- [ ] **Step 4: Update the dataclass and function**

In `src/vergil_tooling/lib/github_config.py`, change the `DesiredSecuritySettings` dataclass field types (lines 44-48):

```python
@dataclass
class DesiredSecuritySettings:
    secret_scanning: str | None
    secret_scanning_push_protection: str | None
    vulnerability_alerts: bool
    dependabot_security_updates: str
```

Then update `desired_security_settings` (lines 127-133) to accept `visibility`:

```python
def desired_security_settings(*, visibility: str) -> DesiredSecuritySettings:
    ghas_available = visibility != "private"
    return DesiredSecuritySettings(
        secret_scanning="enabled" if ghas_available else None,  # noqa: S106
        secret_scanning_push_protection="enabled" if ghas_available else None,  # noqa: S106
        vulnerability_alerts=False,
        dependabot_security_updates="disabled",
    )
```

- [ ] **Step 5: Update `compute_desired_state` to pass visibility through**

In `src/vergil_tooling/lib/github_config.py`, line 321 currently reads:

```python
security=desired_security_settings(),
```

Change to:

```python
security=desired_security_settings(visibility=visibility),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py::test_desired_security_settings_public tests/vergil_tooling/test_github_config_lib.py::test_desired_security_settings_private_skips_ghas_features -v`

Expected: Both PASS.

- [ ] **Step 7: Fix existing tests that call `desired_security_settings()` without visibility**

The existing `test_desired_security_settings` at line 76 calls `desired_security_settings()` without the new keyword arg. Replace it with the `test_desired_security_settings_public` test from Step 1.

Other tests that reference security settings construct `DesiredSecuritySettings` directly or go through `compute_desired_state`, so they are unaffected.

- [ ] **Step 8: Run the full test suite to check for regressions**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py tests/vergil_tooling/test_vrg_github_repo_config.py -v`

Expected: All tests PASS.

- [ ] **Step 9: Commit**

```
fix(vrg-github-repo-config): make desired_security_settings visibility-aware

Returns None for GHAS-gated features (secret_scanning,
secret_scanning_push_protection) on private repos.
```

---

### Task 2: Add `skipped` field to `ConfigDiff` and populate it in diff engine

**Files:**
- Modify: `src/vergil_tooling/lib/github_config.py:543-566`
- Test: `tests/vergil_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing test for skipped fields on private repo diff**

```python
def test_diff_records_skipped_security_fields_for_private_repo() -> None:
    desired = compute_desired_state(_st_config(), visibility="private", is_org=True)
    actual = compute_desired_state(_st_config(), visibility="public", is_org=True)
    actual.security.secret_scanning = "enabled"
    actual.security.secret_scanning_push_protection = "enabled"
    diff = compute_diff(desired=desired, actual=actual)
    assert diff.is_compliant()
    assert "security.secret_scanning" in diff.skipped
    assert "security.secret_scanning_push_protection" in diff.skipped
```

- [ ] **Step 2: Write failing test that existing diff with no skips has empty skipped list**

```python
def test_diff_identical_states_has_no_skipped() -> None:
    state = compute_desired_state(_st_config(), visibility="public", is_org=True)
    diff = compute_diff(desired=state, actual=state)
    assert diff.is_compliant()
    assert diff.skipped == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py::test_diff_records_skipped_security_fields_for_private_repo tests/vergil_tooling/test_github_config_lib.py::test_diff_identical_states_has_no_skipped -v`

Expected: Both fail — `ConfigDiff` has no `skipped` attribute.

- [ ] **Step 4: Add `skipped` field to `ConfigDiff`**

In `src/vergil_tooling/lib/github_config.py`, update the `ConfigDiff` dataclass (line 543):

```python
@dataclass
class ConfigDiff:
    items: list[DiffItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def is_compliant(self) -> bool:
        return len(self.items) == 0
```

- [ ] **Step 5: Update `_diff_dataclass` to record skipped fields**

Change the signature of `_diff_dataclass` (line 551) to accept a `skipped` list:

```python
def _diff_dataclass(
    prefix: str,
    desired: object,
    actual: object,
    items: list[DiffItem],
    skipped: list[str] | None = None,
) -> None:
    if not hasattr(desired, "__dataclass_fields__"):
        if desired is None:
            if skipped is not None:
                skipped.append(prefix)
            return
        if desired != actual:
            items.append(DiffItem(field=prefix, expected=desired, actual=actual))
        return
    for field_name in cast("dict[str, object]", desired.__dataclass_fields__):
        d_val = getattr(desired, field_name)
        a_val = getattr(actual, field_name)
        _diff_dataclass(f"{prefix}.{field_name}", d_val, a_val, items, skipped)
```

- [ ] **Step 6: Update `compute_diff` to thread the skipped list through**

In `src/vergil_tooling/lib/github_config.py`, update `compute_diff` (line 605):

```python
def compute_diff(*, desired: DesiredState, actual: DesiredState) -> ConfigDiff:
    """Compare desired vs actual state and return structured diff."""
    items: list[DiffItem] = []
    skipped: list[str] = []
    _diff_dataclass("repo_settings", desired.repo_settings, actual.repo_settings, items, skipped)
    _diff_dataclass("security", desired.security, actual.security, items, skipped)
    _diff_dataclass(
        "actions_permissions",
        desired.actions_permissions,
        actual.actions_permissions,
        items,
        skipped,
    )
    _diff_rulesets(desired.rulesets, actual.rulesets, items)
    return ConfigDiff(items=items, skipped=skipped)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py::test_diff_records_skipped_security_fields_for_private_repo tests/vergil_tooling/test_github_config_lib.py::test_diff_identical_states_has_no_skipped -v`

Expected: Both PASS.

- [ ] **Step 8: Run full test suite for regressions**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py tests/vergil_tooling/test_vrg_github_repo_config.py -v`

Expected: All tests PASS. The `skipped` field defaults to `[]` so existing `ConfigDiff()` construction in CLI test helpers is unaffected.

- [ ] **Step 9: Commit**

```
fix(vrg-github-repo-config): record skipped fields in ConfigDiff during diff

_diff_dataclass now appends to a skipped list when the desired
value is None, instead of silently returning.
```

---

### Task 3: Omit `None` fields from security apply PATCH body

**Files:**
- Modify: `src/vergil_tooling/lib/github_config.py:649-668`
- Test: `tests/vergil_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing test for private repo apply (None fields omitted)**

```python
def test_apply_security_settings_skips_none_fields() -> None:
    from vergil_tooling.lib.github_config import DesiredSecuritySettings

    sec = DesiredSecuritySettings(
        secret_scanning=None,
        secret_scanning_push_protection=None,
        vulnerability_alerts=False,
        dependabot_security_updates="disabled",
    )
    with (
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_security_settings("o/r", sec)
    assert mock_write.call_count == 1
    body = mock_write.call_args[0][2]
    sa = body["security_and_analysis"]
    assert "secret_scanning" not in sa
    assert "secret_scanning_push_protection" not in sa
    assert sa["dependabot_security_updates"] == {"status": "disabled"}
    mock_del.assert_called_once_with("repos/o/r/vulnerability-alerts")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py::test_apply_security_settings_skips_none_fields -v`

Expected: FAIL — the current code unconditionally includes all fields.

- [ ] **Step 3: Update `_apply_security_settings` to conditionally include fields**

In `src/vergil_tooling/lib/github_config.py`, replace `_apply_security_settings` (lines 649-668):

```python
def _apply_security_settings(repo: str, security: DesiredSecuritySettings) -> None:
    sa: dict[str, object] = {}
    if security.secret_scanning is not None:
        sa["secret_scanning"] = {"status": security.secret_scanning}
    if security.secret_scanning_push_protection is not None:
        sa["secret_scanning_push_protection"] = {
            "status": security.secret_scanning_push_protection,
        }
    sa["dependabot_security_updates"] = {"status": security.dependabot_security_updates}
    github.write_json(
        "PATCH",
        f"repos/{repo}",
        {"security_and_analysis": sa},
    )
    if security.vulnerability_alerts:
        github.write_json("PUT", f"repos/{repo}/vulnerability-alerts", {})
    else:
        github.delete(f"repos/{repo}/vulnerability-alerts")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py::test_apply_security_settings_skips_none_fields -v`

Expected: PASS.

- [ ] **Step 5: Run existing apply security tests to check for regressions**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github_config_lib.py::test_apply_security_settings_enables_vuln_alerts tests/vergil_tooling/test_github_config_lib.py::test_apply_security_settings_disables_vuln_alerts -v`

Expected: Both PASS — they construct `DesiredSecuritySettings` with string values, so the conditional includes them.

- [ ] **Step 6: Commit**

```
fix(vrg-github-repo-config): omit None security fields from apply PATCH body

Prevents HTTP 422 when applying to private repos without GHAS.
```

---

### Task 4: Render skipped fields in CLI output

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_github_repo_config.py:98-105`
- Test: `tests/vergil_tooling/test_vrg_github_repo_config.py`

- [ ] **Step 1: Write failing test for skipped output in audit mode**

```python
def test_audit_prints_skipped_fields(capsys: pytest.CaptureFixture[str]) -> None:
    diff = ConfigDiff(
        items=[],
        skipped=[
            "security.secret_scanning",
            "security.secret_scanning_push_protection",
        ],
    )
    with (
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=diff),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        result = main(["audit", "--repo", "o/r"])
    assert result == 0
    output = capsys.readouterr().out
    assert "secret_scanning: skipped" in output
    assert "secret_scanning_push_protection: skipped" in output
    assert "requires GitHub Advanced Security" in output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_github_repo_config.py::test_audit_prints_skipped_fields -v`

Expected: FAIL — `_print_diff` does not render skipped fields.

- [ ] **Step 3: Update `_print_diff` to render skipped fields**

In `src/vergil_tooling/bin/vrg_github_repo_config.py`, update `_print_diff` (lines 98-105):

```python
def _print_diff(repo: str, diff: ConfigDiff) -> None:
    """Print GitHub config diff results for a repo."""
    if diff.is_compliant():
        print(f"  {repo}: compliant")
    else:
        print(f"  {repo}: NON-COMPLIANT ({len(diff.items)} issues)")
        for item in diff.items:
            print(f"    {item.field}: expected={item.expected!r}, actual={item.actual!r}")
    for field_name in diff.skipped:
        print(f"    {field_name}: skipped (requires GitHub Advanced Security for private repos)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_github_repo_config.py::test_audit_prints_skipped_fields -v`

Expected: PASS.

- [ ] **Step 5: Write test that compliant public repo has no skipped output**

```python
def test_audit_compliant_public_repo_no_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "o/r"])
    output = capsys.readouterr().out
    assert "skipped" not in output
```

- [ ] **Step 6: Run to verify it passes immediately**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_github_repo_config.py::test_audit_compliant_public_repo_no_skipped -v`

Expected: PASS — `_mock_github_compliant()` returns `ConfigDiff(items=[])`, whose `skipped` defaults to `[]`.

- [ ] **Step 7: Commit**

```
fix(vrg-github-repo-config): render skipped fields in CLI audit/diff output

Shows informational "skipped (requires GitHub Advanced Security)"
messages for GHAS-gated features on private repos.
```

---

### Task 5: Run full validation and verify

**Files:**
- No new files.

- [ ] **Step 1: Run full validation pipeline**

Run: `vrg-container-run -- uv run vrg-validate`

Expected: All checks pass (lint, typecheck, tests, audit).

- [ ] **Step 2: Fix any issues found by validation**

If typecheck or lint failures arise (e.g., from the `str | None` type change), fix them. Likely candidates:
- The `# noqa: S106` comments on the conditional expressions may need repositioning.
- Pyright may flag the `None` comparison in `_apply_security_settings`.

- [ ] **Step 3: Run validation again to confirm clean**

Run: `vrg-container-run -- uv run vrg-validate`

Expected: Clean pass.

- [ ] **Step 4: Commit any lint/type fixes if needed**

```
fix(vrg-github-repo-config): address lint/type findings from visibility gating
```
