# Repo Settings Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 8 new repo-level settings to `st-github-config` so every managed repository converges on the canonical configuration.

**Architecture:** Extend `DesiredRepoSettings` with 8 fields, thread repo `visibility` from the fetch layer through to derivation (for `allow_forking`), and update the PATCH body. No new API calls — all fields come from the existing `repos/{repo}` response.

**Tech Stack:** Python 3.12+, dataclasses, `gh` CLI subprocess wrappers, pytest with unittest.mock

---

### Task 1: Extend DesiredRepoSettings and derivation

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py:22-33` (dataclass)
- Modify: `src/standard_tooling/lib/github_config.py:82-93` (factory)
- Modify: `src/standard_tooling/lib/github_config.py:272-288` (compute)
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing tests for new derivation behavior**

Add to `tests/standard_tooling/test_github_config_lib.py` after the existing `test_desired_repo_settings_are_fixed` (line 51):

```python
def test_desired_repo_settings_public_allows_forking() -> None:
    s = desired_repo_settings(visibility="public")
    assert s.allow_forking is True


def test_desired_repo_settings_private_disallows_forking() -> None:
    s = desired_repo_settings(visibility="private")
    assert s.allow_forking is False


def test_desired_repo_settings_new_hardcoded_values() -> None:
    s = desired_repo_settings(visibility="public")
    assert s.allow_update_branch is True
    assert s.has_downloads is False
    assert s.merge_commit_title == "MERGE_MESSAGE"
    assert s.merge_commit_message == "PR_TITLE"
    assert s.squash_merge_commit_title == "COMMIT_OR_PR_TITLE"
    assert s.squash_merge_commit_message == "COMMIT_MESSAGES"
    assert s.web_commit_signoff_required is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py::test_desired_repo_settings_public_allows_forking tests/standard_tooling/test_github_config_lib.py::test_desired_repo_settings_private_disallows_forking tests/standard_tooling/test_github_config_lib.py::test_desired_repo_settings_new_hardcoded_values -v`

Expected: FAIL — `desired_repo_settings()` does not accept `visibility`

- [ ] **Step 3: Add new fields to DesiredRepoSettings**

In `src/standard_tooling/lib/github_config.py`, replace the dataclass (lines 22–33):

```python
@dataclass
class DesiredRepoSettings:
    default_branch: str
    allow_auto_merge: bool
    delete_branch_on_merge: bool
    allow_merge_commit: bool
    allow_squash_merge: bool
    allow_rebase_merge: bool
    has_issues: bool
    has_projects: bool
    has_wiki: bool
    allow_forking: bool
    allow_update_branch: bool
    has_downloads: bool
    merge_commit_title: str
    merge_commit_message: str
    squash_merge_commit_title: str
    squash_merge_commit_message: str
    web_commit_signoff_required: bool
```

- [ ] **Step 4: Update desired_repo_settings() to accept visibility**

Replace the factory function (lines 82–93):

```python
def desired_repo_settings(*, visibility: str) -> DesiredRepoSettings:
    return DesiredRepoSettings(
        default_branch="develop",
        allow_auto_merge=False,
        delete_branch_on_merge=True,
        allow_merge_commit=True,
        allow_squash_merge=True,
        allow_rebase_merge=True,
        has_issues=True,
        has_projects=True,
        has_wiki=True,
        allow_forking=visibility == "public",
        allow_update_branch=True,
        has_downloads=False,
        merge_commit_title="MERGE_MESSAGE",
        merge_commit_message="PR_TITLE",
        squash_merge_commit_title="COMMIT_OR_PR_TITLE",
        squash_merge_commit_message="COMMIT_MESSAGES",
        web_commit_signoff_required=True,
    )
