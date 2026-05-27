"""Tests for vergil_tooling.lib.github_config."""

from __future__ import annotations

import subprocess
from typing import cast
from unittest.mock import patch

from vergil_tooling.lib.config import (
    CiConfig,
    ContainerConfig,
    MarkdownlintConfig,
    ProjectConfig,
    PublishConfig,
    VergilConfig,
)
from vergil_tooling.lib.github_config import (
    DesiredRuleset,
    FetchResult,
    _apply_actions_permissions,
    _apply_repo_settings,
    _apply_rulesets,
    _apply_security_settings,
    _cleanup_classic_branch_protection,
    _fetch_vulnerability_alerts,
    _lang_has_check,
    _normalize_rules,
    _ruleset_body,
    apply_desired_state,
    compute_desired_state,
    compute_diff,
    desired_actions_permissions,
    desired_branch_protection_ruleset,
    desired_ci_gates_ruleset,
    desired_repo_settings,
    desired_security_settings,
    desired_tag_protection_ruleset,
    fetch_actual_state,
)


def test_desired_repo_settings_are_fixed() -> None:
    s = desired_repo_settings(visibility="public", is_org=True)
    assert s.default_branch == "develop"
    assert s.allow_auto_merge is False
    assert s.delete_branch_on_merge is True
    assert s.allow_merge_commit is True
    assert s.allow_squash_merge is True
    assert s.allow_rebase_merge is True
    assert s.has_issues is True
    assert s.has_projects is True
    assert s.has_wiki is True


def test_desired_repo_settings_public_org_omits_forking() -> None:
    s = desired_repo_settings(visibility="public", is_org=True)
    assert s.allow_forking is None


def test_desired_repo_settings_private_disallows_forking() -> None:
    s = desired_repo_settings(visibility="private", is_org=True)
    assert s.allow_forking is False


def test_desired_repo_settings_new_hardcoded_values() -> None:
    s = desired_repo_settings(visibility="public", is_org=True)
    assert s.allow_update_branch is True
    assert s.has_downloads is False
    assert s.merge_commit_title == "MERGE_MESSAGE"
    assert s.merge_commit_message == "PR_TITLE"
    assert s.squash_merge_commit_title == "COMMIT_OR_PR_TITLE"
    assert s.squash_merge_commit_message == "COMMIT_MESSAGES"
    assert s.web_commit_signoff_required is True


def test_desired_security_settings_public() -> None:
    s = desired_security_settings(visibility="public")
    assert s.secret_scanning == "enabled"  # noqa: S105
    assert s.secret_scanning_push_protection == "enabled"  # noqa: S105
    assert s.vulnerability_alerts is False
    assert s.dependabot_security_updates == "disabled"


def test_desired_security_settings_private_skips_ghas_features() -> None:
    s = desired_security_settings(visibility="private")
    assert s.secret_scanning is None
    assert s.secret_scanning_push_protection is None
    assert s.vulnerability_alerts is False
    assert s.dependabot_security_updates == "disabled"


def test_desired_actions_permissions_base_only() -> None:
    a = desired_actions_permissions("go")
    assert a.default_workflow_permissions == "read"
    assert a.can_approve_pull_request_reviews is False
    assert a.allowed_actions == "selected"
    assert a.patterns_allowed == [
        "actions/*",
        "docker/*",
        "github/*",
        "vergil-project/*",
    ]


def test_desired_actions_permissions_with_language_patterns() -> None:
    a = desired_actions_permissions("rust")
    assert a.patterns_allowed == [
        "actions-rust-lang/*",
        "actions/*",
        "docker/*",
        "github/*",
        "swatinem/*",
        "vergil-project/*",
    ]


