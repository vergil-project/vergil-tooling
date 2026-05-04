"""Tests for standard_tooling.lib.github_config."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from standard_tooling.lib.config import (
    CiConfig,
    GithubOverrides,
    MarkdownlintConfig,
    ProjectConfig,
    StConfig,
)
from standard_tooling.lib.github_config import (
    _fetch_vulnerability_alerts,
    _lang_has_check,
    compute_desired_state,
    desired_actions_permissions,
    desired_branch_protection_ruleset,
    desired_ci_gates_ruleset,
    desired_repo_settings,
    desired_security_settings,
    desired_tag_protection_ruleset,
    fetch_actual_state,
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


def test_desired_security_settings() -> None:
    s = desired_security_settings()
    assert s.secret_scanning == "enabled"  # noqa: S105
    assert s.secret_scanning_push_protection == "enabled"  # noqa: S105
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


# ---------------------------------------------------------------------------
# CI gates ruleset tests
# ---------------------------------------------------------------------------


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


def _check_names(ruleset) -> list[str]:  # noqa: ANN001
    """Extract check context names from a CI gates ruleset."""
    status_rule = next(rule for rule in ruleset.rules if rule["type"] == "required_status_checks")
    return [c["context"] for c in status_rule["parameters"]["required_status_checks"]]


def test_ci_gates_structure() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    assert r.name == "CI gates"
    assert r.target == "branch"
    assert r.enforcement == "active"
    assert r.ref_include == ["refs/heads/main", "refs/heads/develop"]
    assert r.bypass_actors == []


def test_ci_gates_strict_policy() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    status_rule = next(rule for rule in r.rules if rule["type"] == "required_status_checks")
    assert status_rule["parameters"]["strict_required_status_checks_policy"] is True


def test_ci_gates_always_includes_common_and_security() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    check_names = _check_names(r)
    assert "quality / common" in check_names
    assert "security / trivy" in check_names
    assert "security / semgrep" in check_names
    assert "security / standards" in check_names


def test_ci_gates_codeql_for_supported_language() -> None:
    r = desired_ci_gates_ruleset(_project(language="python"), _ci())
    assert "security / codeql" in _check_names(r)


def test_ci_gates_no_codeql_for_shell() -> None:
    r = desired_ci_gates_ruleset(_project(language="shell"), _ci())
    assert "security / codeql" not in _check_names(r)


def test_ci_gates_versioned_checks_per_version() -> None:
    ci = _ci(versions=["3.12", "3.13", "3.14"])
    r = desired_ci_gates_ruleset(_project(), ci)
    names = _check_names(r)
    assert "quality / lint / 3.12" in names
    assert "quality / lint / 3.13" in names
    assert "quality / lint / 3.14" in names
    assert "quality / typecheck / 3.12" in names
    assert "test / unit / 3.12" in names
    assert "audit / dependencies / 3.12" in names


def test_ci_gates_integration_tests_when_enabled() -> None:
    ci = _ci(versions=["3.12", "3.13"], integration_tests=True)
    r = desired_ci_gates_ruleset(_project(), ci)
    names = _check_names(r)
    assert "test / integration / 3.12" in names
    assert "test / integration / 3.13" in names


def test_ci_gates_no_integration_tests_when_disabled() -> None:
    ci = _ci(versions=["3.12"], integration_tests=False)
    r = desired_ci_gates_ruleset(_project(), ci)
    names = _check_names(r)
    assert not any("test / integration" in n for n in names)


def test_ci_gates_release_version_bump_present() -> None:
    r = desired_ci_gates_ruleset(_project(release_model="tagged-release"), _ci())
    assert "release / version-bump" in _check_names(r)


def test_ci_gates_no_release_when_none() -> None:
    r = desired_ci_gates_ruleset(_project(release_model="none"), _ci())
    assert "release / version-bump" not in _check_names(r)


def test_ci_gates_shell_has_no_versioned_checks() -> None:
    r = desired_ci_gates_ruleset(_project(language="shell"), _ci(versions=["latest"]))
    names = _check_names(r)
    assert "quality / common" in names
    assert not any("quality / lint" in n for n in names)
    assert not any("test / unit" in n for n in names)


def test_lang_has_check_returns_false_for_unknown_check() -> None:
    assert _lang_has_check("python", "nonexistent") is False


# ---------------------------------------------------------------------------
# compute_desired_state tests
# ---------------------------------------------------------------------------


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


def test_compute_desired_state_includes_repo_settings() -> None:
    state = compute_desired_state(_st_config())
    assert state.repo_settings.default_branch == "develop"


def test_compute_desired_state_includes_security() -> None:
    state = compute_desired_state(_st_config())
    assert state.security.secret_scanning == "enabled"  # noqa: S105


def test_compute_desired_state_includes_actions() -> None:
    state = compute_desired_state(_st_config())
    assert state.actions_permissions.allowed_actions == "selected"


# ---------------------------------------------------------------------------
# fetch_actual_state tests
# ---------------------------------------------------------------------------


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
        "security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
            "dependabot_security_updates": {"status": "disabled"},
        },
    }

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
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
    assert actual.repo_settings.delete_branch_on_merge is True
    assert actual.security.secret_scanning == "enabled"  # noqa: S105
    assert actual.security.vulnerability_alerts is False
    assert actual.actions_permissions.default_workflow_permissions == "read"
    assert actual.actions_permissions.patterns_allowed == [
        "actions/*",
        "wphillipmoore/*",
    ]
    assert actual.rulesets == []


def test_fetch_actual_state_with_rulesets() -> None:
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "security_and_analysis": {},
    }
    ruleset_summary: list[object] = [{"id": 42}]
    ruleset_detail: dict[str, object] = {
        "name": "Branch protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
        "bypass_actors": [],
        "rules": [{"type": "deletion"}],
    }

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return ruleset_summary
        if endpoint == "repos/o/r/rulesets/42":
            return ruleset_detail
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
        actual = fetch_actual_state("o/r")

    assert len(actual.rulesets) == 1
    assert actual.rulesets[0].name == "Branch protection"
    assert actual.rulesets[0].ref_include == ["refs/heads/main"]


def test_fetch_actual_state_no_selected_actions_skips_patterns() -> None:
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "security_and_analysis": {},
    }
    call_log: list[str] = []

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        call_log.append(endpoint)
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "all"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
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
        actual = fetch_actual_state("o/r")

    assert "repos/o/r/actions/permissions/selected-actions" not in call_log
    assert actual.actions_permissions.patterns_allowed == []


def test_fetch_actual_state_missing_security_and_analysis() -> None:
    """Cover branch where security_and_analysis is None/missing."""
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        # no security_and_analysis key at all
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
        actual = fetch_actual_state("o/r")

    assert actual.security.secret_scanning == "disabled"  # noqa: S105
    assert actual.security.secret_scanning_push_protection == "disabled"  # noqa: S105
    assert actual.security.dependabot_security_updates == "disabled"


def test_fetch_actual_state_rulesets_edge_cases() -> None:
    """Cover branches: non-dict summary, missing id, non-dict detail, non-list rulesets."""
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "security_and_analysis": {},
    }
    # Include: a non-dict entry, a dict without id, and a valid one
    ruleset_summary: list[object] = [
        "not-a-dict",
        {"name": "no id here"},
        {"id": 99},
    ]

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return ruleset_summary
        if endpoint == "repos/o/r/rulesets/99":
            # Return a list instead of dict to trigger non-dict detail branch
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
        actual = fetch_actual_state("o/r")

    # All invalid rulesets should be skipped
    assert actual.rulesets == []


def test_fetch_actual_state_rulesets_not_a_list() -> None:
    """Cover branch where raw_rulesets is a dict (not a list)."""
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "security_and_analysis": {},
    }

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return {"error": "unexpected format"}
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
        actual = fetch_actual_state("o/r")

    assert actual.rulesets == []


def test_fetch_actual_state_selected_actions_non_dict_response() -> None:
    """Cover branches where selected-actions response is non-dict or patterns is non-list."""
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
            return {"allowed_actions": "selected"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        if endpoint == "repos/o/r/actions/permissions/selected-actions":
            # Return a list instead of dict to trigger non-dict branch
            return []
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

    assert actual.actions_permissions.patterns_allowed == []


def test_fetch_actual_state_selected_actions_non_list_patterns() -> None:
    """Cover branch where patterns_allowed is not a list."""
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
            return {"allowed_actions": "selected"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        if endpoint == "repos/o/r/actions/permissions/selected-actions":
            return {"patterns_allowed": "not-a-list"}
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

    assert actual.actions_permissions.patterns_allowed == []


def test_fetch_vulnerability_alerts_enabled() -> None:
    cp = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 204 No Content\n", stderr=""
    )
    with patch("standard_tooling.lib.github_config.subprocess.run", return_value=cp):
        assert _fetch_vulnerability_alerts("o/r") is True


def test_fetch_vulnerability_alerts_disabled() -> None:
    cp = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 404 Not Found\n", stderr=""
    )
    with patch("standard_tooling.lib.github_config.subprocess.run", return_value=cp):
        assert _fetch_vulnerability_alerts("o/r") is False