```

- [ ] **Step 5: Update compute_desired_state() to accept and thread visibility**

Replace the function (lines 272–288):

```python
def compute_desired_state(config: StConfig, *, visibility: str) -> DesiredState:
    """Compute the full desired GitHub configuration from a repo's StConfig."""
    rulesets: list[DesiredRuleset] = []

    if not config.github.skip_rulesets:
        rulesets.append(desired_branch_protection_ruleset())
        rulesets.append(desired_tag_protection_ruleset())

        if config.ci is not None:
            rulesets.append(desired_ci_gates_ruleset(config.project, config.ci))

    return DesiredState(
        repo_settings=desired_repo_settings(visibility=visibility),
        security=desired_security_settings(),
        actions_permissions=desired_actions_permissions(),
        rulesets=rulesets,
    )
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py::test_desired_repo_settings_public_allows_forking tests/standard_tooling/test_github_config_lib.py::test_desired_repo_settings_private_disallows_forking tests/standard_tooling/test_github_config_lib.py::test_desired_repo_settings_new_hardcoded_values -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && git add src/standard_tooling/lib/github_config.py tests/standard_tooling/test_github_config_lib.py && git commit -m "feat(github-config): add 8 new fields to DesiredRepoSettings and derivation"
```

---

### Task 2: Update fetch layer — FetchResult wrapper and new field extraction

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py:60-67` (add FetchResult after DesiredState)
- Modify: `src/standard_tooling/lib/github_config.py:326-452` (fetch_actual_state)
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing tests for fetch changes**

Add to `tests/standard_tooling/test_github_config_lib.py`. First update the imports at the top to include `FetchResult`:

```python
from standard_tooling.lib.github_config import (
    DesiredRuleset,
    FetchResult,
    _apply_actions_permissions,
    ...  # keep all existing imports
)
```

Then add new test functions after the existing fetch tests (after line 654):

```python
def test_fetch_actual_state_returns_fetch_result() -> None:
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "visibility": "public",
        "security_and_analysis": {},
    }

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "all"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        return {}

    with (
        patch(
            "standard_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "standard_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")

    assert isinstance(result, FetchResult)
    assert result.visibility == "public"
    assert result.state.repo_settings.default_branch == "develop"


def test_fetch_actual_state_extracts_new_repo_fields() -> None:
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "allow_auto_merge": False,
        "delete_branch_on_merge": True,
        "allow_merge_commit": True,
        "allow_squash_merge": True,
        "allow_rebase_merge": True,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True,
        "allow_forking": True,
        "allow_update_branch": True,
        "has_downloads": False,
        "merge_commit_title": "MERGE_MESSAGE",
        "merge_commit_message": "PR_TITLE",
        "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
        "squash_merge_commit_message": "COMMIT_MESSAGES",
        "web_commit_signoff_required": True,
        "visibility": "public",
        "security_and_analysis": {},
    }

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "all"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        return {}

    with (
        patch(
            "standard_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "standard_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")

    s = result.state.repo_settings
    assert s.allow_forking is True
    assert s.allow_update_branch is True
    assert s.has_downloads is False
    assert s.merge_commit_title == "MERGE_MESSAGE"
    assert s.merge_commit_message == "PR_TITLE"
    assert s.squash_merge_commit_title == "COMMIT_OR_PR_TITLE"
    assert s.squash_merge_commit_message == "COMMIT_MESSAGES"
    assert s.web_commit_signoff_required is True


def test_fetch_actual_state_defaults_visibility_to_private() -> None:
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "security_and_analysis": {},
    }

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "all"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        return {}

    with (
        patch(
            "standard_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "standard_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")

    assert result.visibility == "private"
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py::test_fetch_actual_state_returns_fetch_result tests/standard_tooling/test_github_config_lib.py::test_fetch_actual_state_extracts_new_repo_fields tests/standard_tooling/test_github_config_lib.py::test_fetch_actual_state_defaults_visibility_to_private -v`

Expected: FAIL — `FetchResult` does not exist, `fetch_actual_state` returns `DesiredState`

- [ ] **Step 3: Add FetchResult dataclass**

In `src/standard_tooling/lib/github_config.py`, add after the `DesiredState` dataclass (after line 67):

```python
@dataclass
class FetchResult:
    state: DesiredState
    visibility: str
```

- [ ] **Step 4: Update fetch_actual_state() to extract new fields and return FetchResult**

Replace `fetch_actual_state` (lines 326–452). The key changes are:
1. Extract `visibility` from `repo_data`
2. Add 8 new fields to the `DesiredRepoSettings` construction
3. Return `FetchResult` wrapping `DesiredState` and `visibility`