def test_desired_actions_permissions_python() -> None:
    a = desired_actions_permissions("python")
    assert a.patterns_allowed == [
        "actions/*",
        "astral-sh/*",
        "docker/*",
        "github/*",
        "pypa/*",
        "vergil-project/*",
    ]


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
    params = cast("dict[str, object]", pr_rule["parameters"])
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
    language: str | None = "python",
    release_model: str = "tagged-release",
) -> ProjectConfig:
    return ProjectConfig(
        repository_type="library",
        versioning_scheme="semver",
        branching_model="library-release",
        release_model=release_model,
        primary_language=language,
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


def _check_names(ruleset: DesiredRuleset) -> list[str]:
    """Extract check context names from a CI gates ruleset."""
    return [str(c["context"]) for c in _checks(ruleset)]


def _checks(ruleset: DesiredRuleset) -> list[dict[str, object]]:
    """Extract all check dicts from a CI gates ruleset."""
    status_rule = next(rule for rule in ruleset.rules if rule["type"] == "required_status_checks")
    params = cast("dict[str, list[dict[str, object]]]", status_rule["parameters"])
    return params["required_status_checks"]


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
    params = cast("dict[str, object]", status_rule["parameters"])
    assert params["strict_required_status_checks_policy"] is True


def test_ci_gates_always_includes_common_and_security() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    check_names = _check_names(r)
    assert "quality / common" in check_names
    assert "security / trivy" in check_names
    assert "security / semgrep" in check_names
    assert "security / standards" in check_names
    assert "Trivy" in check_names
    assert "Semgrep OSS" in check_names


def test_ci_gates_ghas_checks_use_ghas_integration_id() -> None:
    r = desired_ci_gates_ruleset(_project(), _ci())
    checks = _checks(r)
    ghas_names = ("Trivy", "Semgrep OSS")
    ghas_checks = {c["context"]: c["integration_id"] for c in checks if c["context"] in ghas_names}
    assert ghas_checks == {"Trivy": 57789, "Semgrep OSS": 57789}


def test_ci_gates_codeql_for_supported_language() -> None:
    r = desired_ci_gates_ruleset(_project(language="python"), _ci())
    assert "security / codeql" in _check_names(r)


def test_ci_gates_no_codeql_without_language() -> None:
    r = desired_ci_gates_ruleset(_project(language=None), _ci())
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
    assert "version / version-bump" in _check_names(r)


def test_ci_gates_no_release_when_none() -> None:
    r = desired_ci_gates_ruleset(_project(release_model="none"), _ci())
    assert "version / version-bump" not in _check_names(r)


def test_ci_gates_no_language_has_no_versioned_checks() -> None:
    r = desired_ci_gates_ruleset(_project(language=None), _ci(versions=["latest"]))
    names = _check_names(r)
    assert "quality / common" in names
    assert not any("quality / lint" in n for n in names)
    assert not any("test / unit" in n for n in names)


def test_lang_has_check_returns_false_for_unknown_check() -> None:
    assert _lang_has_check("python", "nonexistent") is False


# ---------------------------------------------------------------------------
# compute_desired_state tests
# ---------------------------------------------------------------------------


def _vergil_config(
    *,
    language: str = "python",
    release_model: str = "tagged-release",
    versions: list[str] | None = None,
    integration_tests: bool = False,
) -> VergilConfig:
    return VergilConfig(
        project=_project(language=language, release_model=release_model),
        dependencies={"vergil": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=_ci(versions=versions or ["3.14"], integration_tests=integration_tests),
        publish=PublishConfig(release=False, docs=True, consumer_refresh=None),
        container=ContainerConfig(env_prefixes=[]),
    )


def test_compute_desired_state_has_three_rulesets() -> None:
    state = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    assert len(state.rulesets) == 3
    names = [r.name for r in state.rulesets]
    assert "Branch protection" in names
    assert "Tag protection" in names
    assert "CI gates" in names


def test_compute_desired_state_includes_repo_settings() -> None:
    state = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    assert state.repo_settings.default_branch == "develop"


def test_compute_desired_state_includes_security() -> None:
    state = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    assert state.security.secret_scanning == "enabled"  # noqa: S105


def test_compute_desired_state_includes_actions() -> None:
    state = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    assert state.actions_permissions.allowed_actions == "selected"
    assert "pypa/*" in state.actions_permissions.patterns_allowed


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
            return {"patterns_allowed": ["actions/*", "vergil-project/*"]}
        return {}

    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

    assert actual.repo_settings.default_branch == "develop"
    assert actual.repo_settings.delete_branch_on_merge is True
    assert actual.security.secret_scanning == "enabled"  # noqa: S105
    assert actual.security.vulnerability_alerts is False
    assert actual.actions_permissions.default_workflow_permissions == "read"
    assert actual.actions_permissions.patterns_allowed == [
        "actions/*",
        "vergil-project/*",
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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")
    actual = result.state

    assert actual.actions_permissions.patterns_allowed == []


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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")

    assert result.visibility == "private"


def test_fetch_vulnerability_alerts_enabled() -> None:
    cp = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 204 No Content\n", stderr=""
    )
    with patch("vergil_tooling.lib.github_config.subprocess.run", return_value=cp):
        assert _fetch_vulnerability_alerts("o/r") is True


def test_fetch_vulnerability_alerts_disabled() -> None:
    cp = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 404 Not Found\n", stderr=""
    )
    with patch("vergil_tooling.lib.github_config.subprocess.run", return_value=cp):
        assert _fetch_vulnerability_alerts("o/r") is False


# ---------------------------------------------------------------------------
# Diff computation tests
# ---------------------------------------------------------------------------


def test_diff_identical_states_is_empty() -> None:
    state = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    diff = compute_diff(desired=state, actual=state)
    assert diff.is_compliant()
    assert diff.items == []


def test_diff_detects_repo_setting_mismatch() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual.repo_settings.allow_auto_merge = True
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field == "repo_settings.allow_auto_merge" for d in diff.items)


