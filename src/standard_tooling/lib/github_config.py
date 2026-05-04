"""GitHub configuration derivation engine.

Computes the desired GitHub configuration for a repository from its
``standard-tooling.toml`` identity.  The desired state can be compared
against the actual GitHub API state to produce audit diffs.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from standard_tooling.lib.config import CiConfig, ProjectConfig, StConfig

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


# ---------------------------------------------------------------------------
# CI gates ruleset derivation
# ---------------------------------------------------------------------------

_GITHUB_ACTIONS_INTEGRATION_ID = 15368

_CODEQL_SUPPORTED_LANGUAGES = frozenset(
    {
        "python",
        "go",
        "java",
        "ruby",
        "rust",
    }
)


def _make_check(context: str) -> dict[str, object]:
    return {
        "context": context,
        "integration_id": _GITHUB_ACTIONS_INTEGRATION_ID,
    }


def _lang_has_check(language: str, check: str) -> bool:
    """Consult the per-language command registry."""
    from standard_tooling.lib.validate_commands import CheckKind, language_commands

    kind_map = {
        "lint": CheckKind.LINT,
        "typecheck": CheckKind.TYPECHECK,
        "unit": CheckKind.TEST,
        "dependencies": CheckKind.AUDIT,
    }
    kind = kind_map.get(check)
    if kind is None:
        return False
    return len(language_commands(language, kind)) > 0


def desired_ci_gates_ruleset(
    project: ProjectConfig,
    ci: CiConfig,
) -> DesiredRuleset:
    """Derive the CI gates ruleset from project identity and CI config."""
    checks: list[dict[str, object]] = []
    lang = project.primary_language

    # Always present
    checks.append(_make_check("quality / common"))
    checks.append(_make_check("security / trivy"))
    checks.append(_make_check("security / semgrep"))
    checks.append(_make_check("security / standards"))

    # CodeQL for supported languages
    if lang in _CODEQL_SUPPORTED_LANGUAGES:
        checks.append(_make_check("security / codeql"))

    # Versioned checks — only emitted when the language has
    # a command registry entry for the check
    for version in ci.versions:
        if _lang_has_check(lang, "lint"):
            checks.append(_make_check(f"quality / lint / {version}"))
        if _lang_has_check(lang, "typecheck"):
            checks.append(_make_check(f"quality / typecheck / {version}"))
        if _lang_has_check(lang, "unit"):
            checks.append(_make_check(f"test / unit / {version}"))
        if _lang_has_check(lang, "dependencies"):
            checks.append(_make_check(f"audit / dependencies / {version}"))

    # Integration tests per version (when enabled)
    if ci.integration_tests:
        for version in ci.versions:
            checks.append(_make_check(f"test / integration / {version}"))

    # Release check
    if project.release_model != "none":
        checks.append(_make_check("release / version-bump"))

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


def compute_desired_state(config: StConfig) -> DesiredState:
    """Compute the full desired GitHub configuration from a repo's StConfig."""
    rulesets: list[DesiredRuleset] = []

    if not config.github.skip_rulesets:
        rulesets.append(desired_branch_protection_ruleset())
        rulesets.append(desired_tag_protection_ruleset())

        if config.ci is not None:
            rulesets.append(desired_ci_gates_ruleset(config.project, config.ci))

    return DesiredState(
        repo_settings=desired_repo_settings(),
        security=desired_security_settings(),
        actions_permissions=desired_actions_permissions(),
        rulesets=rulesets,
    )


# ---------------------------------------------------------------------------
# Fetch actual state from GitHub API
# ---------------------------------------------------------------------------


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

    sa = repo_data.get("security_and_analysis") if isinstance(repo_data, dict) else None
    if not isinstance(sa, dict):
        sa = {}

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

    ss = sa.get("secret_scanning")
    ss_status = ss.get("status", "disabled") if isinstance(ss, dict) else "disabled"
    sspp = sa.get("secret_scanning_push_protection")
    sspp_status = sspp.get("status", "disabled") if isinstance(sspp, dict) else "disabled"
    dsu = sa.get("dependabot_security_updates")
    dsu_status = dsu.get("status", "disabled") if isinstance(dsu, dict) else "disabled"

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
        for rs_summary in raw_rulesets:
            if not isinstance(rs_summary, dict):
                continue
            rs_id = rs_summary.get("id")
            if rs_id is None:
                continue
            rs_detail = github.read_json("api", f"repos/{repo}/rulesets/{rs_id}")
            if not isinstance(rs_detail, dict):
                continue
            conditions = rs_detail.get("conditions")
            conditions = conditions if isinstance(conditions, dict) else {}
            ref_name = conditions.get("ref_name")
            ref_name = ref_name if isinstance(ref_name, dict) else {}
            include = ref_name.get("include")
            include = include if isinstance(include, list) else []

            bypass_raw = rs_detail.get("bypass_actors")
            bypass = bypass_raw if isinstance(bypass_raw, list) else []
            rules_raw = rs_detail.get("rules")
            rules = rules_raw if isinstance(rules_raw, list) else []

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

    return DesiredState(
        repo_settings=repo_settings,
        security=security,
        actions_permissions=actions_permissions,
        rulesets=rulesets,
    )