```python
def fetch_actual_state(repo: str) -> FetchResult:
    """Fetch the current GitHub configuration for a repo via gh api."""
    repo_data = github.read_json("api", f"repos/{repo}")

    visibility = (
        str(repo_data.get("visibility", "private"))
        if isinstance(repo_data, dict)
        else "private"
    )

    sa_raw = repo_data.get("security_and_analysis") if isinstance(repo_data, dict) else None
    sa: dict[str, object] = cast("dict[str, object]", sa_raw) if isinstance(sa_raw, dict) else {}

    repo_settings = DesiredRepoSettings(
        default_branch=str(repo_data.get("default_branch", ""))
        if isinstance(repo_data, dict)
        else "",
        allow_auto_merge=bool(repo_data.get("allow_auto_merge", False))
        if isinstance(repo_data, dict)
        else False,
        delete_branch_on_merge=bool(repo_data.get("delete_branch_on_merge", False))
        if isinstance(repo_data, dict)
        else False,
        allow_merge_commit=bool(repo_data.get("allow_merge_commit", False))
        if isinstance(repo_data, dict)
        else False,
        allow_squash_merge=bool(repo_data.get("allow_squash_merge", False))
        if isinstance(repo_data, dict)
        else False,
        allow_rebase_merge=bool(repo_data.get("allow_rebase_merge", False))
        if isinstance(repo_data, dict)
        else False,
        has_issues=bool(repo_data.get("has_issues", False))
        if isinstance(repo_data, dict)
        else False,
        has_projects=bool(repo_data.get("has_projects", False))
        if isinstance(repo_data, dict)
        else False,
        has_wiki=bool(repo_data.get("has_wiki", False))
        if isinstance(repo_data, dict)
        else False,
        allow_forking=bool(repo_data.get("allow_forking", False))
        if isinstance(repo_data, dict)
        else False,
        allow_update_branch=bool(repo_data.get("allow_update_branch", False))
        if isinstance(repo_data, dict)
        else False,
        has_downloads=bool(repo_data.get("has_downloads", False))
        if isinstance(repo_data, dict)
        else False,
        merge_commit_title=str(repo_data.get("merge_commit_title", ""))
        if isinstance(repo_data, dict)
        else "",
        merge_commit_message=str(repo_data.get("merge_commit_message", ""))
        if isinstance(repo_data, dict)
        else "",
        squash_merge_commit_title=str(repo_data.get("squash_merge_commit_title", ""))
        if isinstance(repo_data, dict)
        else "",
        squash_merge_commit_message=str(repo_data.get("squash_merge_commit_message", ""))
        if isinstance(repo_data, dict)
        else "",
        web_commit_signoff_required=bool(repo_data.get("web_commit_signoff_required", False))
        if isinstance(repo_data, dict)
        else False,
    )

    ss_raw = sa.get("secret_scanning")
    ss = cast("dict[str, object]", ss_raw) if isinstance(ss_raw, dict) else {}
    ss_status = str(ss.get("status", "disabled"))
    sspp_raw = sa.get("secret_scanning_push_protection")
    sspp = cast("dict[str, object]", sspp_raw) if isinstance(sspp_raw, dict) else {}
    sspp_status = str(sspp.get("status", "disabled"))
    dsu_raw = sa.get("dependabot_security_updates")
    dsu = cast("dict[str, object]", dsu_raw) if isinstance(dsu_raw, dict) else {}
    dsu_status = str(dsu.get("status", "disabled"))

    security = DesiredSecuritySettings(
        secret_scanning=ss_status,
        secret_scanning_push_protection=sspp_status,
        vulnerability_alerts=_fetch_vulnerability_alerts(repo),
        dependabot_security_updates=dsu_status,
    )

    actions_perm = github.read_json("api", f"repos/{repo}/actions/permissions")
    actions_workflow = github.read_json("api", f"repos/{repo}/actions/permissions/workflow")

    patterns: list[str] = []
    allowed_actions = (
        actions_perm.get("allowed_actions") if isinstance(actions_perm, dict) else None
    )
    if allowed_actions == "selected":
        selected = github.read_json("api", f"repos/{repo}/actions/permissions/selected-actions")
        if isinstance(selected, dict):
            raw_patterns = selected.get("patterns_allowed")
            if isinstance(raw_patterns, list):
                patterns = [str(p) for p in raw_patterns]

    actions_permissions = DesiredActionsPermissions(
        default_workflow_permissions=str(actions_workflow.get("default_workflow_permissions", ""))
        if isinstance(actions_workflow, dict)
        else "",
        can_approve_pull_request_reviews=bool(
            actions_workflow.get("can_approve_pull_request_reviews", False)
        )
        if isinstance(actions_workflow, dict)
        else False,
        allowed_actions=str(allowed_actions) if allowed_actions else "",
        patterns_allowed=sorted(patterns),
    )

    raw_rulesets = github.read_json("api", f"repos/{repo}/rulesets")
    rulesets: list[DesiredRuleset] = []
    if isinstance(raw_rulesets, list):
        for raw_rs in raw_rulesets:
            if not isinstance(raw_rs, dict):
                continue
            rs_summary = cast("dict[str, object]", raw_rs)
            rs_id = rs_summary.get("id")
            if rs_id is None:
                continue
            rs_detail = github.read_json("api", f"repos/{repo}/rulesets/{rs_id}")
            if not isinstance(rs_detail, dict):
                continue
            cond_raw = rs_detail.get("conditions")
            conditions: dict[str, object] = (
                cast("dict[str, object]", cond_raw) if isinstance(cond_raw, dict) else {}
            )
            rn_raw = conditions.get("ref_name")
            ref_name: dict[str, object] = (
                cast("dict[str, object]", rn_raw) if isinstance(rn_raw, dict) else {}
            )
            include = ref_name.get("include")
            include = include if isinstance(include, list) else []

            bypass_raw = rs_detail.get("bypass_actors")
            bypass: list[dict[str, object]] = (
                cast("list[dict[str, object]]", bypass_raw) if isinstance(bypass_raw, list) else []
            )
            rules_raw = rs_detail.get("rules")
            rules = _normalize_rules(rules_raw if isinstance(rules_raw, list) else [])

            rulesets.append(
                DesiredRuleset(
                    name=str(rs_detail.get("name", "")),
                    target=str(rs_detail.get("target", "")),
                    enforcement=str(rs_detail.get("enforcement", "")),
                    ref_include=[str(r) for r in include],
                    bypass_actors=bypass,
                    rules=rules,
                )
            )

    return FetchResult(
        state=DesiredState(
            repo_settings=repo_settings,
            security=security,
            actions_permissions=actions_permissions,
            rulesets=rulesets,
        ),
        visibility=visibility,
    )
```