def test_diff_detects_missing_ruleset() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual.rulesets = []
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field.startswith("rulesets.") for d in diff.items)


def test_diff_detects_extra_ruleset() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual.rulesets.append(
        DesiredRuleset(
            name="Extra",
            target="branch",
            enforcement="active",
            ref_include=[],
            bypass_actors=[],
            rules=[],
        )
    )
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field == "rulesets.Extra" and d.expected == "absent" for d in diff.items)


def test_diff_detects_actions_permission_mismatch() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual.actions_permissions.default_workflow_permissions = "write"
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field == "actions_permissions.default_workflow_permissions" for d in diff.items)


def test_diff_detects_security_mismatch() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual.security.vulnerability_alerts = True
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field == "security.vulnerability_alerts" for d in diff.items)


def test_diff_detects_new_repo_setting_drift() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual.repo_settings.merge_commit_title = "PR_TITLE"
    actual.repo_settings.web_commit_signoff_required = False
    diff = compute_diff(desired=desired, actual=actual)
    fields = {item.field for item in diff.items}
    assert "repo_settings.merge_commit_title" in fields
    assert "repo_settings.web_commit_signoff_required" in fields


def test_diff_records_skipped_security_fields_for_private_repo() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="private", is_org=True)
    actual = compute_desired_state(_vergil_config(), visibility="private", is_org=True)
    actual.security.secret_scanning = "enabled"  # noqa: S105
    actual.security.secret_scanning_push_protection = "enabled"  # noqa: S105
    diff = compute_diff(desired=desired, actual=actual)
    assert diff.is_compliant()
    assert "security.secret_scanning" in diff.skipped
    assert "security.secret_scanning_push_protection" in diff.skipped


def test_diff_public_repo_has_no_security_skipped() -> None:
    state = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    diff = compute_diff(desired=state, actual=state)
    assert diff.is_compliant()
    assert not any(s.startswith("security.") for s in diff.skipped)


# ---------------------------------------------------------------------------
# Apply tests
# ---------------------------------------------------------------------------


def test_apply_repo_settings_calls_write_json() -> None:
    settings = desired_repo_settings(visibility="public", is_org=True)
    with patch("vergil_tooling.lib.github_config.github.write_json") as mock_write:
        _apply_repo_settings("o/r", settings)
    mock_write.assert_called_once()
    call_args = mock_write.call_args
    assert call_args[0][0] == "PATCH"
    assert call_args[0][1] == "repos/o/r"
    body = call_args[0][2]
    assert body["default_branch"] == "develop"
    assert body["delete_branch_on_merge"] is True


def test_apply_repo_settings_includes_new_fields() -> None:
    settings = desired_repo_settings(visibility="public", is_org=True)
    with patch("vergil_tooling.lib.github_config.github.write_json") as mock_write:
        _apply_repo_settings("o/r", settings)
    body = mock_write.call_args[0][2]
    assert "allow_forking" not in body
    assert body["allow_update_branch"] is True
    assert body["has_downloads"] is False
    assert body["merge_commit_title"] == "MERGE_MESSAGE"
    assert body["merge_commit_message"] == "PR_TITLE"
    assert body["squash_merge_commit_title"] == "COMMIT_OR_PR_TITLE"
    assert body["squash_merge_commit_message"] == "COMMIT_MESSAGES"
    assert body["web_commit_signoff_required"] is True


