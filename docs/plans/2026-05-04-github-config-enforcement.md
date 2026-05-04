# GitHub Configuration Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centrally enforce GitHub configuration across all managed
repos via a derivation engine that computes desired state from
`standard-tooling.toml`, with tooling to audit, diff, and apply
corrections.

**Architecture:** Layered configuration model — central defaults
(in code) + per-repo identity (`standard-tooling.toml` `[project]`
+ `[ci]`) + rare overrides (`[github]`) = desired state. A
derivation engine computes the full GitHub config; CLI wraps it in
audit/diff/apply modes. All `gh api` calls go through `lib/github.py`.

**Tech Stack:** Python 3.12+, `gh` CLI (subprocess), TOML
(`tomllib`), pytest, mypy.

**Spec:**
`docs/specs/2026-05-04-github-config-enforcement-design.md`

**Issue:**
https://github.com/wphillipmoore/standard-tooling/issues/173

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `src/standard_tooling/lib/github_config.py` | Derivation engine: compute desired state from `StConfig`, compare against actual, produce diffs |
| `src/standard_tooling/bin/github_config.py` | CLI entry point: `st-github-config audit\|diff\|apply` with `--repo` / `--owner --project` targeting |
| `src/standard_tooling/bin/validate.py` | Host-side validation orchestrator: `st-validate` with version matrix, check filtering |
| `src/standard_tooling/lib/validate_commands.py` | Per-language command registry (lint/typecheck/test/audit per language) |
| `src/standard_tooling/bin/validate_common.py` | Container-local common checks (replaces `validate_local_common_container.py`) |
| `tests/standard_tooling/test_github_config_lib.py` | Tests for derivation engine |
| `tests/standard_tooling/test_github_config_cli.py` | Tests for CLI entry point |
| `tests/standard_tooling/test_validate.py` | Tests for st-validate orchestrator |
| `tests/standard_tooling/test_validate_commands.py` | Tests for per-language command registry |
| `tests/standard_tooling/test_validate_common.py` | Tests for common validator |

### Modified files

| File | Change |
|---|---|
| `src/standard_tooling/lib/config.py` | Add `CiConfig` and `GithubOverrides` dataclasses; extend `StConfig`; extract `_parse_raw_config()`; add `[ci]` and `[github]` parsing |
| `src/standard_tooling/lib/github.py` | Add `read_json()` wrapper for `gh api` calls that return JSON |
| `src/standard_tooling/bin/validate_local.py` | Rewrite as deprecation wrapper delegating to `st-validate` |
| `src/standard_tooling/bin/validate_local_lang.py` | Rewrite as deprecation wrapper |
| `src/standard_tooling/bin/validate_local_common_container.py` | Rewrite as deprecation wrapper |
| `pyproject.toml` | Add `st-github-config`, `st-validate`, `st-validate-common` entry points |
| `tests/standard_tooling/test_config.py` | Tests for new TOML sections |
| `tests/standard_tooling/test_github.py` | Tests for `read_json()` |

### Files removed (Phase 13)

| File | Reason |
|---|---|
| `src/standard_tooling/bin/validate_local.py` | Replaced by `validate.py` |
| `src/standard_tooling/bin/validate_local_lang.py` | Replaced by `validate_commands.py` |
| `src/standard_tooling/bin/validate_local_common_container.py` | Replaced by `validate_common.py` |
| `src/standard_tooling/bin/docker_test.py` | Subsumed by `st-validate` calling `st-docker-run` |
| `scripts/dev/lint.sh` | Replaced by command registry |
| `scripts/dev/typecheck.sh` | Replaced by command registry |
| `scripts/dev/test.sh` | Replaced by command registry |
| `scripts/dev/audit.sh` | Replaced by command registry |

---

## Phase 1: TOML Schema Extension

### Task 1: Add `CiConfig` dataclass and `[ci]` parsing

**Files:**
- Modify: `src/standard_tooling/lib/config.py`
- Test: `tests/standard_tooling/test_config.py`

- [ ] **Step 1: Write failing tests for `[ci]` section parsing**

Add to `tests/standard_tooling/test_config.py`:

```python
# -- [ci] section --------------------------------------------------------------

_CI_TOML = (
    _VALID_TOML
    + """
[ci]
versions = ["3.12", "3.13", "3.14"]
integration-tests = true
"""
)


def test_read_config_ci_section(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_CI_TOML)
    cfg = read_config(tmp_path)
    assert cfg.ci.versions == ["3.12", "3.13", "3.14"]
    assert cfg.ci.integration_tests is True


def test_read_config_ci_no_integration_tests(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nversions = [\"3.14\"]\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.ci.integration_tests is False


def test_read_config_ci_missing_versions(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nintegration-tests = true\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions"):
        read_config(tmp_path)


def test_read_config_ci_empty_versions(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nversions = []\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions.*at least one"):
        read_config(tmp_path)


def test_read_config_ci_versions_not_strings(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nversions = [3.12, 3.13]\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions.*strings"):
        read_config(tmp_path)


def test_read_config_no_ci_section(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.ci is None
```

Update the import at the top to include `CiConfig`:

```python
from standard_tooling.lib.config import (
    CiConfig,
    ConfigError,
    MarkdownlintConfig,
    read_config,
    st_install_tag,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_config.py -v -k "ci" --no-header`

Expected: ImportError for `CiConfig`, then AttributeError for `cfg.ci`.

- [ ] **Step 3: Implement `CiConfig` and `[ci]` parsing**

In `src/standard_tooling/lib/config.py`, add the dataclass after
`MarkdownlintConfig`:

```python
@dataclass
class CiConfig:
    versions: list[str]
    integration_tests: bool
```

Add `ci` field to `StConfig`:

```python
@dataclass
class StConfig:
    project: ProjectConfig
    dependencies: dict[str, str]
    markdownlint: MarkdownlintConfig
    ci: CiConfig | None
```

In `read_config()`, after the markdownlint parsing block, add:

```python
ci_raw = raw.get("ci")
ci: CiConfig | None = None
if ci_raw is not None:
    versions = ci_raw.get("versions")
    if versions is None:
        msg = f"{CONFIG_FILE}: [ci] missing required field 'versions'"
        raise ConfigError(msg)
    if not isinstance(versions, list) or not versions:
        msg = f"{CONFIG_FILE}: [ci].versions must be a list with at least one entry"
        raise ConfigError(msg)
    if not all(isinstance(v, str) for v in versions):
        msg = f"{CONFIG_FILE}: [ci].versions entries must be strings"
        raise ConfigError(msg)
    ci = CiConfig(
        versions=versions,
        integration_tests=bool(ci_raw.get("integration-tests", False)),
    )
```

Update the `return` statement to include `ci=ci`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_config.py -v --no-header`

Expected: All tests pass including the new `ci` tests and all
existing tests (the new `ci=None` default must not break them).

- [ ] **Step 5: Commit**

```
feat(config): add [ci] section to standard-tooling.toml schema

Adds CiConfig dataclass with versions (required list of language
version strings) and integration-tests (optional bool). The [ci]
section is optional for backward compatibility during rollout.

Ref #173
```

### Task 2: Add `[github]` override section parsing

**Files:**
- Modify: `src/standard_tooling/lib/config.py`
- Test: `tests/standard_tooling/test_config.py`

- [ ] **Step 1: Write failing tests for `[github]` section**

```python
# -- [github] section ---------------------------------------------------------

_GITHUB_OVERRIDE_TOML = (
    _VALID_TOML
    + """
[github]
skip-rulesets = true
"""
)


def test_read_config_github_overrides(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_GITHUB_OVERRIDE_TOML)
    cfg = read_config(tmp_path)
    assert cfg.github.skip_rulesets is True