- [ ] **Step 5: Run new tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py::test_fetch_actual_state_returns_fetch_result tests/standard_tooling/test_github_config_lib.py::test_fetch_actual_state_extracts_new_repo_fields tests/standard_tooling/test_github_config_lib.py::test_fetch_actual_state_defaults_visibility_to_private -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && git add src/standard_tooling/lib/github_config.py tests/standard_tooling/test_github_config_lib.py && git commit -m "feat(github-config): add FetchResult wrapper and extract new fields in fetch"
```

---

### Task 3: Update apply layer

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py:547-562` (_apply_repo_settings)
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing test for new fields in PATCH body**

Add to `tests/standard_tooling/test_github_config_lib.py` after the existing `test_apply_repo_settings_calls_write_json` test:

```python
def test_apply_repo_settings_includes_new_fields() -> None:
    settings = desired_repo_settings(visibility="public")
    with patch("standard_tooling.lib.github_config.github.write_json") as mock_write:
        _apply_repo_settings("o/r", settings)
    body = mock_write.call_args[0][2]
    assert body["allow_forking"] is True
    assert body["allow_update_branch"] is True
    assert body["has_downloads"] is False
    assert body["merge_commit_title"] == "MERGE_MESSAGE"
    assert body["merge_commit_message"] == "PR_TITLE"
    assert body["squash_merge_commit_title"] == "COMMIT_OR_PR_TITLE"
    assert body["squash_merge_commit_message"] == "COMMIT_MESSAGES"
    assert body["web_commit_signoff_required"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py::test_apply_repo_settings_includes_new_fields -v`

Expected: FAIL — `allow_forking` not in PATCH body

- [ ] **Step 3: Update _apply_repo_settings to include new fields**

Replace `_apply_repo_settings` (lines 547–562):