def test_apply_security_settings_enables_vuln_alerts() -> None:
    from vergil_tooling.lib.github_config import DesiredSecuritySettings

    sec = DesiredSecuritySettings(
        secret_scanning="enabled",  # noqa: S106
        secret_scanning_push_protection="enabled",  # noqa: S106
        vulnerability_alerts=True,
        dependabot_security_updates="disabled",
    )
    with (
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_security_settings("o/r", sec)
    assert mock_write.call_count == 2
    # First call is PATCH repos/o/r with security_and_analysis
    assert mock_write.call_args_list[0][0][0] == "PATCH"
    # Second call is PUT vulnerability-alerts
    assert mock_write.call_args_list[1][0][0] == "PUT"
    assert "vulnerability-alerts" in mock_write.call_args_list[1][0][1]
    mock_del.assert_not_called()


def test_apply_security_settings_disables_vuln_alerts() -> None:
    from vergil_tooling.lib.github_config import DesiredSecuritySettings

    sec = DesiredSecuritySettings(
        secret_scanning="enabled",  # noqa: S106
        secret_scanning_push_protection="enabled",  # noqa: S106
        vulnerability_alerts=False,
        dependabot_security_updates="disabled",
    )
    with (
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_security_settings("o/r", sec)
    assert mock_write.call_count == 1
    mock_del.assert_called_once_with("repos/o/r/vulnerability-alerts")


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


def test_apply_actions_permissions_selected() -> None:
    perms = desired_actions_permissions("python")
    with patch("vergil_tooling.lib.github_config.github.write_json") as mock_write:
        _apply_actions_permissions("o/r", perms)
    assert mock_write.call_count == 3
    endpoints = [c[0][1] for c in mock_write.call_args_list]
    assert "repos/o/r/actions/permissions" in endpoints
    assert "repos/o/r/actions/permissions/workflow" in endpoints
    assert "repos/o/r/actions/permissions/selected-actions" in endpoints


def test_apply_actions_permissions_not_selected_skips_patterns() -> None:
    from vergil_tooling.lib.github_config import DesiredActionsPermissions

    perms = DesiredActionsPermissions(
        default_workflow_permissions="read",
        can_approve_pull_request_reviews=False,
        allowed_actions="all",
        patterns_allowed=[],
    )
    with patch("vergil_tooling.lib.github_config.github.write_json") as mock_write:
        _apply_actions_permissions("o/r", perms)
    assert mock_write.call_count == 2
    endpoints = [c[0][1] for c in mock_write.call_args_list]
    assert "repos/o/r/actions/permissions/selected-actions" not in endpoints


def test_apply_rulesets_creates_new() -> None:
    ruleset = desired_branch_protection_ruleset()
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value=[],
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_rulesets("o/r", [ruleset])
    mock_write.assert_called_once()
    assert mock_write.call_args[0][0] == "POST"
    assert mock_write.call_args[0][1] == "repos/o/r/rulesets"
    mock_del.assert_not_called()


def test_apply_rulesets_updates_existing() -> None:
    ruleset = desired_branch_protection_ruleset()
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value=[{"name": "Branch protection", "id": 42}],
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_rulesets("o/r", [ruleset])
    mock_write.assert_called_once()
    assert mock_write.call_args[0][0] == "PUT"
    assert mock_write.call_args[0][1] == "repos/o/r/rulesets/42"
    mock_del.assert_not_called()


def test_apply_rulesets_deletes_extra() -> None:
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value=[{"name": "Old rule", "id": 99}],
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_rulesets("o/r", [])
    mock_write.assert_not_called()
    mock_del.assert_called_once_with("repos/o/r/rulesets/99")


def test_apply_rulesets_skips_invalid_entries() -> None:
    ruleset = desired_branch_protection_ruleset()
    # Mix of invalid entries: non-dict, missing name, missing id
    existing: list[object] = [
        "not-a-dict",
        {"name": 123, "id": 1},  # name not str
        {"name": "Valid", "id": "not-int"},  # id not int
        {"name": "Branch protection", "id": 7},
    ]
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value=existing,
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete"),
    ):
        _apply_rulesets("o/r", [ruleset])
    assert mock_write.call_args[0][0] == "PUT"
    assert mock_write.call_args[0][1] == "repos/o/r/rulesets/7"


def test_apply_rulesets_non_list_response_creates_all() -> None:
    ruleset = desired_branch_protection_ruleset()
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value={"error": "unexpected"},
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_rulesets("o/r", [ruleset])
    mock_write.assert_called_once()
    assert mock_write.call_args[0][0] == "POST"
    mock_del.assert_not_called()


def test_ruleset_body_structure() -> None:
    ruleset = desired_tag_protection_ruleset()
    body = _ruleset_body(ruleset)
    assert body["name"] == "Tag protection"
    assert body["target"] == "tag"
    assert body["enforcement"] == "active"
    assert body["conditions"] == {
        "ref_name": {"include": ["refs/tags/v*.*.*"], "exclude": []},
    }
    assert body["bypass_actors"] == ruleset.bypass_actors
    assert body["rules"] == ruleset.rules


def test_apply_desired_state_orchestrates_all() -> None:
    state = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    with (
        patch("vergil_tooling.lib.github_config._apply_repo_settings") as mock_repo,
        patch("vergil_tooling.lib.github_config._apply_security_settings") as mock_sec,
        patch("vergil_tooling.lib.github_config._apply_actions_permissions") as mock_actions,
        patch("vergil_tooling.lib.github_config._apply_rulesets") as mock_rulesets,
        patch(
            "vergil_tooling.lib.github_config._cleanup_classic_branch_protection",
            return_value=[],
        ) as mock_cleanup,
    ):
        result = apply_desired_state("o/r", state)
    mock_repo.assert_called_once_with("o/r", state.repo_settings)
    mock_sec.assert_called_once_with("o/r", state.security)
    mock_actions.assert_called_once_with("o/r", state.actions_permissions)
    mock_rulesets.assert_called_once_with("o/r", state.rulesets)
    mock_cleanup.assert_called_once_with("o/r", state.rulesets)
    assert result == []


# ---------------------------------------------------------------------------
# Classic branch protection cleanup tests
# ---------------------------------------------------------------------------


def test_cleanup_removes_legacy_protection_for_covered_branches() -> None:
    rulesets = [desired_branch_protection_ruleset()]
    with patch(
        "vergil_tooling.lib.github_config.github.delete_if_exists",
        return_value=True,
    ) as mock_del:
        removed = _cleanup_classic_branch_protection("o/r", rulesets)
    assert sorted(removed) == ["develop", "main"]
    assert mock_del.call_count == 2
    endpoints = sorted(c[0][0] for c in mock_del.call_args_list)
    assert endpoints == [
        "repos/o/r/branches/develop/protection",
        "repos/o/r/branches/main/protection",
    ]


def test_cleanup_returns_empty_when_no_legacy_protection() -> None:
    rulesets = [desired_branch_protection_ruleset()]
    with patch(
        "vergil_tooling.lib.github_config.github.delete_if_exists",
        return_value=False,
    ):
        removed = _cleanup_classic_branch_protection("o/r", rulesets)
    assert removed == []


def test_cleanup_skips_tag_rulesets() -> None:
    rulesets = [desired_tag_protection_ruleset()]
    with patch(
        "vergil_tooling.lib.github_config.github.delete_if_exists",
    ) as mock_del:
        removed = _cleanup_classic_branch_protection("o/r", rulesets)
    mock_del.assert_not_called()
    assert removed == []


def test_cleanup_ignores_non_heads_refs() -> None:
    rulesets = [
        DesiredRuleset(
            name="Mixed refs",
            target="branch",
            enforcement="active",
            ref_include=["refs/heads/main", "refs/tags/v*"],
            bypass_actors=[],
            rules=[],
        ),
    ]
    with patch(
        "vergil_tooling.lib.github_config.github.delete_if_exists",
        return_value=True,
    ) as mock_del:
        removed = _cleanup_classic_branch_protection("o/r", rulesets)
    mock_del.assert_called_once_with("repos/o/r/branches/main/protection")
    assert removed == ["main"]


def test_cleanup_deduplicates_branches_across_rulesets() -> None:
    rulesets = [
        desired_branch_protection_ruleset(),
        DesiredRuleset(
            name="CI gates",
            target="branch",
            enforcement="active",
            ref_include=["refs/heads/main", "refs/heads/develop"],
            bypass_actors=[],
            rules=[],
        ),
    ]
    with patch(
        "vergil_tooling.lib.github_config.github.delete_if_exists",
        return_value=True,
    ) as mock_del:
        removed = _cleanup_classic_branch_protection("o/r", rulesets)
    # Should only call once per unique branch
    assert mock_del.call_count == 2
    assert sorted(removed) == ["develop", "main"]


def test_normalize_rules_strips_default_params() -> None:
    rules: list[object] = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "do_not_enforce_on_create": False,
                "required_status_checks": [],
            },
        },
    ]
    result = _normalize_rules(rules)
    assert len(result) == 1
    params = cast("dict[str, object]", result[0]["parameters"])
    assert "do_not_enforce_on_create" not in params
    assert params["strict_required_status_checks_policy"] is True