# ---------------------------------------------------------------------------
# Diff computation
# ---------------------------------------------------------------------------


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
            items.append(
                DiffItem(
                    field=f"rulesets.{name}",
                    expected="present",
                    actual="missing",
                )
            )
        else:
            _diff_dataclass(
                f"rulesets.{name}",
                desired_by_name[name],
                actual_by_name[name],
                items,
            )

    for name in actual_by_name:
        if name not in desired_by_name:
            items.append(
                DiffItem(
                    field=f"rulesets.{name}",
                    expected="absent",
                    actual="present",
                )
            )


def compute_diff(*, desired: DesiredState, actual: DesiredState) -> ConfigDiff:
    """Compare desired vs actual state and return structured diff."""
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


# ---------------------------------------------------------------------------
# Apply desired state via GitHub API
# ---------------------------------------------------------------------------


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
        },
    )


def _apply_security_settings(repo: str, security: DesiredSecuritySettings) -> None:
    github.write_json(
        "PATCH",
        f"repos/{repo}",
        {
            "security_and_analysis": {
                "secret_scanning": {"status": security.secret_scanning},
                "secret_scanning_push_protection": {
                    "status": security.secret_scanning_push_protection,
                },
                "dependabot_security_updates": {
                    "status": security.dependabot_security_updates,
                },
            },
        },
    )
    if security.vulnerability_alerts:
        github.write_json("PUT", f"repos/{repo}/vulnerability-alerts", {})
    else:
        github.delete(f"repos/{repo}/vulnerability-alerts")


def _apply_actions_permissions(repo: str, perms: DesiredActionsPermissions) -> None:
    github.write_json(
        "PUT",
        f"repos/{repo}/actions/permissions",
        {"allowed_actions": perms.allowed_actions},
    )
    github.write_json(
        "PUT",
        f"repos/{repo}/actions/permissions/workflow",
        {
            "default_workflow_permissions": perms.default_workflow_permissions,
            "can_approve_pull_request_reviews": perms.can_approve_pull_request_reviews,
        },
    )
    if perms.allowed_actions == "selected":
        github.write_json(
            "PUT",
            f"repos/{repo}/actions/permissions/selected-actions",
            {"patterns_allowed": perms.patterns_allowed},
        )


def _ruleset_body(ruleset: DesiredRuleset) -> dict[str, object]:
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


def _apply_rulesets(repo: str, desired: list[DesiredRuleset]) -> None:
    raw_rulesets = github.read_json("api", f"repos/{repo}/rulesets")
    existing: dict[str, int] = {}
    if isinstance(raw_rulesets, list):
        for rs in raw_rulesets:
            if isinstance(rs, dict):
                name = rs.get("name")
                rs_id = rs.get("id")
                if isinstance(name, str) and isinstance(rs_id, int):
                    existing[name] = rs_id

    desired_names = {r.name for r in desired}

    for ruleset in desired:
        body = _ruleset_body(ruleset)
        if ruleset.name in existing:
            github.write_json(
                "PUT",
                f"repos/{repo}/rulesets/{existing[ruleset.name]}",
                body,
            )
        else:
            github.write_json("POST", f"repos/{repo}/rulesets", body)

    for name, rs_id in existing.items():
        if name not in desired_names:
            github.delete(f"repos/{repo}/rulesets/{rs_id}")


def apply_desired_state(repo: str, desired: DesiredState) -> None:
    """Apply the desired configuration to a GitHub repo via the API."""
    _apply_repo_settings(repo, desired.repo_settings)
    _apply_security_settings(repo, desired.security)
    _apply_actions_permissions(repo, desired.actions_permissions)
    _apply_rulesets(repo, desired.rulesets)