```python
def _apply_repo_settings(repo: str, settings: DesiredRepoSettings) -> None:
    github.write_json(
        "PATCH",
        f"repos/{repo}",
        {
            "default_branch": settings.default_branch,
            "allow_auto_merge": settings.allow_auto_merge,
            "delete_branch_on_merge": settings.delete_branch_on_merge,
            "allow_merge_commit": settings.allow_merge_commit,
            "allow_squash_merge": settings.allow_squash_merge,
            "allow_rebase_merge": settings.allow_rebase_merge,
            "has_issues": settings.has_issues,
            "has_projects": settings.has_projects,
            "has_wiki": settings.has_wiki,
            "allow_forking": settings.allow_forking,
            "allow_update_branch": settings.allow_update_branch,
            "has_downloads": settings.has_downloads,
            "merge_commit_title": settings.merge_commit_title,
            "merge_commit_message": settings.merge_commit_message,
            "squash_merge_commit_title": settings.squash_merge_commit_title,
            "squash_merge_commit_message": settings.squash_merge_commit_message,
            "web_commit_signoff_required": settings.web_commit_signoff_required,
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py::test_apply_repo_settings_includes_new_fields -v`

Expected: PASS

- [ ] **Step 5: Add drift detection test for new fields**

Add to `tests/standard_tooling/test_github_config_lib.py` after the diff tests:

```python
def test_diff_detects_new_repo_setting_drift() -> None:
    desired = compute_desired_state(_st_config(), visibility="public")
    actual = compute_desired_state(_st_config(), visibility="public")
    actual.repo_settings.merge_commit_title = "PR_TITLE"
    actual.repo_settings.web_commit_signoff_required = False
    diff = compute_diff(desired=desired, actual=actual)
    fields = {item.field for item in diff.items}
    assert "repo_settings.merge_commit_title" in fields
    assert "repo_settings.web_commit_signoff_required" in fields
```

- [ ] **Step 6: Run drift test to verify it passes**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py::test_diff_detects_new_repo_setting_drift -v`

Expected: PASS (the diff mechanism is generic — new fields are picked up automatically once they exist on the dataclass)

- [ ] **Step 7: Commit**

```bash
cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && git add src/standard_tooling/lib/github_config.py tests/standard_tooling/test_github_config_lib.py && git commit -m "feat(github-config): include new fields in repo settings PATCH body"
```

---

### Task 4: Update CLI plumbing

**Files:**
- Modify: `src/standard_tooling/bin/st_github_config.py:84-88` (_audit_repo)
- Modify: `src/standard_tooling/bin/st_github_config.py:101-104` (_apply_repo)
- Test: `tests/standard_tooling/test_st_github_config.py`

- [ ] **Step 1: Write failing tests for CLI plumbing changes**

In `tests/standard_tooling/test_st_github_config.py`, replace `test_audit_repo_calls_compute_and_diff` (lines 270–288) and `test_apply_repo_calls_apply_desired_state` (lines 294–302):

```python
def test_audit_repo_calls_compute_and_diff() -> None:
    cfg = _make_config()
    with (
        patch(
            "standard_tooling.bin.st_github_config.fetch_actual_state",
        ) as mock_fetch,
        patch(
            "standard_tooling.bin.st_github_config.compute_desired_state",
        ) as mock_desired,
        patch(
            "standard_tooling.bin.st_github_config.compute_diff",
            return_value=ConfigDiff(items=[]),
        ) as mock_diff,
    ):
        result = _audit_repo("o/r", cfg)
    mock_fetch.assert_called_once_with("o/r")
    mock_desired.assert_called_once_with(cfg, visibility=mock_fetch.return_value.visibility)
    mock_diff.assert_called_once_with(
        desired=mock_desired.return_value,
        actual=mock_fetch.return_value.state,
    )
    assert result.is_compliant()