def test_read_config_no_github_section(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.github.skip_rulesets is False
```

Update the import to include `GithubOverrides`:

```python
from standard_tooling.lib.config import (
    CiConfig,
    ConfigError,
    GithubOverrides,
    MarkdownlintConfig,
    read_config,
    st_install_tag,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_config.py -v -k "github" --no-header`

Expected: ImportError for `GithubOverrides`.

- [ ] **Step 3: Implement `GithubOverrides` and parsing**

In `config.py`, add dataclass:

```python
@dataclass
class GithubOverrides:
    skip_rulesets: bool
```

Add to `StConfig`:

```python
@dataclass
class StConfig:
    project: ProjectConfig
    dependencies: dict[str, str]
    markdownlint: MarkdownlintConfig
    ci: CiConfig | None
    github: GithubOverrides
```

In `read_config()`, after the ci parsing block:

```python
github_raw = raw.get("github", {})
github_overrides = GithubOverrides(
    skip_rulesets=bool(github_raw.get("skip-rulesets", False)),
)
```

Update the return statement to include `github=github_overrides`.

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_config.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(config): add [github] override section to TOML schema

Adds GithubOverrides dataclass with skip-rulesets flag for repos
that intentionally deviate from the standard (e.g.,
mq-rest-admin-template). Defaults to false — overrides are rare
and their presence is a signal for review.

Ref #173
```

---

## Phase 2: `gh api` JSON Helper

### Task 3: Add `read_json()` to `lib/github.py`

**Files:**
- Modify: `src/standard_tooling/lib/github.py`
- Test: `tests/standard_tooling/test_github.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/standard_tooling/test_github.py`:

```python
import json


def test_read_json_returns_parsed_dict() -> None:
    payload = {"name": "test", "value": 42}
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r")
    assert result == payload


def test_read_json_returns_parsed_list() -> None:
    payload = [{"id": 1}, {"id": 2}]
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r/rulesets")
    assert result == payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github.py -v -k "read_json" --no-header`

Expected: AttributeError — `read_json` does not exist.

- [ ] **Step 3: Implement `read_json()`**

Add to `src/standard_tooling/lib/github.py`:

```python
import json

def read_json(*args: str) -> dict | list:
    """Run a gh command and return parsed JSON from stdout."""
    raw = read_output(*args)
    return json.loads(raw)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(github): add read_json() helper for gh api calls

Wraps read_output() with JSON parsing. Used by the upcoming
st-github-config tool for all GitHub API interactions.

Ref #173
```

---

## Phase 3: Derivation Engine

### Task 4: Desired state data model

**Files:**
- Create: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing test for desired state construction**

Create `tests/standard_tooling/test_github_config_lib.py`:

```python
"""Tests for standard_tooling.lib.github_config."""

from __future__ import annotations

from standard_tooling.lib.github_config import (
    DesiredRepoSettings,
    DesiredRuleset,
    DesiredState,
    desired_repo_settings,
)


def test_desired_repo_settings_are_fixed() -> None:
    s = desired_repo_settings()
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

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement desired state data model**

Create `src/standard_tooling/lib/github_config.py`:

```python
"""GitHub configuration derivation engine.

Computes the desired GitHub configuration for a repository from its
``standard-tooling.toml`` identity.  The desired state can be compared
against the actual GitHub API state to produce audit diffs.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


@dataclass
class DesiredSecuritySettings:
    secret_scanning: str
    secret_scanning_push_protection: str
    vulnerability_alerts: bool
    dependabot_security_updates: str


@dataclass
class DesiredActionsPermissions:
    default_workflow_permissions: str
    can_approve_pull_request_reviews: bool
    allowed_actions: str
    patterns_allowed: list[str]


@dataclass
class DesiredRuleset:
    name: str
    target: str
    enforcement: str
    ref_include: list[str]
    bypass_actors: list[dict]
    rules: list[dict]


@dataclass
class DesiredState:
    repo_settings: DesiredRepoSettings
    security: DesiredSecuritySettings
    actions_permissions: DesiredActionsPermissions
    rulesets: list[DesiredRuleset]


def desired_repo_settings() -> DesiredRepoSettings:
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
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: PASS.

- [ ] **Step 5: Commit**

```
feat(github-config): add desired state data model

Dataclasses for DesiredRepoSettings, DesiredSecuritySettings,
DesiredActionsPermissions, DesiredRuleset, and DesiredState.
These represent the canonical GitHub configuration that the
derivation engine computes and the CLI enforces.

Ref #173
```

### Task 5: Fixed desired state functions (security, actions, branch/tag rulesets)

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/standard_tooling/test_github_config_lib.py`:

```python
from standard_tooling.lib.github_config import (
    DesiredRepoSettings,
    DesiredRuleset,
    DesiredState,
    desired_actions_permissions,
    desired_branch_protection_ruleset,
    desired_repo_settings,
    desired_security_settings,
    desired_tag_protection_ruleset,
)


def test_desired_security_settings() -> None:
    s = desired_security_settings()
    assert s.secret_scanning == "enabled"
    assert s.secret_scanning_push_protection == "enabled"
    assert s.vulnerability_alerts is False
    assert s.dependabot_security_updates == "disabled"


def test_desired_actions_permissions() -> None:
    a = desired_actions_permissions()
    assert a.default_workflow_permissions == "read"
    assert a.can_approve_pull_request_reviews is False
    assert a.allowed_actions == "selected"
    assert "wphillipmoore/*" in a.patterns_allowed
    assert "actions/*" in a.patterns_allowed


def test_branch_protection_ruleset() -> None:
    r = desired_branch_protection_ruleset()
    assert r.name == "Branch protection"
    assert r.target == "branch"
    assert r.enforcement == "active"
    assert r.ref_include == ["refs/heads/main", "refs/heads/develop"]
    assert r.bypass_actors == []
    rule_types = [rule["type"] for rule in r.rules]
    assert "deletion" in rule_types
    assert "non_fast_forward" in rule_types
    assert "pull_request" in rule_types


def test_branch_protection_pr_rule_details() -> None:
    r = desired_branch_protection_ruleset()
    pr_rule = next(rule for rule in r.rules if rule["type"] == "pull_request")
    params = pr_rule["parameters"]
    assert params["required_approving_review_count"] == 0
    assert params["dismiss_stale_reviews_on_push"] is True
    assert params["require_code_owner_review"] is False


def test_tag_protection_ruleset() -> None:
    r = desired_tag_protection_ruleset()
    assert r.name == "Tag protection"
    assert r.target == "tag"
    assert r.ref_include == ["refs/tags/v*.*.*"]
    assert len(r.bypass_actors) == 1
    assert r.bypass_actors[0]["actor_type"] == "RepositoryRole"
    assert r.bypass_actors[0]["actor_id"] == 5
    rule_types = [rule["type"] for rule in r.rules]
    assert "deletion" in rule_types
    assert "non_fast_forward" in rule_types
    assert "update" in rule_types
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: ImportError for the new function names.

- [ ] **Step 3: Implement the four fixed desired state functions**

Add to `src/standard_tooling/lib/github_config.py`:

```python
_ALLOWED_ACTION_PATTERNS = [
    "actions/*",
    "actions-rust-lang/*",
    "astral-sh/*",
    "docker/*",
    "github/*",
    "pypa/*",
    "ruby/*",
    "wphillipmoore/*",
]


def desired_security_settings() -> DesiredSecuritySettings:
    return DesiredSecuritySettings(
        secret_scanning="enabled",
        secret_scanning_push_protection="enabled",
        vulnerability_alerts=False,
        dependabot_security_updates="disabled",
    )


def desired_actions_permissions() -> DesiredActionsPermissions:
    return DesiredActionsPermissions(
        default_workflow_permissions="read",
        can_approve_pull_request_reviews=False,
        allowed_actions="selected",
        patterns_allowed=list(_ALLOWED_ACTION_PATTERNS),
    )


def desired_branch_protection_ruleset() -> DesiredRuleset:
    return DesiredRuleset(
        name="Branch protection",
        target="branch",
        enforcement="active",
        ref_include=["refs/heads/main", "refs/heads/develop"],
        bypass_actors=[],
        rules=[
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "required_reviewers": [],
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                    "allowed_merge_methods": ["merge", "squash", "rebase"],
                },
            },
        ],
    )


def desired_tag_protection_ruleset() -> DesiredRuleset:
    return DesiredRuleset(
        name="Tag protection",
        target="tag",
        enforcement="active",
        ref_include=["refs/tags/v*.*.*"],
        bypass_actors=[
            {
                "actor_id": 5,
                "actor_type": "RepositoryRole",
                "bypass_mode": "always",
            },
        ],
        rules=[
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "update"},
        ],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(github-config): add fixed desired state functions

Implement desired_security_settings(), desired_actions_permissions(),
desired_branch_protection_ruleset(), and
desired_tag_protection_ruleset(). These return the canonical config
that is uniform across all repos.

Ref #173
```

### Task 6: CI gates ruleset derivation

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

This is the derived ruleset whose check list depends on repo
identity. The canonical check name registry will be defined in a
separate design cycle (see Dependency Gate above). This task
implements the derivation structure using the check names observed
in the current fleet audit. When the registry is finalized, these
names are updated to match (a targeted find-and-replace in this
module and its tests).

- [ ] **Step 1: Write failing tests for CI gates derivation**

Add to `tests/standard_tooling/test_github_config_lib.py`:

```python
from standard_tooling.lib.config import CiConfig, ProjectConfig
from standard_tooling.lib.github_config import desired_ci_gates_ruleset


def _project(
    *,
    language: str = "python",
    release_model: str = "tagged-release",
) -> ProjectConfig:
    return ProjectConfig(
        repository_type="library",
        versioning_scheme="semver",
        branching_model="library-release",
        release_model=release_model,
        primary_language=language,
        co_authors={},
    )


def _ci(
    *,
    versions: list[str] | None = None,
    integration_tests: bool = False,
) -> CiConfig:
    return CiConfig(
        versions=versions or ["3.14"],
        integration_tests=integration_tests,
    )


def test_ci_gates_structure() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    assert r.name == "CI gates"
    assert r.target == "branch"
    assert r.enforcement == "active"
    assert r.ref_include == ["refs/heads/main", "refs/heads/develop"]
    assert r.bypass_actors == []


def test_ci_gates_strict_policy() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    status_rule = next(
        rule for rule in r.rules
        if rule["type"] == "required_status_checks"
    )
    assert status_rule["parameters"]["strict_required_status_checks_policy"] is True


def test_ci_gates_always_includes_standards_and_security() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    check_names = _check_names(r)
    assert "security-and-standards / ci: standards-compliance" in check_names
    assert "security-and-standards / security: trivy" in check_names
    assert "security-and-standards / security: semgrep" in check_names


def test_ci_gates_codeql_for_supported_language() -> None:
    r = desired_ci_gates_ruleset(_project(language="python"), _ci())
    assert "security-and-standards / security: codeql" in _check_names(r)


def test_ci_gates_no_codeql_for_shell() -> None:
    r = desired_ci_gates_ruleset(_project(language="shell"), _ci())
    assert "security-and-standards / security: codeql" not in _check_names(r)


def test_ci_gates_unit_tests_per_version() -> None:
    ci = _ci(versions=["3.12", "3.13", "3.14"])
    r = desired_ci_gates_ruleset(_project(), ci)
    names = _check_names(r)
    assert "test: unit (3.12)" in names
    assert "test: unit (3.13)" in names
    assert "test: unit (3.14)" in names


def test_ci_gates_integration_tests_when_enabled() -> None:
    ci = _ci(versions=["3.12", "3.13"], integration_tests=True)
    r = desired_ci_gates_ruleset(_project(), ci)
    names = _check_names(r)
    assert "test: integration (3.12)" in names
    assert "test: integration (3.13)" in names


def test_ci_gates_no_integration_tests_when_disabled() -> None:
    ci = _ci(versions=["3.12"], integration_tests=False)
    r = desired_ci_gates_ruleset(_project(), ci)
    names = _check_names(r)
    assert not any(n.startswith("test: integration") for n in names)


def test_ci_gates_release_gates_present() -> None:
    r = desired_ci_gates_ruleset(
        _project(release_model="tagged-release"), _ci()
    )
    assert "release: gates" in _check_names(r)


def test_ci_gates_no_release_gates_when_none() -> None:
    r = desired_ci_gates_ruleset(
        _project(release_model="none"), _ci()
    )
    assert "release: gates" not in _check_names(r)


def test_ci_gates_dependency_audit_present() -> None:
    r = desired_ci_gates_ruleset(_project(language="python"), _ci())
    assert "ci: dependency-audit" in _check_names(r)


def _check_names(ruleset: DesiredRuleset) -> list[str]:
    """Extract check context names from a CI gates ruleset."""
    status_rule = next(
        rule for rule in ruleset.rules
        if rule["type"] == "required_status_checks"
    )
    return [
        c["context"]
        for c in status_rule["parameters"]["required_status_checks"]
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v -k "ci_gates" --no-header`

Expected: ImportError for `desired_ci_gates_ruleset`.

- [ ] **Step 3: Implement CI gates derivation**

Add to `src/standard_tooling/lib/github_config.py`:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from standard_tooling.lib.config import CiConfig, ProjectConfig

_GITHUB_ACTIONS_INTEGRATION_ID = 15368

_CODEQL_SUPPORTED_LANGUAGES = frozenset({
    "python", "go", "java", "ruby", "rust",
})

_DEPENDENCY_AUDIT_LANGUAGES = frozenset({
    "python", "go", "java", "ruby", "rust",
})


def _make_check(context: str) -> dict:
    return {
        "context": context,
        "integration_id": _GITHUB_ACTIONS_INTEGRATION_ID,
    }


def desired_ci_gates_ruleset(
    project: ProjectConfig,
    ci: CiConfig,
) -> DesiredRuleset:
    checks: list[dict] = []

    # Always present: standards compliance + security
    checks.append(_make_check("security-and-standards / ci: standards-compliance"))
    checks.append(_make_check("security-and-standards / security: trivy"))
    checks.append(_make_check("security-and-standards / security: semgrep"))

    if project.primary_language in _CODEQL_SUPPORTED_LANGUAGES:
        checks.append(_make_check("security-and-standards / security: codeql"))

    # Dependency audit
    if project.primary_language in _DEPENDENCY_AUDIT_LANGUAGES:
        checks.append(_make_check("ci: dependency-audit"))

    # Release gates
    if project.release_model != "none":
        checks.append(_make_check("release: gates"))

    # Unit tests per version
    for version in ci.versions:
        checks.append(_make_check(f"test: unit ({version})"))

    # Integration tests per version
    if ci.integration_tests:
        for version in ci.versions:
            checks.append(_make_check(f"test: integration ({version})"))

    return DesiredRuleset(
        name="CI gates",
        target="branch",
        enforcement="active",
        ref_include=["refs/heads/main", "refs/heads/develop"],
        bypass_actors=[],
        rules=[
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": checks,
                },
            },
        ],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(github-config): add CI gates ruleset derivation

Computes required status checks from project identity and CI
config. Check names follow current fleet conventions; exact names
are subject to the canonical check name registry (spec step 1).

Ref #173
```

### Task 7: Full desired state computation

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing test**

```python
from standard_tooling.lib.config import (
    CiConfig,
    GithubOverrides,
    MarkdownlintConfig,
    ProjectConfig,
    StConfig,
)
from standard_tooling.lib.github_config import compute_desired_state


def _st_config(
    *,
    language: str = "python",
    release_model: str = "tagged-release",
    versions: list[str] | None = None,
    integration_tests: bool = False,
    skip_rulesets: bool = False,
) -> StConfig:
    return StConfig(
        project=_project(language=language, release_model=release_model),
        dependencies={"standard-tooling": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=_ci(versions=versions or ["3.14"], integration_tests=integration_tests),
        github=GithubOverrides(skip_rulesets=skip_rulesets),
    )


def test_compute_desired_state_has_three_rulesets() -> None:
    state = compute_desired_state(_st_config())
    assert len(state.rulesets) == 3
    names = [r.name for r in state.rulesets]
    assert "Branch protection" in names
    assert "Tag protection" in names
    assert "CI gates" in names


def test_compute_desired_state_skip_rulesets() -> None:
    state = compute_desired_state(_st_config(skip_rulesets=True))
    assert state.rulesets == []


def test_compute_desired_state_no_ci_section() -> None:
    cfg = _st_config()
    cfg.ci = None
    state = compute_desired_state(cfg)
    assert len(state.rulesets) == 2
    names = [r.name for r in state.rulesets]
    assert "CI gates" not in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v -k "compute" --no-header`

Expected: ImportError for `compute_desired_state`.

- [ ] **Step 3: Implement `compute_desired_state()`**

Add to `src/standard_tooling/lib/github_config.py`:

```python
if TYPE_CHECKING:
    from standard_tooling.lib.config import CiConfig, ProjectConfig, StConfig


def compute_desired_state(config: StConfig) -> DesiredState:
    rulesets: list[DesiredRuleset] = []

    if not config.github.skip_rulesets:
        rulesets.append(desired_branch_protection_ruleset())
        rulesets.append(desired_tag_protection_ruleset())

        if config.ci is not None:
            rulesets.append(
                desired_ci_gates_ruleset(config.project, config.ci)
            )

    return DesiredState(
        repo_settings=desired_repo_settings(),
        security=desired_security_settings(),
        actions_permissions=desired_actions_permissions(),
        rulesets=rulesets,
    )
```

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(github-config): add compute_desired_state() top-level function

Assembles full desired state from StConfig. Handles skip-rulesets
override and gracefully omits CI gates when [ci] is absent.

Ref #173
```

---

## Phase 4: Actual State Fetching and Comparison

### Task 8: Fetch actual GitHub state

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing tests**

```python
from unittest.mock import patch, call
from standard_tooling.lib.github_config import fetch_actual_state


def test_fetch_actual_state_repo_settings() -> None:
    repo_json = {
        "default_branch": "develop",
        "allow_auto_merge": False,
        "delete_branch_on_merge": True,
        "allow_merge_commit": True,
        "allow_squash_merge": True,
        "allow_rebase_merge": True,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": True,
        "security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
            "dependabot_security_updates": {"status": "disabled"},
        },
    }

    def mock_read_json(*args: str) -> dict | list:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"enabled": True, "allowed_actions": "selected"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        if endpoint == "repos/o/r/actions/permissions/selected-actions":
            return {"patterns_allowed": ["actions/*", "wphillipmoore/*"]}
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
        actual = fetch_actual_state("o/r")

    assert actual.repo_settings.default_branch == "develop"
    assert actual.security.secret_scanning == "enabled"
    assert actual.actions_permissions.default_workflow_permissions == "read"
    assert actual.rulesets == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v -k "fetch_actual" --no-header`

Expected: ImportError for `fetch_actual_state`.

- [ ] **Step 3: Implement `fetch_actual_state()`**

Add to `src/standard_tooling/lib/github_config.py`:

```python
import subprocess

from standard_tooling.lib import github


def _fetch_vulnerability_alerts(repo: str) -> bool:
    try:
        result = subprocess.run(
            ("gh", "api", f"repos/{repo}/vulnerability-alerts", "-i"),
            capture_output=True,
            text=True,
            check=False,
        )
        return "204" in result.stdout.split("\n")[0]
    except Exception:
        return False


def fetch_actual_state(repo: str) -> DesiredState:
    repo_data = github.read_json("api", f"repos/{repo}")

    sa = repo_data.get("security_and_analysis", {})

    repo_settings = DesiredRepoSettings(
        default_branch=repo_data.get("default_branch", ""),
        allow_auto_merge=repo_data.get("allow_auto_merge", False),
        delete_branch_on_merge=repo_data.get("delete_branch_on_merge", False),
        allow_merge_commit=repo_data.get("allow_merge_commit", False),
        allow_squash_merge=repo_data.get("allow_squash_merge", False),
        allow_rebase_merge=repo_data.get("allow_rebase_merge", False),
        has_issues=repo_data.get("has_issues", False),
        has_projects=repo_data.get("has_projects", False),
        has_wiki=repo_data.get("has_wiki", False),
    )

    security = DesiredSecuritySettings(
        secret_scanning=sa.get("secret_scanning", {}).get("status", "disabled"),
        secret_scanning_push_protection=sa.get(
            "secret_scanning_push_protection", {}
        ).get("status", "disabled"),
        vulnerability_alerts=_fetch_vulnerability_alerts(repo),
        dependabot_security_updates=sa.get(
            "dependabot_security_updates", {}
        ).get("status", "disabled"),
    )

    actions_perm = github.read_json("api", f"repos/{repo}/actions/permissions")
    actions_workflow = github.read_json(
        "api", f"repos/{repo}/actions/permissions/workflow"
    )

    patterns: list[str] = []
    if actions_perm.get("allowed_actions") == "selected":
        selected = github.read_json(
            "api", f"repos/{repo}/actions/permissions/selected-actions"
        )
        patterns = selected.get("patterns_allowed", [])

    actions_permissions = DesiredActionsPermissions(
        default_workflow_permissions=actions_workflow.get(
            "default_workflow_permissions", ""
        ),
        can_approve_pull_request_reviews=actions_workflow.get(
            "can_approve_pull_request_reviews", False
        ),
        allowed_actions=actions_perm.get("allowed_actions", ""),
        patterns_allowed=sorted(patterns),
    )

    raw_rulesets = github.read_json("api", f"repos/{repo}/rulesets")
    rulesets: list[DesiredRuleset] = []
    for rs_summary in raw_rulesets:
        rs_detail = github.read_json(
            "api", f"repos/{repo}/rulesets/{rs_summary['id']}"
        )
        conditions = rs_detail.get("conditions", {})
        ref_name = conditions.get("ref_name", {})
        rulesets.append(
            DesiredRuleset(
                name=rs_detail.get("name", ""),
                target=rs_detail.get("target", ""),
                enforcement=rs_detail.get("enforcement", ""),
                ref_include=ref_name.get("include", []),
                bypass_actors=rs_detail.get("bypass_actors", []),
                rules=rs_detail.get("rules", []),
            )
        )

    return DesiredState(
        repo_settings=repo_settings,
        security=security,
        actions_permissions=actions_permissions,
        rulesets=rulesets,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(github-config): add fetch_actual_state() for GitHub API reads

Fetches repo settings, security settings, Actions permissions, and
rulesets via gh api. Returns a DesiredState for comparison against
the computed desired state.

Ref #173
```

### Task 9: Diff computation

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing tests**

```python
from standard_tooling.lib.github_config import ConfigDiff, compute_diff


def test_diff_identical_states_is_empty() -> None:
    state = compute_desired_state(_st_config())
    diff = compute_diff(desired=state, actual=state)
    assert diff.is_compliant()
    assert diff.items == []


def test_diff_detects_repo_setting_mismatch() -> None:
    desired = compute_desired_state(_st_config())
    actual = compute_desired_state(_st_config())
    actual.repo_settings.allow_auto_merge = True
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(
        d.field == "repo_settings.allow_auto_merge" for d in diff.items
    )


def test_diff_detects_missing_ruleset() -> None:
    desired = compute_desired_state(_st_config())
    actual = compute_desired_state(_st_config())
    actual.rulesets = []
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field.startswith("rulesets.") for d in diff.items)


def test_diff_detects_actions_permission_mismatch() -> None:
    desired = compute_desired_state(_st_config())
    actual = compute_desired_state(_st_config())
    actual.actions_permissions.default_workflow_permissions = "write"
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v -k "diff" --no-header`

Expected: ImportError.

- [ ] **Step 3: Implement diff computation**

Add to `src/standard_tooling/lib/github_config.py`:

```python
@dataclass
class DiffItem:
    field: str
    expected: object
    actual: object


@dataclass
class ConfigDiff:
    items: list[DiffItem] = field(default_factory=list)

    def is_compliant(self) -> bool:
        return len(self.items) == 0


def _diff_dataclass(
    prefix: str,
    desired: object,
    actual: object,
    items: list[DiffItem],
) -> None:
    if not hasattr(desired, "__dataclass_fields__"):
        if desired != actual:
            items.append(DiffItem(field=prefix, expected=desired, actual=actual))
        return
    for field_name in desired.__dataclass_fields__:
        d_val = getattr(desired, field_name)
        a_val = getattr(actual, field_name)
        _diff_dataclass(f"{prefix}.{field_name}", d_val, a_val, items)


def _diff_rulesets(
    desired: list[DesiredRuleset],
    actual: list[DesiredRuleset],
    items: list[DiffItem],
) -> None:
    desired_by_name = {r.name: r for r in desired}
    actual_by_name = {r.name: r for r in actual}

    for name in desired_by_name:
        if name not in actual_by_name:
            items.append(DiffItem(
                field=f"rulesets.{name}",
                expected="present",
                actual="missing",
            ))
        else:
            _diff_dataclass(
                f"rulesets.{name}",
                desired_by_name[name],
                actual_by_name[name],
                items,
            )

    for name in actual_by_name:
        if name not in desired_by_name:
            items.append(DiffItem(
                field=f"rulesets.{name}",
                expected="absent",
                actual="present",
            ))


def compute_diff(*, desired: DesiredState, actual: DesiredState) -> ConfigDiff:
    items: list[DiffItem] = []
    _diff_dataclass("repo_settings", desired.repo_settings, actual.repo_settings, items)
    _diff_dataclass("security", desired.security, actual.security, items)
    _diff_dataclass(
        "actions_permissions",
        desired.actions_permissions,
        actual.actions_permissions,
        items,
    )
    _diff_rulesets(desired.rulesets, actual.rulesets, items)
    return ConfigDiff(items=items)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(github-config): add diff computation engine

Compares desired vs actual DesiredState objects field-by-field,
including ruleset-level comparison by name. Returns a ConfigDiff
with is_compliant() for audit exit codes.

Ref #173
```

---

## Phase 5: CLI Tool

### Task 10: CLI argument parsing

**Files:**
- Create: `src/standard_tooling/bin/github_config.py`
- Create: `tests/standard_tooling/test_github_config_cli.py`

- [ ] **Step 1: Write failing tests**

Create `tests/standard_tooling/test_github_config_cli.py`:

```python
"""Tests for standard_tooling.bin.github_config."""

from __future__ import annotations

import pytest

from standard_tooling.bin.github_config import parse_args


def test_parse_audit_single_repo() -> None:
    args = parse_args(["audit", "--repo", "o/r"])
    assert args.command == "audit"
    assert args.repo == "o/r"


def test_parse_diff_single_repo() -> None:
    args = parse_args(["diff", "--repo", "o/r"])
    assert args.command == "diff"


def test_parse_apply_single_repo() -> None:
    args = parse_args(["apply", "--repo", "o/r"])
    assert args.command == "apply"
    assert args.yes is False


def test_parse_apply_with_yes() -> None:
    args = parse_args(["apply", "--repo", "o/r", "--yes"])
    assert args.yes is True


def test_parse_project_mode() -> None:
    args = parse_args(["audit", "--owner", "acme", "--project", "3"])
    assert args.owner == "acme"
    assert args.project == "3"


def test_parse_no_target_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["audit"])


def test_parse_no_command_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--repo", "o/r"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_cli.py -v --no-header`

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement CLI argument parsing**

Create `src/standard_tooling/bin/github_config.py`:

```python
"""Audit, diff, and apply GitHub configuration for managed repos."""

from __future__ import annotations

import argparse
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enforce canonical GitHub configuration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("audit", "diff", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", help="Single repo (OWNER/REPO)")
        sp.add_argument("--owner", help="GitHub owner (project mode)")
        sp.add_argument("--project", help="GitHub Project number")
        if name == "apply":
            sp.add_argument(
                "--yes",
                action="store_true",
                help="Skip confirmation prompt",
            )

    args = parser.parse_args(argv)

    if not args.repo and not (args.owner and args.project):
        parser.error("--repo or --owner/--project required")

    return args


def main(argv: list[str] | None = None) -> int:
    _args = parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_cli.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Add entry point to pyproject.toml**

Add to `[project.scripts]`:

```
st-github-config = "standard_tooling.bin.github_config:main"
```

- [ ] **Step 6: Commit**

```
feat(github-config): add st-github-config CLI skeleton

Argument parsing for audit/diff/apply subcommands with --repo and
--owner/--project targeting. Entry point registered in pyproject.toml.

Ref #173
```

### Task 11: Audit mode implementation

**Files:**
- Modify: `src/standard_tooling/bin/github_config.py`
- Test: `tests/standard_tooling/test_github_config_cli.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/standard_tooling/test_github_config_cli.py`:

```python
from unittest.mock import patch, MagicMock

from standard_tooling.bin.github_config import main
from standard_tooling.lib.github_config import (
    ConfigDiff,
    DesiredState,
    DiffItem,
)


def _mock_compliant() -> ConfigDiff:
    return ConfigDiff(items=[])


def _mock_noncompliant() -> ConfigDiff:
    return ConfigDiff(items=[
        DiffItem(
            field="repo_settings.allow_auto_merge",
            expected=False,
            actual=True,
        ),
    ])


def test_audit_compliant_returns_zero() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch(
            "standard_tooling.bin.github_config._fetch_remote_config",
        ),
    ):
        assert main(["audit", "--repo", "o/r"]) == 0


def test_audit_noncompliant_returns_one() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch(
            "standard_tooling.bin.github_config._fetch_remote_config",
        ),
    ):
        assert main(["audit", "--repo", "o/r"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_cli.py -v -k "audit" --no-header`

Expected: AttributeError or similar — the functions don't exist.

- [ ] **Step 3: Implement audit mode**

Update `src/standard_tooling/bin/github_config.py`:

```python
"""Audit, diff, and apply GitHub configuration for managed repos."""

from __future__ import annotations

import argparse
import base64
import sys

import tomllib

from standard_tooling.lib import github
from standard_tooling.lib.config import StConfig, read_config
from standard_tooling.lib.github_config import (
    ConfigDiff,
    compute_desired_state,
    compute_diff,
    fetch_actual_state,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enforce canonical GitHub configuration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("audit", "diff", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", help="Single repo (OWNER/REPO)")
        sp.add_argument("--owner", help="GitHub owner (project mode)")
        sp.add_argument("--project", help="GitHub Project number")
        if name == "apply":
            sp.add_argument(
                "--yes",
                action="store_true",
                help="Skip confirmation prompt",
            )

    args = parser.parse_args(argv)

    if not args.repo and not (args.owner and args.project):
        parser.error("--repo or --owner/--project required")

    return args


def _resolve_repos(args: argparse.Namespace) -> list[str]:
    if args.repo:
        return [args.repo]
    return github.list_project_repos(args.owner, args.project)


def _fetch_remote_config(repo: str) -> StConfig:
    content_data = github.read_json(
        "api",
        f"repos/{repo}/contents/standard-tooling.toml",
    )
    raw_bytes = base64.b64decode(content_data["content"])
    raw = tomllib.loads(raw_bytes.decode())
    from standard_tooling.lib.config import _parse_raw_config
    return _parse_raw_config(raw)


def _audit_repo(repo: str, config: StConfig) -> ConfigDiff:
    desired = compute_desired_state(config)
    actual = fetch_actual_state(repo)
    return compute_diff(desired=desired, actual=actual)


def _print_diff(repo: str, diff: ConfigDiff) -> None:
    if diff.is_compliant():
        print(f"  {repo}: compliant")
        return
    print(f"  {repo}: NON-COMPLIANT ({len(diff.items)} issues)")
    for item in diff.items:
        print(f"    {item.field}: expected={item.expected!r}, actual={item.actual!r}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repos = _resolve_repos(args)
    all_compliant = True

    for repo in repos:
        config = _fetch_remote_config(repo)
        diff = _audit_repo(repo, config)
        _print_diff(repo, diff)
        if not diff.is_compliant():
            all_compliant = False

    if args.command == "audit":
        return 0 if all_compliant else 1
    if args.command == "diff":
        return 0

    # apply mode — implemented in Task 12
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: This references `_parse_raw_config` which needs to be
extracted from `read_config()` in `config.py`. The extraction is
straightforward: move the parsing logic (everything after file
reading) into `_parse_raw_config(raw: dict) -> StConfig`, and have
`read_config()` call it. This keeps the file I/O separate from
parsing, which the remote config fetch needs.

- [ ] **Step 4: Extract `_parse_raw_config()` from `config.py`**

In `src/standard_tooling/lib/config.py`, refactor `read_config()`
to split file reading from parsing:

```python
def _parse_raw_config(raw: dict) -> StConfig:
    """Parse and validate a raw TOML dict into StConfig."""
    # (move all validation and construction logic here)
    ...


def read_config(repo_root: Path) -> StConfig:
    config_path = repo_root / CONFIG_FILE
    if not config_path.is_file():
        msg = f"{CONFIG_FILE} not found at {repo_root}"
        raise FileNotFoundError(msg)

    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{CONFIG_FILE} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc

    return _parse_raw_config(raw)
```

- [ ] **Step 5: Run full test suite**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/ -v --no-header`

Expected: All pass — both new CLI tests and all existing config
tests (the refactor must not change behavior).

- [ ] **Step 6: Commit**

```
feat(github-config): implement audit and diff modes

st-github-config audit fetches each repo's standard-tooling.toml
via gh api, computes desired state, fetches actual state, and
reports diffs. Exit 1 if any repo is non-compliant.

Extracts _parse_raw_config() from read_config() to support remote
config parsing.

Ref #173
```

### Task 12: Apply mode implementation

**Files:**
- Modify: `src/standard_tooling/bin/github_config.py`
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_cli.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/standard_tooling/test_github_config_cli.py`:

```python
def test_apply_calls_apply_functions() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._apply_repo",
        ) as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r", "--yes"])
    assert result == 0
    mock_apply.assert_called_once()


def test_apply_without_yes_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "y")
    with (
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch("standard_tooling.bin.github_config._apply_repo") as mock_apply,
    ):
        main(["apply", "--repo", "o/r"])
    mock_apply.assert_called_once()


def test_apply_aborted_on_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with (
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch("standard_tooling.bin.github_config._apply_repo") as mock_apply,
    ):
        main(["apply", "--repo", "o/r"])
    mock_apply.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_cli.py -v -k "apply" --no-header`

Expected: AttributeError — `_apply_repo` does not exist.

- [ ] **Step 3: Implement apply functions**

Add `apply_desired_state()` to
`src/standard_tooling/lib/github_config.py`:

```python
def apply_desired_state(repo: str, desired: DesiredState) -> list[str]:
    """Apply the desired state to a repo. Returns list of changes made."""
    changes: list[str] = []

    # Repo settings
    github.run(
        "api", f"repos/{repo}",
        "-X", "PATCH",
        "-f", f"default_branch={desired.repo_settings.default_branch}",
        "-F", f"allow_auto_merge={str(desired.repo_settings.allow_auto_merge).lower()}",
        "-F", f"delete_branch_on_merge={str(desired.repo_settings.delete_branch_on_merge).lower()}",
        "-F", f"allow_merge_commit={str(desired.repo_settings.allow_merge_commit).lower()}",
        "-F", f"allow_squash_merge={str(desired.repo_settings.allow_squash_merge).lower()}",
        "-F", f"allow_rebase_merge={str(desired.repo_settings.allow_rebase_merge).lower()}",
        "-F", f"has_issues={str(desired.repo_settings.has_issues).lower()}",
        "-F", f"has_projects={str(desired.repo_settings.has_projects).lower()}",
        "-F", f"has_wiki={str(desired.repo_settings.has_wiki).lower()}",
    )
    changes.append("repo_settings")

    # Actions permissions
    github.run(
        "api", f"repos/{repo}/actions/permissions/workflow",
        "-X", "PUT",
        "-f", f"default_workflow_permissions={desired.actions_permissions.default_workflow_permissions}",
        "-F", f"can_approve_pull_request_reviews={str(desired.actions_permissions.can_approve_pull_request_reviews).lower()}",
    )

    github.run(
        "api", f"repos/{repo}/actions/permissions",
        "-X", "PUT",
        "-f", f"allowed_actions={desired.actions_permissions.allowed_actions}",
    )

    if desired.actions_permissions.allowed_actions == "selected":
        import json as _json
        import tempfile

        body = {"patterns_allowed": desired.actions_permissions.patterns_allowed}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            _json.dump(body, f)
            f.flush()
            github.run(
                "api", f"repos/{repo}/actions/permissions/selected-actions",
                "-X", "PUT",
                "--input", f.name,
            )
    changes.append("actions_permissions")

    return changes
```

The full apply implementation for rulesets (create/update/delete)
is complex — it needs to match existing rulesets by name, update
those that differ, create missing ones, and optionally remove
unexpected ones. The exact `gh api` calls for ruleset CRUD are:

- Create: `POST repos/{repo}/rulesets` with JSON body
- Update: `PUT repos/{repo}/rulesets/{id}` with JSON body
- Delete: `DELETE repos/{repo}/rulesets/{id}`

Add `_apply_repo()` to the CLI module and wire it into the apply
command branch:

```python
def _apply_repo(repo: str, config: StConfig) -> None:
    desired = compute_desired_state(config)
    changes = apply_desired_state(repo, desired)
    for change in changes:
        print(f"  applied: {change}")
```

Wire into `main()`:

```python
    if args.command == "apply":
        for repo in repos:
            config = _fetch_remote_config(repo)
            diff = _audit_repo(repo, config)
            if diff.is_compliant():
                print(f"  {repo}: already compliant")
                continue

            # Safety gate: refuse to apply CI gates ruleset if the
            # required check names are not yet produced by CI workflows.
            # This prevents writing rulesets that would block all merges.
            ci_gate_diffs = [
                d for d in diff.items
                if d.field.startswith("rulesets.CI gates")
            ]
            if ci_gate_diffs:
                print(f"  {repo}: CI gates ruleset has diffs — "
                      "verify CI workflows produce expected check names "
                      "before applying (run audit to confirm)")

            _print_diff(repo, diff)
            if not args.yes:
                response = input(f"  Apply changes to {repo}? [y/N] ")
                if response.lower() != "y":
                    print(f"  {repo}: skipped")
                    continue
            _apply_repo(repo, config)
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_cli.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(github-config): implement apply mode with confirmation prompt

Apply mode diffs each repo, shows what would change, prompts for
confirmation (or skips with --yes), then writes corrections via
gh api. Repo settings and actions permissions are applied; ruleset
CRUD is stubbed for the next task.

Ref #173
```

### Task 13: Ruleset apply (create/update/delete)

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing tests**

```python
def test_apply_rulesets_creates_missing() -> None:
    desired = [desired_branch_protection_ruleset()]
    with (
        patch("standard_tooling.lib.github_config.github.read_json", return_value=[]),
        patch("standard_tooling.lib.github_config.github.run") as mock_run,
    ):
        apply_rulesets("o/r", desired)
    assert any("rulesets" in str(c) for c in mock_run.call_args_list)


def test_apply_rulesets_skips_matching() -> None:
    ruleset = desired_branch_protection_ruleset()
    actual_summary = [{"id": 1, "name": "Branch protection"}]
    actual_detail = {
        "id": 1,
        "name": "Branch protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main", "refs/heads/develop"], "exclude": []}},
        "bypass_actors": [],
        "rules": ruleset.rules,
    }

    def mock_read_json(*args: str) -> dict | list:
        if "rulesets" in args[1] and "/" not in args[1].split("rulesets/")[-1]:
            return actual_summary
        return actual_detail

    with (
        patch("standard_tooling.lib.github_config.github.read_json", side_effect=mock_read_json),
        patch("standard_tooling.lib.github_config.github.run") as mock_run,
    ):
        apply_rulesets("o/r", [ruleset])
    mock_run.assert_not_called()
```

Update imports to include `apply_rulesets`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v -k "apply_rulesets" --no-header`

Expected: ImportError.

- [ ] **Step 3: Implement `apply_rulesets()`**

Add to `src/standard_tooling/lib/github_config.py`:

```python
import json as _json
import tempfile


def apply_rulesets(repo: str, desired: list[DesiredRuleset]) -> list[str]:
    changes: list[str] = []
    actual_summaries = github.read_json("api", f"repos/{repo}/rulesets")
    actual_by_name: dict[str, dict] = {}

    for summary in actual_summaries:
        detail = github.read_json(
            "api", f"repos/{repo}/rulesets/{summary['id']}"
        )
        actual_by_name[detail["name"]] = detail

    for d_ruleset in desired:
        body = _ruleset_to_api_body(d_ruleset)

        if d_ruleset.name not in actual_by_name:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as f:
                _json.dump(body, f)
                f.flush()
                github.run(
                    "api", f"repos/{repo}/rulesets",
                    "-X", "POST",
                    "--input", f.name,
                )
            changes.append(f"created ruleset: {d_ruleset.name}")
        else:
            existing = actual_by_name[d_ruleset.name]
            if not _ruleset_matches(d_ruleset, existing):
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False
                ) as f:
                    _json.dump(body, f)
                    f.flush()
                    github.run(
                        "api",
                        f"repos/{repo}/rulesets/{existing['id']}",
                        "-X", "PUT",
                        "--input", f.name,
                    )
                changes.append(f"updated ruleset: {d_ruleset.name}")

    return changes


def _ruleset_to_api_body(ruleset: DesiredRuleset) -> dict:
    return {
        "name": ruleset.name,
        "target": ruleset.target,
        "enforcement": ruleset.enforcement,
        "conditions": {
            "ref_name": {
                "include": ruleset.ref_include,
                "exclude": [],
            },
        },
        "bypass_actors": ruleset.bypass_actors,
        "rules": ruleset.rules,
    }


def _ruleset_matches(desired: DesiredRuleset, actual: dict) -> bool:
    conditions = actual.get("conditions", {})
    ref_name = conditions.get("ref_name", {})
    return (
        actual.get("name") == desired.name
        and actual.get("target") == desired.target
        and actual.get("enforcement") == desired.enforcement
        and ref_name.get("include", []) == desired.ref_include
        and actual.get("bypass_actors", []) == desired.bypass_actors
        and actual.get("rules", []) == desired.rules
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Wire `apply_rulesets` into `apply_desired_state`**

Update `apply_desired_state()` in `github_config.py` to call
`apply_rulesets(repo, desired.rulesets)` and extend `changes`.

- [ ] **Step 6: Run full test suite**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/ -v --no-header`

Expected: All pass.

- [ ] **Step 7: Commit**

```
feat(github-config): implement ruleset create/update via gh api

apply_rulesets() matches existing rulesets by name, creates missing
ones, and updates those that don't match the desired state. Uses
JSON input files for gh api calls.

Ref #173
```

---

## Phase 6: Classic Branch Protection Cleanup

### Task 14: Detect and remove classic branch protection

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Test: `tests/standard_tooling/test_github_config_lib.py`

- [ ] **Step 1: Write failing tests**

```python
def test_detect_classic_branch_protection() -> None:
    def mock_read_json(*args: str) -> dict | list:
        if "protection" in args[1]:
            return {
                "required_pull_request_reviews": {
                    "required_approving_review_count": 0,
                },
            }
        return {}

    with patch(
        "standard_tooling.lib.github_config.github.read_json",
        side_effect=mock_read_json,
    ):
        result = detect_classic_branch_protection("o/r")
    assert "develop" in result or "main" in result


def test_detect_classic_none_present() -> None:
    def mock_read_json(*args: str) -> dict | list:
        raise subprocess.CalledProcessError(1, "gh")

    with patch(
        "standard_tooling.lib.github_config.github.read_json",
        side_effect=mock_read_json,
    ):
        result = detect_classic_branch_protection("o/r")
    assert result == []
```

Update imports to include `detect_classic_branch_protection`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v -k "classic" --no-header`

Expected: ImportError.

- [ ] **Step 3: Implement detection**

```python
def detect_classic_branch_protection(repo: str) -> list[str]:
    branches_with_protection: list[str] = []
    for branch in ("develop", "main"):
        try:
            github.read_json(
                "api", f"repos/{repo}/branches/{branch}/protection"
            )
            branches_with_protection.append(branch)
        except subprocess.CalledProcessError:
            pass
    return branches_with_protection
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_github_config_lib.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Wire into audit output**

In the CLI `_audit_repo`, after computing the diff, also call
`detect_classic_branch_protection()` and report any findings as
warnings.

- [ ] **Step 6: Commit**

```
feat(github-config): detect stale classic branch protection

Checks develop and main for classic branch protection rules. Audit
mode reports these as warnings — all protection should be via
rulesets.

Ref #173
```

---

## Phase 7: Validation and Mypy

### Task 15: Type checking and lint pass

**Files:**
- All new and modified files

- [ ] **Step 1: Run mypy**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run mypy src/`

Fix any type errors.

- [ ] **Step 2: Run ruff**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run ruff check src/ tests/`

Fix any lint errors.

- [ ] **Step 3: Run full test suite with coverage**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest --cov=standard_tooling --cov-branch --cov-fail-under=100`

Fix any coverage gaps.

- [ ] **Step 4: Commit fixes**

```
chore: fix type errors and lint warnings in github-config

Ref #173
```

### Task 16: Container validation

- [ ] **Step 1: Run st-validate-local in the dev container**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && st-docker-run -- uv run st-validate-local`

This is the canonical validation command. Fix any issues.

- [ ] **Step 2: Commit if needed**

```
fix: address validation findings

Ref #173
```

---

## Dependency Gate: Canonical Check Name Registry

**Phases 8–13 below depend on the canonical check name registry.**

The check name registry defines every status check name, the CI job
structure that produces each one, and the naming convention for
version-matrix expansions. It is the linchpin connecting rulesets,
CI workflows, and `st-validate --only/--skip`.

The registry will be designed in a separate
brainstorm→spec→pushback→plan cycle (per user direction). Once
defined, the interim check names used in Task 6
(`desired_ci_gates_ruleset`) are updated to match the canonical
registry, and work on Phase 8 begins.

**Until the registry is defined:**
- Phases 1–7 (st-github-config) can be implemented and merged
- The derivation engine uses current fleet conventions as interim
  check names
- Phases 8–13 are blocked

---

## Phase 8: `st-validate` — Rename and Host-Orchestrated Execution

### Task 17: Create `st-validate` host-side entry point

**Files:**
- Create: `src/standard_tooling/bin/validate.py`
- Create: `tests/standard_tooling/test_validate.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing tests for arg parsing and orchestration skeleton**

Create `tests/standard_tooling/test_validate.py`:

```python
"""Tests for standard_tooling.bin.validate."""

from __future__ import annotations

import pytest

from standard_tooling.bin.validate import parse_args


def test_parse_args_no_flags() -> None:
    args = parse_args([])
    assert args.only is None
    assert args.skip is None


def test_parse_args_only() -> None:
    args = parse_args(["--only", "lint,typecheck"])
    assert args.only == "lint,typecheck"


def test_parse_args_skip() -> None:
    args = parse_args(["--skip", "integration"])
    assert args.skip == "integration"


def test_parse_args_only_and_skip_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--only", "lint", "--skip", "test"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate.py -v --no-header`

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement st-validate entry point skeleton**

Create `src/standard_tooling/bin/validate.py`:

```python
"""Host-side validation orchestrator.

Reads standard-tooling.toml, then runs validation checks via
st-docker-run. Replaces st-validate-local with host-orchestrated
execution and version-matrix awareness.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from standard_tooling.lib import config, git


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="st-validate",
        description="Run validation checks via dev containers.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--only",
        help="Run only these checks (comma-separated canonical names)",
    )
    group.add_argument(
        "--skip",
        help="Skip these checks (comma-separated canonical names)",
    )
    return parser.parse_args(argv)


def _run_in_container(
    image: str, command: list[str], repo_root: Path
) -> int:
    cmd = [
        "st-docker-run",
        "--",
        *command,
    ]
    result = subprocess.run(cmd, check=False, cwd=repo_root)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = git.repo_root()

    try:
        st_config = config.read_config(repo_root)
    except (FileNotFoundError, config.ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("=" * 40)
    print("st-validate")
    print(f"language: {st_config.project.primary_language}")
    if st_config.ci:
        print(f"versions: {', '.join(st_config.ci.versions)}")
    print("=" * 40)
    print()

    # Phase 1: Common checks (once, base image)
    # Phase 2: Language checks (per-version)
    # Implemented in subsequent tasks

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add entry point to pyproject.toml**

Add to `[project.scripts]`:

```
st-validate = "standard_tooling.bin.validate:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate.py -v --no-header`

Expected: All pass.

- [ ] **Step 6: Commit**

```
feat(validate): add st-validate host-side entry point skeleton

Replaces st-validate-local with a host-orchestrated model. Reads
config and orchestrates checks via st-docker-run rather than
running inside a container. --only/--skip arg parsing for check
filtering (canonical name based).

Ref #173
```

### Task 18: Deprecation wrappers for old entry points

**Files:**
- Modify: `src/standard_tooling/bin/validate_local.py`
- Modify: `tests/standard_tooling/test_validate_local.py`

- [ ] **Step 1: Write failing test for deprecation warning**

Add to `tests/standard_tooling/test_validate_local.py` (create if
needed):

```python
"""Tests for st-validate-local deprecation wrapper."""

from __future__ import annotations

from unittest.mock import patch

from standard_tooling.bin.validate_local import main


def test_validate_local_emits_deprecation(capsys) -> None:
    with patch("standard_tooling.bin.validate_local._delegate") as mock:
        mock.return_value = 0
        main([])
    captured = capsys.readouterr()
    assert "deprecated" in captured.err.lower()
    assert "st-validate" in captured.err


def test_validate_local_delegates_to_validate() -> None:
    with patch("standard_tooling.bin.validate_local._delegate") as mock:
        mock.return_value = 0
        result = main([])
    assert result == 0
    mock.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate_local.py -v --no-header`

Expected: Tests fail (current `main()` has different behavior).

- [ ] **Step 3: Rewrite validate_local.py as deprecation wrapper**

Replace `src/standard_tooling/bin/validate_local.py`:

```python
"""Deprecated — use st-validate instead.

This wrapper exists for one minor version cycle. It prints a
deprecation warning, then delegates to st-validate.
"""

from __future__ import annotations

import sys
import warnings


def _delegate(argv: list[str] | None = None) -> int:
    from standard_tooling.bin.validate import main as validate_main
    return validate_main(argv)


def main(argv: list[str] | None = None) -> int:
    print(
        "WARNING: st-validate-local is deprecated. Use st-validate instead.",
        file=sys.stderr,
    )
    return _delegate(argv)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate_local.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Similarly update validate_local_lang.py and validate_local_common_container.py**

Both become thin wrappers that emit deprecation warnings. The
`st-validate-local-python`, `st-validate-local-common`, etc. entry
points all warn and delegate to `st-validate`.

- [ ] **Step 6: Run full test suite**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/ -v --no-header`

Expected: All pass.

- [ ] **Step 7: Commit**

```
feat(validate): deprecate st-validate-local entry points

st-validate-local, st-validate-local-<lang>, and
st-validate-local-common now emit deprecation warnings and delegate
to st-validate. Entry points remain for one minor version cycle.

Ref #173
```

---

## Phase 9: Version Matrix and Per-Language Command Registry

### Task 19: Per-language command registry module

**Files:**
- Create: `src/standard_tooling/lib/validate_commands.py`
- Create: `tests/standard_tooling/test_validate_commands.py`

- [ ] **Step 1: Write failing tests**

Create `tests/standard_tooling/test_validate_commands.py`:

```python
"""Tests for standard_tooling.lib.validate_commands."""

from __future__ import annotations

from standard_tooling.lib.validate_commands import (
    CheckKind,
    language_commands,
)


def test_python_lint_commands() -> None:
    cmds = language_commands("python", CheckKind.LINT)
    assert cmds == ["ruff check", "ruff format --check ."]


def test_python_typecheck_commands() -> None:
    cmds = language_commands("python", CheckKind.TYPECHECK)
    assert cmds == ["mypy src/"]


def test_python_test_commands() -> None:
    cmds = language_commands("python", CheckKind.TEST)
    assert cmds == ["pytest --cov --cov-branch --cov-fail-under=100"]


def test_python_audit_commands() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    assert cmds == ["uv sync --check --frozen --group dev", "uv lock --check"]


def test_go_lint_commands() -> None:
    cmds = language_commands("go", CheckKind.LINT)
    assert "golangci-lint run" in cmds


def test_go_test_commands() -> None:
    cmds = language_commands("go", CheckKind.TEST)
    assert any("go test" in c for c in cmds)


def test_rust_lint_commands() -> None:
    cmds = language_commands("rust", CheckKind.LINT)
    assert "cargo fmt --check" in cmds
    assert "cargo clippy" in cmds


def test_unknown_language_returns_empty() -> None:
    cmds = language_commands("unknown", CheckKind.LINT)
    assert cmds == []


def test_language_with_no_typecheck() -> None:
    cmds = language_commands("go", CheckKind.TYPECHECK)
    assert cmds == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate_commands.py -v --no-header`

Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement per-language command registry**

Create `src/standard_tooling/lib/validate_commands.py`:

```python
"""Per-language validation command registry.

Defines the canonical commands for lint, typecheck, test, and audit
per supported language. These are not configurable per-repo — the
standard defines them centrally.
"""

from __future__ import annotations

from enum import Enum


class CheckKind(Enum):
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    AUDIT = "audit"


_REGISTRY: dict[str, dict[CheckKind, list[str]]] = {
    "python": {
        CheckKind.LINT: ["ruff check", "ruff format --check ."],
        CheckKind.TYPECHECK: ["mypy src/"],
        CheckKind.TEST: [
            "pytest --cov --cov-branch --cov-fail-under=100"
        ],
        CheckKind.AUDIT: [
            "uv sync --check --frozen --group dev",
            "uv lock --check",
        ],
    },
    "go": {
        CheckKind.LINT: ["golangci-lint run", "gocyclo -over 15 ."],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["go test -coverprofile=coverage.out ./..."],
        CheckKind.AUDIT: ["govulncheck ./..."],
    },
    "java": {
        CheckKind.LINT: ["./mvnw spotless:check checkstyle:check"],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["./mvnw verify"],
        CheckKind.AUDIT: [],
    },
    "ruby": {
        CheckKind.LINT: ["rubocop"],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["rake"],
        CheckKind.AUDIT: [],
    },
    "rust": {
        CheckKind.LINT: ["cargo fmt --check", "cargo clippy"],
        CheckKind.TYPECHECK: [],
        CheckKind.TEST: ["cargo llvm-cov --fail-under-lines 100"],
        CheckKind.AUDIT: ["cargo audit"],
    },
}


def language_commands(language: str, kind: CheckKind) -> list[str]:
    lang_entry = _REGISTRY.get(language)
    if lang_entry is None:
        return []
    return list(lang_entry.get(kind, []))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate_commands.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(validate): add per-language command registry

Centrally defines lint/typecheck/test/audit commands for each
supported language. Replaces per-repo scripts/dev/*.sh scripts
that were identical boilerplate.

Ref #173
```

### Task 20: Version-matrix execution in st-validate

**Files:**
- Modify: `src/standard_tooling/bin/validate.py`
- Test: `tests/standard_tooling/test_validate.py`

- [ ] **Step 1: Write failing tests for version-matrix execution**

Add to `tests/standard_tooling/test_validate.py`:

```python
from unittest.mock import patch, call
from pathlib import Path

from standard_tooling.bin.validate import main
from standard_tooling.lib.config import (
    CiConfig,
    GithubOverrides,
    MarkdownlintConfig,
    ProjectConfig,
    StConfig,
)


def _config(
    *,
    language: str = "python",
    versions: list[str] | None = None,
) -> StConfig:
    return StConfig(
        project=ProjectConfig(
            repository_type="library",
            versioning_scheme="semver",
            branching_model="library-release",
            release_model="tagged-release",
            primary_language=language,
            co_authors={},
        ),
        dependencies={"standard-tooling": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=CiConfig(
            versions=versions or ["3.14"],
            integration_tests=False,
        ),
        github=GithubOverrides(skip_rulesets=False),
    )


def test_main_runs_common_then_per_version() -> None:
    with (
        patch(
            "standard_tooling.bin.validate.config.read_config",
            return_value=_config(versions=["3.12", "3.13"]),
        ),
        patch(
            "standard_tooling.bin.validate.git.repo_root",
            return_value=Path("/fake"),
        ),
        patch(
            "standard_tooling.bin.validate._run_common_checks",
            return_value=0,
        ) as mock_common,
        patch(
            "standard_tooling.bin.validate._run_language_checks",
            return_value=0,
        ) as mock_lang,
    ):
        result = main([])
    assert result == 0
    mock_common.assert_called_once()
    assert mock_lang.call_count == 2


def test_main_stops_on_common_failure() -> None:
    with (
        patch(
            "standard_tooling.bin.validate.config.read_config",
            return_value=_config(),
        ),
        patch(
            "standard_tooling.bin.validate.git.repo_root",
            return_value=Path("/fake"),
        ),
        patch(
            "standard_tooling.bin.validate._run_common_checks",
            return_value=1,
        ),
        patch(
            "standard_tooling.bin.validate._run_language_checks",
        ) as mock_lang,
    ):
        result = main([])
    assert result == 1
    mock_lang.assert_not_called()


def test_main_stops_on_first_version_failure() -> None:
    with (
        patch(
            "standard_tooling.bin.validate.config.read_config",
            return_value=_config(versions=["3.12", "3.13"]),
        ),
        patch(
            "standard_tooling.bin.validate.git.repo_root",
            return_value=Path("/fake"),
        ),
        patch(
            "standard_tooling.bin.validate._run_common_checks",
            return_value=0,
        ),
        patch(
            "standard_tooling.bin.validate._run_language_checks",
            return_value=1,
        ) as mock_lang,
    ):
        result = main([])
    assert result == 1
    assert mock_lang.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate.py -v -k "version" --no-header`

Expected: AttributeError (functions don't exist yet).

- [ ] **Step 3: Implement version-matrix execution**

Update `src/standard_tooling/bin/validate.py` to add:

```python
from standard_tooling.lib.validate_commands import CheckKind, language_commands

_DEV_IMAGE_TEMPLATE = "ghcr.io/wphillipmoore/dev-{lang}:{version}"


def _run_common_checks(repo_root: Path) -> int:
    cmd = ["st-docker-run", "--", "uv", "run", "st-validate-common"]
    result = subprocess.run(cmd, check=False, cwd=repo_root)
    return result.returncode


def _run_language_checks(
    repo_root: Path,
    language: str,
    version: str,
) -> int:
    image = _DEV_IMAGE_TEMPLATE.format(lang=language, version=version)
    env = {**os.environ, "DOCKER_DEV_IMAGE": image}

    for kind in (CheckKind.LINT, CheckKind.TYPECHECK, CheckKind.TEST, CheckKind.AUDIT):
        commands = language_commands(language, kind)
        for command_str in commands:
            cmd = ["st-docker-run", "--", "bash", "-c", command_str]
            result = subprocess.run(cmd, check=False, cwd=repo_root, env=env)
            if result.returncode != 0:
                return result.returncode
    return 0
```

Update `main()` to orchestrate:

```python
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = git.repo_root()

    try:
        st_config = config.read_config(repo_root)
    except (FileNotFoundError, config.ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    language = st_config.project.primary_language
    versions = st_config.ci.versions if st_config.ci else [language]

    print("=" * 40)
    print("st-validate")
    print(f"language: {language}")
    print(f"versions: {', '.join(versions)}")
    print("=" * 40)
    print()

    # Common checks (once)
    print("[common checks]")
    rc = _run_common_checks(repo_root)
    if rc != 0:
        return rc

    # Language checks (per version)
    for version in versions:
        print(f"\n[{language} {version}]")
        rc = _run_language_checks(repo_root, language, version)
        if rc != 0:
            return rc

    print("\n" + "=" * 40)
    print("st-validate: all checks passed")
    print("=" * 40)
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(validate): implement version-matrix execution

st-validate runs common checks once, then language-specific checks
per version in the matrix. Each version invocation uses the
matching dev container image via st-docker-run.

Ref #173
```

### Task 21: `--only` / `--skip` check filtering

**Files:**
- Modify: `src/standard_tooling/bin/validate.py`
- Test: `tests/standard_tooling/test_validate.py`

- [ ] **Step 1: Write failing tests for filtering**

Add to `tests/standard_tooling/test_validate.py`:

```python
from standard_tooling.bin.validate import _should_run_check


def test_should_run_no_filter() -> None:
    assert _should_run_check("lint", only=None, skip=None) is True


def test_should_run_only_includes() -> None:
    assert _should_run_check("lint", only="lint,typecheck", skip=None) is True


def test_should_run_only_excludes() -> None:
    assert _should_run_check("test", only="lint,typecheck", skip=None) is False


def test_should_run_skip_excludes() -> None:
    assert _should_run_check("integration", only=None, skip="integration") is False


def test_should_run_skip_includes() -> None:
    assert _should_run_check("lint", only=None, skip="integration") is True


def test_should_run_version_qualified() -> None:
    assert _should_run_check("unit(3.14)", only="unit(3.14)", skip=None) is True


def test_should_run_version_base_match() -> None:
    assert _should_run_check("unit(3.14)", only="unit", skip=None) is True


def test_should_run_common_category() -> None:
    assert _should_run_check("shellcheck", only="common", skip=None) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate.py -v -k "should_run" --no-header`

Expected: ImportError for `_should_run_check`.

- [ ] **Step 3: Implement check filtering**

Add to `src/standard_tooling/bin/validate.py`:

```python
_COMMON_CHECKS = frozenset({
    "shellcheck", "hadolint", "actionlint",
    "yamllint", "markdownlint", "repo-profile",
})


def _should_run_check(
    check_name: str,
    *,
    only: str | None,
    skip: str | None,
) -> bool:
    if only is not None:
        targets = {t.strip() for t in only.split(",")}
        if check_name in targets:
            return True
        # Base name match: "unit(3.14)" matches filter "unit"
        base = check_name.split("(")[0]
        if base in targets:
            return True
        # Category match: "shellcheck" matches filter "common"
        if "common" in targets and check_name in _COMMON_CHECKS:
            return True
        return False

    if skip is not None:
        targets = {t.strip() for t in skip.split(",")}
        if check_name in targets:
            return False
        base = check_name.split("(")[0]
        if base in targets:
            return False
        if "common" in targets and check_name in _COMMON_CHECKS:
            return False

    return True
```

Wire `_should_run_check` into the main orchestration loop so each
check category is gated by the filter.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate.py -v --no-header`

Expected: All pass.

- [ ] **Step 5: Commit**

```
feat(validate): add --only/--skip check filtering

Filters use canonical check names — same namespace as CI gates and
rulesets. Supports base-name matching (e.g., "unit" matches
"unit(3.14)") and category matching ("common" matches shellcheck,
hadolint, etc.).

Ref #173
```

---

## Phase 10: Common Checks Consolidation

### Task 22: Add actionlint and hadolint to common validator

**Files:**
- Modify: `src/standard_tooling/bin/validate_local_common_container.py`
  (or its replacement — see note)
- Test: `tests/standard_tooling/test_validate_common.py`

Note: The common validator runs inside the container. Whether it
stays as the existing `validate_local_common_container.py` (renamed
to `validate_common.py`) or is rewritten depends on how much of the
original structure survives the deprecation wrappers from Task 18.
The simplest path is to keep the container-local module, rename it,
and extend it.

- [ ] **Step 1: Write failing tests for new checks**

```python
from unittest.mock import patch
from pathlib import Path


def test_finds_dockerfile_files(tmp_path: Path) -> None:
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "Dockerfile").write_text("FROM alpine\n")
    from standard_tooling.bin.validate_common import _find_dockerfiles
    result = _find_dockerfiles(tmp_path)
    assert len(result) == 1


def test_finds_workflow_files(tmp_path: Path) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yml").write_text("name: CI\n")
    from standard_tooling.bin.validate_common import _find_workflow_files
    result = _find_workflow_files(tmp_path)
    assert len(result) == 1


def test_hadolint_skipped_when_no_dockerfiles(tmp_path: Path) -> None:
    from standard_tooling.bin.validate_common import _find_dockerfiles
    assert _find_dockerfiles(tmp_path) == []


def test_actionlint_skipped_when_no_workflows(tmp_path: Path) -> None:
    from standard_tooling.bin.validate_common import _find_workflow_files
    assert _find_workflow_files(tmp_path) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate_common.py -v --no-header`

Expected: ModuleNotFoundError for `validate_common`.

- [ ] **Step 3: Create or rename the common validator with new checks**

Create `src/standard_tooling/bin/validate_common.py` (new name for
the container-local common checks). Add `_find_dockerfiles()`,
`_find_workflow_files()`, and run hadolint/actionlint when relevant
files exist:

```python
def _find_dockerfiles(repo_root: Path) -> list[str]:
    found: list[str] = []
    for path in repo_root.rglob("Dockerfile*"):
        if path.is_file() and ".worktrees" not in path.parts:
            found.append(str(path))
    return sorted(found)


def _find_workflow_files(repo_root: Path) -> list[str]:
    wf_dir = repo_root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    return sorted(str(p) for p in wf_dir.glob("*.yml"))
```

In `main()`, after yamllint, add:

```python
    dockerfiles = _find_dockerfiles(repo_root)
    if dockerfiles:
        print(f"Running: hadolint ({len(dockerfiles)} files)")
        result = subprocess.run(
            ["hadolint", *dockerfiles], check=False
        )
        if result.returncode != 0:
            return result.returncode

    workflow_files = _find_workflow_files(repo_root)
    if workflow_files:
        print(f"Running: actionlint ({len(workflow_files)} files)")
        result = subprocess.run(
            ["actionlint", *workflow_files], check=False
        )
        if result.returncode != 0:
            return result.returncode
```

- [ ] **Step 4: Add `st-validate-common` entry point to pyproject.toml**

```
st-validate-common = "standard_tooling.bin.validate_common:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/standard_tooling/test_validate_common.py -v --no-header`

Expected: All pass.

- [ ] **Step 6: Commit**

```
feat(validate): consolidate common checks with actionlint and hadolint

Common validator now includes hadolint (Dockerfiles) and actionlint
(workflow files) alongside shellcheck, yamllint, and markdownlint.
All are no-ops when no relevant files exist — safe for every repo.

Ref #173
```

### Task 23: Mypy and full validation pass for Phases 8–10

**Files:**
- All new and modified files from Tasks 17–22

- [ ] **Step 1: Run mypy**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run mypy src/`

Fix any type errors.

- [ ] **Step 2: Run ruff**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run ruff check src/ tests/`

Fix any lint errors.

- [ ] **Step 3: Run full test suite with coverage**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest --cov=standard_tooling --cov-branch --cov-fail-under=100`

Fix any coverage gaps.

- [ ] **Step 4: Run st-docker-run validation**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && st-docker-run -- uv run st-validate-local`

Fix any issues.

- [ ] **Step 5: Commit fixes**

```
chore: fix type errors and lint in validate refactor

Ref #173
```

---

## Phase 11: CI Workflow Refactor

**Dependency:** This phase requires the canonical check name
registry to be defined (separate brainstorm→spec→plan cycle).
Once defined, update the derivation engine (Task 6) check names to
match the registry, then proceed here.

The refactor follows a phased rollout: start with standard-tooling
itself, then standards-project repos, then mq-rest-admin repos.
Each repo is validated before moving to the next.

### Task 24: Update standard-tooling CI workflow

**Files:**
- Modify: `.github/workflows/ci.yml` (in this repo)
- Modify: `standard-tooling.toml` (add `[ci]` section)

- [ ] **Step 0: Update derivation engine to use canonical check names**

Replace the interim check names in
`src/standard_tooling/lib/github_config.py` and
`tests/standard_tooling/test_github_config_lib.py` with the
canonical names from the registry. This is a mechanical
find-and-replace: update the string literals in
`desired_ci_gates_ruleset()` and the corresponding test assertions.
Run the full test suite to verify.

- [ ] **Step 1: Add `[ci]` section to standard-tooling.toml**

```toml
[ci]
versions = ["3.12"]
integration-tests = false
```

- [ ] **Step 2: Refactor ci.yml to use canonical check names**

Update job names and step names to match the canonical check name
registry. The exact changes depend on the registry definition. Key
principles:
- Job names must produce the exact status check names that the
  CI gates ruleset expects
- Version-matrix jobs use the `name` field with version interpolation
- Reusable workflow call results appear as
  `<caller-job> / <callee-job>` in check names

- [ ] **Step 3: Verify CI produces expected check names**

Push to a test branch, open a draft PR, and verify that the status
checks reported by GitHub match the canonical names from the
registry. Use:

```bash
gh pr checks <PR-NUMBER> --repo wphillipmoore/standard-tooling
```

- [ ] **Step 4: Run st-github-config audit to confirm alignment**

```bash
st-github-config audit --repo wphillipmoore/standard-tooling
```

Expected: Compliant (after applying the CI gates ruleset with the
correct check names).

- [ ] **Step 5: Commit**

```
refactor(ci): adopt canonical check names in CI workflow

Job names now match the canonical check name registry. This
enables st-github-config to enforce CI gate rulesets.

Ref #173
```

### Task 25: Update standards-project repos CI workflows

Repos: `standard-tooling-docker`, `standard-tooling-plugin`,
`standard-actions`, `standards-and-conventions`

- [ ] **Step 1: Add `[ci]` sections to each repo's standard-tooling.toml**

Each repo gets a `[ci]` section matching its language and version
requirements. For example, standard-actions (shell/yaml):

```toml
[ci]
versions = ["latest"]
integration-tests = false
```

- [ ] **Step 2: Refactor each repo's ci.yml to canonical names**

Same transformation as Task 24 — rename jobs to produce canonical
check names.

- [ ] **Step 3: Verify per-repo (push test branch, check names)**

For each repo:
```bash
gh pr checks <PR-NUMBER> --repo wphillipmoore/<repo>
```

- [ ] **Step 4: Audit each repo**

```bash
st-github-config audit --repo wphillipmoore/standard-tooling-docker
st-github-config audit --repo wphillipmoore/standard-tooling-plugin
st-github-config audit --repo wphillipmoore/standard-actions
st-github-config audit --repo wphillipmoore/standards-and-conventions
```

- [ ] **Step 5: Merge all PRs**

- [ ] **Step 6: Commit tracking note (in this repo's plan/docs)**

```
docs: mark standards-project CI refactored

Ref #173
```

### Task 26: Update mq-rest-admin repos CI workflows

Repos: `mq-rest-admin-python`, `mq-rest-admin-go`,
`mq-rest-admin-java`, `mq-rest-admin-ruby`, `mq-rest-admin-rust`,
`mq-rest-admin-infra`, `mq-rest-admin-e2e`, `mq-rest-admin-template`

- [ ] **Step 1: Add `[ci]` sections to each repo's standard-tooling.toml**

Example for mq-rest-admin-python:
```toml
[ci]
versions = ["3.12", "3.13", "3.14"]
integration-tests = true
```

- [ ] **Step 2: Refactor each repo's ci.yml to canonical names**

Same transformation. These repos have more complex CI (version
matrices, integration tests) so the naming convention for
version-matrix expansions from the registry is critical here.

- [ ] **Step 3: Verify per-repo**

For each repo, push test branch, open draft PR, verify check names.

- [ ] **Step 4: Audit each repo**

```bash
st-github-config audit --owner wphillipmoore --project 3
```

Expected: All repos in project 3 compliant.

- [ ] **Step 5: Merge all PRs**

- [ ] **Step 6: Commit tracking note**

```
docs: mark mq-rest-admin CI refactored

Ref #173
```

---

## Phase 12: Fleet-Wide Apply

### Task 27: Verify fleet alignment with audit

- [ ] **Step 1: Run fleet-wide audit**

```bash
st-github-config audit --owner wphillipmoore --project 3
st-github-config audit --owner wphillipmoore --project 4
```

Expected: All repos compliant. If any are non-compliant, fix the
CI workflows in those repos first (the safety gate prevents
applying rulesets until CI produces the expected check names).

- [ ] **Step 2: Review diff output for any remaining gaps**

```bash
st-github-config diff --owner wphillipmoore --project 3
st-github-config diff --owner wphillipmoore --project 4
```

Inspect each diff item — any unexpected delta must be investigated
before applying.

### Task 28: Apply canonical configuration fleet-wide

- [ ] **Step 1: Apply to standards-project repos first**

```bash
st-github-config apply --owner wphillipmoore --project 4
```

Confirm each repo. Verify rulesets are correct via GitHub UI.

- [ ] **Step 2: Apply to mq-rest-admin repos**

```bash
st-github-config apply --owner wphillipmoore --project 3
```

Confirm each repo. Verify rulesets.

- [ ] **Step 3: Verify — open a test PR on one repo**

Open a PR on one repo and confirm:
- Required status checks are enforced
- Both `main` and `develop` are protected
- Auto-merge is blocked
- The correct checks must pass before merge is allowed

- [ ] **Step 4: Remove stale classic branch protection**

For any repos where `st-github-config audit` flagged classic branch
protection, remove it:

```bash
gh api -X DELETE "repos/wphillipmoore/<repo>/branches/develop/protection"
gh api -X DELETE "repos/wphillipmoore/<repo>/branches/main/protection"
```

---

## Phase 13: Cleanup

### Task 29: Remove `scripts/dev/*.sh` from all repos

- [ ] **Step 1: Remove from standard-tooling**

Delete: `scripts/dev/lint.sh`, `scripts/dev/typecheck.sh`,
`scripts/dev/test.sh`, `scripts/dev/audit.sh`

Verify `st-validate` still passes (it no longer calls these).

- [ ] **Step 2: Remove from all consuming repos**

For each repo in projects 3 and 4 that has `scripts/dev/*.sh`:
- Open a PR removing the scripts
- Verify CI still passes (CI workflows should already use
  canonical check names, not the scripts)
- Merge

- [ ] **Step 3: Commit (in standard-tooling)**

```
chore: remove scripts/dev/*.sh

These scripts are fully replaced by st-validate's per-language
command registry. No consuming repos reference them.

Ref #173
```

### Task 30: Remove deprecated entry points

**Files:**
- Remove: `src/standard_tooling/bin/validate_local.py`
- Remove: `src/standard_tooling/bin/validate_local_lang.py`
- Remove: `src/standard_tooling/bin/validate_local_common_container.py`
- Remove: `src/standard_tooling/bin/docker_test.py`
- Modify: `pyproject.toml`
- Remove associated tests

- [ ] **Step 1: Remove deprecated modules and their tests**

Delete the four deprecated source files and their test files.

- [ ] **Step 2: Remove entry points from pyproject.toml**

Remove these entries from `[project.scripts]`:

```
st-validate-local
st-validate-local-python
st-validate-local-rust
st-validate-local-go
st-validate-local-java
st-validate-local-common
st-docker-test
```

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest --cov=standard_tooling --cov-branch --cov-fail-under=100`

Expected: All pass with 100% coverage (dead code removed).

- [ ] **Step 4: Run validation**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-173-github-config-enforcement && st-validate`

Note: `st-validate` is host-orchestrated and calls `st-docker-run`
internally — do not run it inside a container.

- [ ] **Step 5: Commit**

```
chore: remove deprecated st-validate-local and st-docker-test

Entry points deprecated in the previous minor version are now
removed. st-validate is the canonical validation tool;
st-docker-run is the only container execution tool.

Ref #173
```

### Task 31: Update documentation and CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/site/docs/guides/ci-architecture.md` (if exists)

- [ ] **Step 1: Update CLAUDE.md validation section**

Replace all references to `st-validate-local` with `st-validate`.
Update the "Validation" and "Docker-First Testing" sections to
reflect the new host-orchestrated model.

- [ ] **Step 2: Update CI architecture documentation**

Update any documentation referencing the old validation pipeline,
`scripts/dev/*.sh`, or `st-docker-test`.

- [ ] **Step 3: Create GitHub security reevaluation issue**

Create a GitHub issue to holistically evaluate GitHub's security
offerings (vulnerability alerts, Dependabot, and any other
features) against the current Trivy/Semgrep/language-audit
toolchain. Per the spec's reevaluation note — determine whether any
GitHub-native security features would add coverage beyond what the
current toolchain provides.

- [ ] **Step 4: Commit**

```
docs: update documentation for st-validate and cleanup

Reflects removal of st-validate-local, scripts/dev/*.sh, and
st-docker-test. Documents the new host-orchestrated validation
model.

Ref #173
```

---

## Summary

| Phase | Scope | Can ship independently |
|---|---|---|
| 1–7 | `st-github-config` tool (derivation + CLI) | Yes — immediately fixes rulesets |
| **Gate** | Canonical check name registry (separate cycle) | Blocks 8–13 |
| 8 | `st-validate` rename + host execution | Yes (with registry) |
| 9 | Version matrix + command registry + filtering | Yes (with Phase 8) |
| 10 | Common checks consolidation | Yes (with Phase 9) |
| 11 | CI workflow refactor (13 repos) | Yes (with Phase 10) |
| 12 | Fleet-wide `st-github-config apply` | Yes (with Phase 11) |
| 13 | Cleanup (remove dead code/scripts) | Yes (with Phase 12) |

Total: 31 tasks, ~120 steps. Phases are ordered by dependency.
Each phase produces a shippable increment.
