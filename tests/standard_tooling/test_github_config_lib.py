"""Tests for standard_tooling.lib.github_config."""

from __future__ import annotations

from standard_tooling.lib.github_config import (
    desired_actions_permissions,
    desired_branch_protection_ruleset,
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