def test_apply_repo_calls_apply_desired_state() -> None:
    cfg = _make_config()
    with (
        patch("standard_tooling.bin.st_github_config.fetch_actual_state") as mock_fetch,
        patch("standard_tooling.bin.st_github_config.compute_desired_state") as mock_desired,
        patch("standard_tooling.bin.st_github_config.apply_desired_state") as mock_apply,
    ):
        _apply_repo("o/r", cfg)
    mock_fetch.assert_called_once_with("o/r")
    mock_desired.assert_called_once_with(cfg, visibility=mock_fetch.return_value.visibility)
    mock_apply.assert_called_once_with("o/r", mock_desired.return_value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_st_github_config.py::test_audit_repo_calls_compute_and_diff tests/standard_tooling/test_st_github_config.py::test_apply_repo_calls_apply_desired_state -v`

Expected: FAIL — `_audit_repo` calls `compute_desired_state(cfg)` without visibility

- [ ] **Step 3: Update _audit_repo to thread visibility via FetchResult**

In `src/standard_tooling/bin/st_github_config.py`, replace `_audit_repo` (lines 84–88):

```python
def _audit_repo(repo: str, config: StConfig) -> ConfigDiff:
    """Compute diff between desired and actual state for a repo."""
    result = fetch_actual_state(repo)
    desired = compute_desired_state(config, visibility=result.visibility)
    return compute_diff(desired=desired, actual=result.state)
```

- [ ] **Step 4: Update _apply_repo to thread visibility via FetchResult**

Replace `_apply_repo` (lines 101–104):

```python
def _apply_repo(repo: str, config: StConfig) -> list[str]:
    """Apply desired state to a repo. Returns branches with legacy protection removed."""
    result = fetch_actual_state(repo)
    desired = compute_desired_state(config, visibility=result.visibility)
    return apply_desired_state(repo, desired)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_st_github_config.py::test_audit_repo_calls_compute_and_diff tests/standard_tooling/test_st_github_config.py::test_apply_repo_calls_apply_desired_state -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && git add src/standard_tooling/bin/st_github_config.py tests/standard_tooling/test_st_github_config.py && git commit -m "feat(github-config): thread visibility from fetch through CLI plumbing"
```

---

### Task 5: Fix all broken existing tests in test_github_config_lib.py

The signature changes break many existing tests. This task fixes all of them.

**Files:**
- Modify: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Fix test_desired_repo_settings_are_fixed**

Update the test (line 40) to pass `visibility`:

```python
def test_desired_repo_settings_are_fixed() -> None:
    s = desired_repo_settings(visibility="public")
    assert s.default_branch == "develop"
    assert s.allow_auto_merge is False
    assert s.delete_branch_on_merge is True
    assert s.allow_merge_commit is True
    assert s.allow_squash_merge is True
    assert s.allow_rebase_merge is True
    assert s.has_issues is True
    assert s.has_projects is True
    assert s.has_wiki is True
```

- [ ] **Step 2: Fix all compute_desired_state callers**

Every call to `compute_desired_state(_st_config())` needs `visibility="public"` added. Update these tests:

`test_compute_desired_state_has_three_rulesets` (line 265):
```python
state = compute_desired_state(_st_config(), visibility="public")
```

`test_compute_desired_state_skip_rulesets` (line 274):
```python
state = compute_desired_state(_st_config(skip_rulesets=True), visibility="public")
```

`test_compute_desired_state_no_ci_section` (line 279):
```python
cfg = _st_config()
cfg.ci = None
state = compute_desired_state(cfg, visibility="public")
```

`test_compute_desired_state_includes_repo_settings` (line 288):
```python
state = compute_desired_state(_st_config(), visibility="public")
```

`test_compute_desired_state_includes_security` (line 293):
```python
state = compute_desired_state(_st_config(), visibility="public")
```

`test_compute_desired_state_includes_actions` (line 298):
```python
state = compute_desired_state(_st_config(), visibility="public")
```

- [ ] **Step 3: Fix all diff test callers**

Same pattern — add `visibility="public"`:

`test_diff_identical_states_is_empty` (line 678):
```python
state = compute_desired_state(_st_config(), visibility="public")
```

`test_diff_detects_repo_setting_mismatch` (line 685):
```python
desired = compute_desired_state(_st_config(), visibility="public")
actual = compute_desired_state(_st_config(), visibility="public")
```

`test_diff_detects_missing_ruleset` (line 694):
```python
desired = compute_desired_state(_st_config(), visibility="public")
actual = compute_desired_state(_st_config(), visibility="public")
```

`test_diff_detects_extra_ruleset` (line 703):
```python
desired = compute_desired_state(_st_config(), visibility="public")
actual = compute_desired_state(_st_config(), visibility="public")
```

`test_diff_detects_actions_permission_mismatch` (line 721):
```python
desired = compute_desired_state(_st_config(), visibility="public")
actual = compute_desired_state(_st_config(), visibility="public")
```

`test_diff_detects_security_mismatch` (line 730):
```python
desired = compute_desired_state(_st_config(), visibility="public")
actual = compute_desired_state(_st_config(), visibility="public")
```

- [ ] **Step 4: Fix apply test callers**

`test_apply_repo_settings_calls_write_json` (line 744):
```python
settings = desired_repo_settings(visibility="public")
```

`test_apply_desired_state_orchestrates_all` (line 924):
```python
state = compute_desired_state(_st_config(), visibility="public")
```

- [ ] **Step 5: Fix all fetch_actual_state tests for FetchResult return type**

These tests assign `actual = fetch_actual_state("o/r")`. Since `fetch_actual_state` now returns `FetchResult`, update them to use `result.state`:

`test_fetch_actual_state_repo_settings` (line 308): Change `actual = fetch_actual_state("o/r")` to `result = fetch_actual_state("o/r")`, then add `actual = result.state` on the next line. Also add `visibility` and the 8 new fields to `repo_json`:

```python
def test_fetch_actual_state_repo_settings() -> None:
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "allow_auto_merge": False,
        "delete_branch_on_merge": True,
        "allow_merge_commit": True,
        "allow_squash_merge": True,
        "allow_rebase_merge": True,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True,
        "allow_forking": True,
        "allow_update_branch": False,
        "has_downloads": True,
        "merge_commit_title": "MERGE_MESSAGE",
        "merge_commit_message": "PR_TITLE",
        "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
        "squash_merge_commit_message": "COMMIT_MESSAGES",
        "web_commit_signoff_required": False,
        "visibility": "public",
        "security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
            "dependabot_security_updates": {"status": "disabled"},
        },
    }
    ...  # mock_read_json stays the same
    with (...):
        result = fetch_actual_state("o/r")

    actual = result.state
    assert actual.repo_settings.default_branch == "develop"
    assert actual.repo_settings.delete_branch_on_merge is True
    assert actual.security.secret_scanning == "enabled"  # noqa: S105
    assert actual.security.vulnerability_alerts is False
    assert actual.actions_permissions.default_workflow_permissions == "read"
    assert actual.actions_permissions.patterns_allowed == [
        "actions/*",
        "wphillipmoore/*",
    ]
    assert actual.rulesets == []