def test_normalize_rules_skips_non_dict_entries() -> None:
    rules: list[object] = [
        "not-a-dict",
        {"type": "deletion"},
    ]
    result = _normalize_rules(rules)
    assert len(result) == 1
    assert result[0] == {"type": "deletion"}


# ---------------------------------------------------------------------------
# Publish config in desired state
# ---------------------------------------------------------------------------


def test_compute_desired_state_includes_publish() -> None:
    config = _vergil_config()
    state = compute_desired_state(config, visibility="public", is_org=True)
    assert state.publish is not None
    assert state.publish.release is False
    assert state.publish.docs is True


def test_compute_desired_state_publish_release_true() -> None:
    config = VergilConfig(
        project=_project(),
        dependencies={"vergil": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=_ci(),
        publish=PublishConfig(release=True, docs=True, consumer_refresh=None),
        container=ContainerConfig(env_prefixes=[]),
    )
    state = compute_desired_state(config, visibility="public", is_org=True)
    assert state.publish.release is True
    assert state.publish.docs is True


# ---------------------------------------------------------------------------
# Issue #666: allow_forking on user-owned repos
# ---------------------------------------------------------------------------


def test_desired_repo_settings_user_repo_allow_forking_is_none() -> None:
    s = desired_repo_settings(visibility="public", is_org=False)
    assert s.allow_forking is None


def test_desired_repo_settings_org_repo_allow_forking_set() -> None:
    s = desired_repo_settings(visibility="public", is_org=True)
    assert s.allow_forking is None
    s2 = desired_repo_settings(visibility="private", is_org=True)
    assert s2.allow_forking is False


def test_apply_repo_settings_omits_allow_forking_when_none() -> None:
    settings = desired_repo_settings(visibility="public", is_org=False)
    with patch("vergil_tooling.lib.github_config.github.write_json") as mock_write:
        _apply_repo_settings("o/r", settings)
    body = mock_write.call_args[0][2]
    assert "allow_forking" not in body


def test_apply_repo_settings_includes_allow_forking_for_private_org() -> None:
    settings = desired_repo_settings(visibility="private", is_org=True)
    with patch("vergil_tooling.lib.github_config.github.write_json") as mock_write:
        _apply_repo_settings("o/r", settings)
    body = mock_write.call_args[0][2]
    assert body["allow_forking"] is False


def test_diff_skips_allow_forking_when_desired_is_none() -> None:
    desired = compute_desired_state(_vergil_config(), visibility="public", is_org=False)
    actual = compute_desired_state(_vergil_config(), visibility="public", is_org=True)
    actual.repo_settings.allow_forking = False
    diff = compute_diff(desired=desired, actual=actual)
    assert not any(d.field == "repo_settings.allow_forking" for d in diff.items)


def test_fetch_actual_state_extracts_owner_type_organization() -> None:
    repo_json: dict[str, object] = {
        "default_branch": "develop",
        "owner": {"type": "Organization"},
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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")

    assert result.owner_type == "Organization"


def test_fetch_actual_state_defaults_owner_type_to_user() -> None:
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
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=mock_read_json,
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")

    assert result.owner_type == "User"
