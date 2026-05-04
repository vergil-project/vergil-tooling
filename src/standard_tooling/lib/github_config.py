"""GitHub configuration derivation engine.

Computes the desired GitHub configuration for a repository from its
``standard-tooling.toml`` identity.  The desired state can be compared
against the actual GitHub API state to produce audit diffs.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from standard_tooling.lib import github


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
    bypass_actors: list[dict[str, object]]
    rules: list[dict[str, object]]


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


def _fetch_vulnerability_alerts(repo: str) -> bool:
    """Check if vulnerability alerts are enabled (204 = enabled, 404 = disabled)."""
    result = subprocess.run(  # noqa: S603
        ("gh", "api", f"repos/{repo}/vulnerability-alerts", "-i"),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return "204" in result.stdout.split("\n")[0]


def fetch_actual_state(repo: str) -> DesiredState:
    """Fetch the current GitHub configuration for a repo via gh api."""
    repo_data = github.read_json("api", f"repos/{repo}")

    sa_raw = repo_data.get("security_and_analysis", {}) if isinstance(repo_data, dict) else {}
    sa: dict[str, object] = sa_raw if isinstance(sa_raw, dict) else {}

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
        has_wiki=bool(repo_data.get("has_wiki", False)) if isinstance(repo_data, dict) else False,
    )

    def _sa_status(key: str) -> str:
        val = sa.get(key, {})
        if isinstance(val, dict):
            return str(val.get("status", "disabled"))
        return "disabled"

    security = DesiredSecuritySettings(
        secret_scanning=_sa_status("secret_scanning"),
        secret_scanning_push_protection=_sa_status("secret_scanning_push_protection"),
        vulnerability_alerts=_fetch_vulnerability_alerts(repo),
        dependabot_security_updates=_sa_status("dependabot_security_updates"),
    )

    actions_perm = github.read_json("api", f"repos/{repo}/actions/permissions")
    actions_workflow = github.read_json("api", f"repos/{repo}/actions/permissions/workflow")

    patterns: list[str] = []
    allowed_actions_val = (
        actions_perm.get("allowed_actions", "") if isinstance(actions_perm, dict) else ""
    )
    if allowed_actions_val == "selected":
        selected = github.read_json("api", f"repos/{repo}/actions/permissions/selected-actions")
        if isinstance(selected, dict):
            raw_patterns = selected.get("patterns_allowed", [])
            patterns = list(raw_patterns) if isinstance(raw_patterns, list) else []

    actions_permissions = DesiredActionsPermissions(
        default_workflow_permissions=str(
            actions_workflow.get("default_workflow_permissions", "")
            if isinstance(actions_workflow, dict)
            else ""
        ),
        can_approve_pull_request_reviews=bool(
            actions_workflow.get("can_approve_pull_request_reviews", False)
            if isinstance(actions_workflow, dict)
            else False
        ),
        allowed_actions=str(allowed_actions_val),
        patterns_allowed=sorted(str(p) for p in patterns),
    )

    raw_rulesets = github.read_json("api", f"repos/{repo}/rulesets")
    rulesets: list[DesiredRuleset] = []
    if isinstance(raw_rulesets, list):
        for rs_summary in raw_rulesets:
            if not isinstance(rs_summary, dict):
                continue
            rs_detail = github.read_json("api", f"repos/{repo}/rulesets/{rs_summary['id']}")
            if not isinstance(rs_detail, dict):
                continue
            conditions = rs_detail.get("conditions", {})
            ref_name = conditions.get("ref_name", {}) if isinstance(conditions, dict) else {}
            ref_include_raw = ref_name.get("include", []) if isinstance(ref_name, dict) else []
            bypass_raw = rs_detail.get("bypass_actors", [])
            rules_raw = rs_detail.get("rules", [])
            rulesets.append(
                DesiredRuleset(
                    name=str(rs_detail.get("name", "")),
                    target=str(rs_detail.get("target", "")),
                    enforcement=str(rs_detail.get("enforcement", "")),
                    ref_include=list(ref_include_raw) if isinstance(ref_include_raw, list) else [],
                    bypass_actors=list(bypass_raw) if isinstance(bypass_raw, list) else [],
                    rules=list(rules_raw) if isinstance(rules_raw, list) else [],
                )
            )

    return DesiredState(
        repo_settings=repo_settings,
        security=security,
        actions_permissions=actions_permissions,
        rulesets=rulesets,
    )