```

Apply the same `result = fetch_actual_state(...)` / `actual = result.state` pattern to:
- `test_fetch_actual_state_with_rulesets` (line 367)
- `test_fetch_actual_state_no_selected_actions_skips_patterns` (line 416)
- `test_fetch_actual_state_missing_security_and_analysis` (line 455)
- `test_fetch_actual_state_rulesets_edge_cases` (line 494)
- `test_fetch_actual_state_rulesets_not_a_list` (line 541)
- `test_fetch_actual_state_selected_actions_non_dict_response` (line 578)
- `test_fetch_actual_state_selected_actions_non_list_patterns` (line 618)

For each, change `actual = fetch_actual_state("o/r")` to:
```python
result = fetch_actual_state("o/r")
actual = result.state
```

Leave the rest of the assertions unchanged — they reference `actual.rulesets`, `actual.security`, etc. which still work via `result.state`.

- [ ] **Step 6: Run the full lib test file**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_github_config_lib.py -v`

Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && git add tests/standard_tooling/test_github_config_lib.py && git commit -m "test(github-config): update lib tests for new fields and FetchResult"
```

---

### Task 6: Fix remaining broken CLI tests

**Files:**
- Modify: `tests/standard_tooling/test_st_github_config.py`

- [ ] **Step 1: Run CLI test file to identify remaining failures**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && uv run pytest tests/standard_tooling/test_st_github_config.py -v`

The CLI tests that mock `_audit_repo` and `_apply_repo` at the function boundary should still pass — they don't call the internals. But `test_audit_repo_calls_compute_and_diff` and `test_apply_repo_calls_apply_desired_state` were already fixed in Task 4. Verify all pass.

Expected: ALL PASS (if any fail, fix them following the same patterns from Task 5)

- [ ] **Step 2: Commit if any changes were needed**

```bash
cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && git add tests/standard_tooling/test_st_github_config.py && git commit -m "test(github-config): update CLI tests for visibility threading"
```

---

### Task 7: Full validation

**Files:** None (validation only)

- [ ] **Step 1: Run full validation pipeline**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && st-docker-run -- uv run st-validate`

Expected: All checks pass (lint, typecheck, tests, audit, common checks)

- [ ] **Step 2: Fix any issues found**

If validation reveals issues (formatting, type errors, lint warnings), fix them following the specific error messages.

- [ ] **Step 3: Final commit if fixes were needed**

```bash
cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-610-repo-settings-coverage && git add -A && git commit -m "fix(github-config): address validation issues"
```
