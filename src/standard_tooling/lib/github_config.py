"""GitHub configuration derivation engine.

Computes the desired GitHub configuration for a repository from its
``standard-tooling.toml`` identity.  The desired state can be compared
against the actual GitHub API state to produce audit diffs.
"""

from __future__ import annotations

from dataclasses import dataclass


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


def desired_security_settings() -> DesiredSecuritySettings:
    return DesiredSecuritySettings(
        secret_scanning="enabled",  # noqa: S106
        secret_scanning_push_protection="enabled",  # noqa: S106
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
