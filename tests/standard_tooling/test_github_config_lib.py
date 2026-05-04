"""Tests for standard_tooling.lib.github_config."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from standard_tooling.lib.github_config import (
    _fetch_vulnerability_alerts,
    desired_repo_settings,
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


# ---------------------------------------------------------------------------
# fetch_actual_state tests
# ---------------------------------------------------------------------------


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
    repo_json: dict = {
        "default_branch": "develop",
        "security_and_analysis": {},
    }
    ruleset_summary: list = [{"id": 42}]
    ruleset_detail: dict = {
        "name": "Branch protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
        "bypass_actors": [],
        "rules": [{"type": "deletion"}],
    }

    def mock_read_json(*args: str) -> dict | list:
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
    assert actual.rulesets[0].rules == [{"type": "deletion"}]


def test_fetch_actual_state_no_selected_actions_skips_patterns() -> None:
    """When allowed_actions != 'selected', don't fetch patterns endpoint."""
    repo_json: dict = {"default_branch": "develop", "security_and_analysis": {}}

    call_log: list[str] = []

    def mock_read_json(*args: str) -> dict | list:
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


def test_fetch_actual_state_sa_non_dict_value() -> None:
    """When security_and_analysis contains a non-dict value, _sa_status returns 'disabled'."""
    repo_json: dict = {
        "default_branch": "develop",
        "security_and_analysis": {
            "secret_scanning": "not-a-dict",
            "secret_scanning_push_protection": None,
            "dependabot_security_updates": 42,
        },
    }

    def mock_read_json(*args: str) -> dict | list:
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


def test_fetch_actual_state_selected_actions_returns_list() -> None:
    """When selected-actions endpoint returns a list instead of dict, patterns stay empty."""
    repo_json: dict = {"default_branch": "develop", "security_and_analysis": {}}

    def mock_read_json(*args: str) -> dict | list:
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
            return []  # list, not dict
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


def test_fetch_actual_state_rulesets_not_a_list() -> None:
    """When rulesets endpoint returns a dict (unexpected), rulesets stays empty."""
    repo_json: dict = {"default_branch": "develop", "security_and_analysis": {}}

    def mock_read_json(*args: str) -> dict | list:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return {"error": "unexpected"}  # dict, not list
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


def test_fetch_actual_state_ruleset_summary_not_dict() -> None:
    """When a ruleset summary entry is not a dict, it's skipped."""
    repo_json: dict = {"default_branch": "develop", "security_and_analysis": {}}

    def mock_read_json(*args: str) -> dict | list:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return ["not-a-dict", 123]  # non-dict entries
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


def test_fetch_actual_state_ruleset_detail_not_dict() -> None:
    """When a ruleset detail response is not a dict, it's skipped."""
    repo_json: dict = {"default_branch": "develop", "security_and_analysis": {}}

    def mock_read_json(*args: str) -> dict | list:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return [{"id": 99}]
        if endpoint == "repos/o/r/rulesets/99":
            return []  # list, not dict
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
