"""Tests for standard_tooling.lib.github_config."""

from __future__ import annotations

from standard_tooling.lib.config import CiConfig, ProjectConfig
from standard_tooling.lib.github_config import (
    desired_actions_permissions,
    desired_branch_protection_ruleset,
    desired_ci_gates_ruleset,
    desired_repo_settings,
    desired_security_settings,
    desired_tag_protection_ruleset,
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
