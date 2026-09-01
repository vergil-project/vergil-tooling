"""GitHub configuration derivation engine.

Computes the desired GitHub configuration for a repository from its
``vergil.toml`` identity.  The desired state can be compared
against the actual GitHub API state to produce audit diffs.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vergil_tooling.lib.config import CiConfig, ProjectConfig, VergilConfig

from vergil_tooling.lib import github


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
    allow_forking: bool | None
    allow_update_branch: bool
    has_downloads: bool
    merge_commit_title: str
    merge_commit_message: str
    squash_merge_commit_title: str
    squash_merge_commit_message: str
    web_commit_signoff_required: bool


@dataclass
class DesiredSecuritySettings:
    secret_scanning: str | None
    secret_scanning_push_protection: str | None
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
    bypass_actors: list[dict[str, object]] | None
    rules: list[dict[str, object]]


@dataclass
class DesiredPublishConfig:
    release: bool
    docs: bool


@dataclass
class DesiredState:
    repo_settings: DesiredRepoSettings
    security: DesiredSecuritySettings
    actions_permissions: DesiredActionsPermissions
    rulesets: list[DesiredRuleset]
    publish: DesiredPublishConfig


@dataclass
class FetchResult:
    state: DesiredState
    visibility: str
    owner_type: str


_BASE_ACTION_PATTERNS = [
    "actions/*",
    "docker/*",
    "github/*",
    "vergil-project/*",
]

_LANGUAGE_ACTION_PATTERNS: dict[str, list[str]] = {
    "python": ["astral-sh/*", "pypa/*"],
    "ruby": ["ruby/*"],
    "rust": ["actions-rust-lang/*", "swatinem/*"],
}


def desired_repo_settings(*, is_org: bool, visibility: str) -> DesiredRepoSettings:
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
        # GitHub only accepts the ``allow_forking`` field on org-owned *private*
        # repos; PATCHing it on a public repo returns 422 (vergil-tooling#1584),
        # and it is moot there since public repos are forkable per org policy.
        # Private org repos additionally depend on the org-level
        # ``members_can_fork_private_repositories`` flag, which the tool cannot
        # yet manage (vergil-tooling#1268). User-owned repos are left unmanaged
        # (vergil-tooling#666). Anything other than a private org repo is left
        # unmanaged (``None`` → omitted from the PATCH body).
        allow_forking=True if (is_org and visibility == "private") else None,
        allow_update_branch=True,
        has_downloads=False,
        merge_commit_title="MERGE_MESSAGE",
        merge_commit_message="PR_TITLE",
        squash_merge_commit_title="COMMIT_OR_PR_TITLE",
        squash_merge_commit_message="COMMIT_MESSAGES",
        web_commit_signoff_required=True,
    )


def ghas_available(config: VergilConfig, *, visibility: str) -> bool:
    """Resolve the GHAS posture for a repo.

    A declared ``[project].ghas`` in ``vergil.toml`` wins; otherwise
    GHAS is inferred as available exactly when the repo is not private.
    """
    if config.project.ghas is not None:
        return config.project.ghas
    return visibility != "private"


def desired_security_settings(*, ghas: bool) -> DesiredSecuritySettings:
    return DesiredSecuritySettings(
        secret_scanning="enabled" if ghas else None,  # noqa: S106
        secret_scanning_push_protection="enabled" if ghas else None,  # noqa: S106
        vulnerability_alerts=False,
        dependabot_security_updates="disabled",
    )


def desired_actions_permissions(
    primary_language: str | None, extra_patterns: list[str] | None = None
) -> DesiredActionsPermissions:
    lang_patterns = _LANGUAGE_ACTION_PATTERNS.get(primary_language, []) if primary_language else []
    patterns = sorted(set(_BASE_ACTION_PATTERNS) | set(lang_patterns) | set(extra_patterns or []))
    return DesiredActionsPermissions(
        default_workflow_permissions="read",
        can_approve_pull_request_reviews=False,
        allowed_actions="selected",
        patterns_allowed=patterns,
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
                    "require_extra_approval_for_unattributed_changes": False,
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
_GHAS_INTEGRATION_ID = 57789

_CODEQL_SUPPORTED_LANGUAGES = frozenset(
    {
        "python",
        "go",
        "java",
        "ruby",
        "rust",
        # cpp aligns this list with repo_init._CODEQL_LANGUAGES, which already
        # lists cpp — the two must agree (epic vergil-project/.github#207 T7).
        "cpp",
        # typescript is keyed by the *primary-language* name here, because this
        # set is tested against ``project.primary_language`` (see
        # ``desired_ci_gates_ruleset`` below). The CodeQL analysis identifier is
        # ``javascript-typescript``, but that ``typescript → javascript-typescript``
        # mapping belongs to the reusable CI Action (epic
        # vergil-project/.github#284 T7), not here — the emitted ``ci.yml``
        # ``language:`` stays ``typescript`` for container resolution. Like cpp,
        # this list must stay aligned with repo_init._CODEQL_LANGUAGES, which
        # already lists typescript (epic vergil-project/.github#284 T6).
        "typescript",
    }
)


def _make_check(context: str) -> dict[str, object]:
    return {
        "context": context,
        "integration_id": _GITHUB_ACTIONS_INTEGRATION_ID,
    }


def _make_ghas_check(context: str) -> dict[str, object]:
    return {
        "context": context,
        "integration_id": _GHAS_INTEGRATION_ID,
    }


def _lang_has_check(language: str | None, check: str) -> bool:
    """Consult the per-language command registry."""
    from vergil_tooling.lib.languages import CheckKind, language_commands

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
    *,
    ghas: bool,
    docs: bool = True,
) -> DesiredRuleset:
    """Derive the CI gates ruleset from project identity and CI config.

    ``docs`` mirrors ``[publish].docs``: the ``docs / docs`` gate is only
    emitted by a repo whose ``ci.yml`` invokes ``ci-docs.yml``, which repos do
    only when they publish docs. Requiring it on a non-docs repo (e.g. an org
    ``.github`` repo with ``[publish].docs = false``) pins a status that never
    reports, permanently blocking every merge with "the base branch policy
    prohibits the merge" (vergil-project/vergil-tooling#2647).
    """
    checks: list[dict[str, object]] = []
    lang = project.primary_language

    # Always present
    checks.append(_make_check("quality / common"))
    checks.append(_make_check("security / trivy"))
    checks.append(_make_check("security / semgrep"))
    # docs / docs is emitted only when the repo publishes docs; requiring it on
    # a non-docs repo blocks every merge (its CI never reports the context).
    if docs:
        checks.append(_make_check("docs / docs"))

    # GHAS check runs — created by GitHub Advanced Security (app 57789)
    # when workflows upload SARIF via codeql-action/upload-sarif.  These
    # gate on whether the PR introduces new alerts in changed lines.
    # Without GHAS the check runs can never materialize, so requiring
    # them would block merges forever; the trivy/semgrep jobs still gate
    # on findings via scanner exit codes.
    if ghas:
        checks.append(_make_ghas_check("Trivy"))
        checks.append(_make_ghas_check("Semgrep OSS"))

    # CodeQL for supported languages — requires GHAS-backed code scanning
    if ghas and lang in _CODEQL_SUPPORTED_LANGUAGES:
        checks.append(_make_check("security / codeql"))
        checks.append(_make_ghas_check("CodeQL"))

    # Matrixed CI kinds are gated on the *stable*, version-agnostic
    # ``<kind> / evidence`` aggregate each reusable workflow emits — one per
    # workflow — not per-version check names. Each ``evidence`` job ``needs`` the
    # whole version matrix, so a single required context covers every version: a
    # matrix change (including a *reduction*) merges through the same gate,
    # instead of pinning a per-version check that the reduced matrix can never
    # report ("expected, never reported" — a permanently blocked PR with no
    # ``--admin`` escape). Epic vergil-project/.github#338.
    #
    # A gate is emitted only when the language's command registry has the
    # underlying check, so the required set stays aligned with what CI can
    # produce: ``quality / evidence`` covers lint + typecheck (both live in the
    # ci-quality workflow), ``test / evidence`` covers unit, ``audit / evidence``
    # covers dependencies. This is version-independent, so the ruleset stops
    # churning when ``[ci].versions`` changes.
    if _lang_has_check(lang, "dependencies"):
        checks.append(_make_check("audit / evidence"))
    if _lang_has_check(lang, "lint") or _lang_has_check(lang, "typecheck"):
        checks.append(_make_check("quality / evidence"))
    if _lang_has_check(lang, "unit"):
        checks.append(_make_check("test / evidence"))

    # Integration tests per version (when enabled)
    if ci.integration_tests:
        for version in ci.versions:
            checks.append(_make_check(f"test / integration / {version}"))

    # Version check
    if project.release_model != "none":
        checks.append(_make_check("version / version-bump"))

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


# ---------------------------------------------------------------------------
# CI-gate producibility cross-check (issue #2720)
# ---------------------------------------------------------------------------
#
# The CI-gates ruleset (``desired_ci_gates_ruleset``) and the generated
# ``ci.yml`` workflow are two artifacts derived from the same identity along two
# independent paths. When they disagree — the ruleset *requires* a status check
# the workflow never *emits on ``pull_request``* — the branch is unmergeable by
# construction, with every check that does run green. Two flags have produced
# this: ``integration-tests`` (no producing job at all) and ``publish-docs``
# (the docs job ran on push, not on ``pull_request``). See issue #2720.
#
# ``unproducible_required_contexts`` reads the generated ci.yml, derives the set
# of contexts each reusable workflow it invokes actually emits on a PR, and
# returns any required context that set cannot produce. repo-init fails loudly on
# a non-empty result instead of shipping a repository that cannot merge its first
# PR with every check green.

_CI_JOB_RE = re.compile(r"^ {2}([a-z][a-z0-9_-]*):\s*$")
_CI_USES_RE = re.compile(r"^ {4}uses:\s+\S+/([a-z0-9-]+\.yml)@")


def _ci_caller_jobs(ci_workflow_yaml: str) -> list[tuple[str, str]]:
    """Parse a generated ci.yml into ``(caller_job, reusable_workflow_file)`` pairs.

    Only jobs that delegate to a reusable workflow via ``uses:`` are returned.
    The check-run context a reusable-workflow job produces is
    ``<caller_job> / <reusable job name>``, so the caller job id is the first
    segment of every context that job emits.
    """
    pairs: list[tuple[str, str]] = []
    current: str | None = None
    for line in ci_workflow_yaml.splitlines():
        job = _CI_JOB_RE.match(line)
        if job:
            current = job.group(1)
            continue
        uses = _CI_USES_RE.match(line)
        if uses and current is not None:
            pairs.append((current, uses.group(1)))
            current = None
    return pairs


def _reusable_pr_contexts(
    reusable_file: str, caller_job: str, *, ghas: bool
) -> tuple[set[str], set[str]]:
    """Contexts a reusable CI workflow emits on ``pull_request`` under ``caller_job``.

    Returns ``(exact, prefixes)``: a required context is producible when it
    equals a member of ``exact`` or starts with a member of ``prefixes`` (the
    version-parameterized families). The table mirrors what each
    ``vergil-actions`` reusable workflow actually reports on a PR — notably
    ``ci-test.yml`` emits ``unit`` but **not** ``integration``, which is what
    flags an ``integration-tests`` repo whose ruleset requires
    ``test / integration / <v>`` with no producing job (issue #2720).
    """
    j = caller_job
    # The matrixed workflows each end in a stable ``<job> / evidence`` aggregate
    # gate (what branch protection now requires — epic vergil-project/.github#338)
    # while still emitting their per-version legs as (non-required) PR check runs;
    # both are producible, so both appear here.
    if reusable_file == "ci-quality.yml":
        return ({f"{j} / common", f"{j} / evidence"}, {f"{j} / lint / ", f"{j} / typecheck / "})
    if reusable_file == "ci-audit.yml":
        return ({f"{j} / evidence"}, {f"{j} / dependencies / "})
    if reusable_file == "ci-test.yml":
        return ({f"{j} / evidence"}, {f"{j} / unit / "})
    if reusable_file == "ci-docs.yml":
        return ({f"{j} / docs"}, set())
    if reusable_file == "ci-security.yml":
        exact = {f"{j} / trivy", f"{j} / semgrep", f"{j} / codeql"}
        if ghas:
            # GHAS-app check runs carry no ``<job> /`` prefix (created by the
            # GitHub Advanced Security app, not the workflow) but do report on a
            # PR when the security job uploads SARIF.
            exact |= {"Trivy", "Semgrep OSS", "CodeQL"}
        return (exact, set())
    if reusable_file == "ci-version-bump.yml":
        return ({f"{j} / version-bump"}, set())
    return (set(), set())


def required_status_contexts(ruleset: DesiredRuleset) -> list[str]:
    """The required status-check context names declared by a CI-gates ruleset."""
    checks = _extract_status_checks(ruleset.rules) or []
    return [str(c.get("context", "")) for c in checks if c.get("context")]


def unproducible_required_contexts(
    ci_workflow_yaml: str, required_contexts: Iterable[str], *, ghas: bool
) -> list[str]:
    """Required status-check contexts the generated ci.yml cannot emit on a PR.

    A non-empty result means the CI-gates ruleset and the generated workflow
    disagree: the ruleset requires a context no ``pull_request`` job produces, so
    every merge is blocked with green checks (issue #2720). Returned sorted for a
    stable, deduplicated error message.
    """
    exact: set[str] = set()
    prefixes: set[str] = set()
    for caller_job, reusable_file in _ci_caller_jobs(ci_workflow_yaml):
        e, p = _reusable_pr_contexts(reusable_file, caller_job, ghas=ghas)
        exact |= e
        prefixes |= p

    missing: set[str] = set()
    for ctx in required_contexts:
        if ctx in exact:
            continue
        if any(ctx.startswith(pre) for pre in prefixes):
            continue
        missing.add(ctx)
    return sorted(missing)


# ---------------------------------------------------------------------------
# Evidence-gate derivation (issue #2289)
# ---------------------------------------------------------------------------
#
# The set of gates a repo MUST emit CI evidence for is derived from the *same*
# required-status-check computation that drives branch protection
# (``desired_ci_gates_ruleset``). This makes the enforced gates and the
# evidence-required gates provably identical — no hand-maintained list, no drift.


@dataclass(frozen=True)
class EvidenceGate:
    """An evidence-producing gate and the required checks classified under it."""

    name: str  # "security" | "test" | "audit" | "quality"
    checks: tuple[str, ...]  # required check names classified under this gate


# GHAS check-run names that carry no ``<gate> /`` prefix but still belong to the
# security gate (created by the GitHub Advanced Security app, not the workflow).
_EVIDENCE_GATE_LITERALS: dict[str, str] = {
    "Trivy": "security",
    "Semgrep OSS": "security",
    "CodeQL": "security",
}

# Ordered prefix table: a required check whose name starts with the prefix maps
# to the gate. ``version /`` is non-evidence-producing (``None``): the version
# bump check is a policy assertion, not a durable-output gate.
_EVIDENCE_GATE_PREFIXES: tuple[tuple[str, str | None], ...] = (
    ("security /", "security"),
    ("test /", "test"),
    ("audit /", "audit"),
    ("quality /", "quality"),
    ("version /", None),
)

# Canonical output order for grouped evidence gates (deterministic tuple order).
_EVIDENCE_GATE_ORDER: tuple[str, ...] = ("security", "test", "audit", "quality")


def classify_evidence_gate(check_name: str) -> str | None:
    """Map a required status-check name to its evidence gate.

    Returns the gate name (``security``/``test``/``audit``/``quality``) or
    ``None`` when the check is non-evidence-producing (e.g. ``version /
    version-bump``, or any name matching no known prefix/literal).
    """
    literal = _EVIDENCE_GATE_LITERALS.get(check_name)
    if literal is not None:
        return literal
    for prefix, gate in _EVIDENCE_GATE_PREFIXES:
        if check_name.startswith(prefix):
            return gate
    return None


def required_evidence_gates(
    project: ProjectConfig,
    ci: CiConfig,
    *,
    ghas: bool,
    docs: bool = True,
) -> tuple[EvidenceGate, ...]:
    """The evidence-producing gates this repo MUST emit.

    Derived from the same required-status-check computation that drives branch
    protection (:func:`desired_ci_gates_ruleset`), so the enforced gates and the
    evidence-required gates cannot drift apart. ``docs`` is threaded through
    identically for the same reason.
    """
    ruleset = desired_ci_gates_ruleset(project, ci, ghas=ghas, docs=docs)
    checks = _extract_status_checks(ruleset.rules) or []

    grouped: dict[str, list[str]] = {}
    for check in checks:
        name = str(check.get("context", ""))
        gate = classify_evidence_gate(name)
        if gate is None:
            continue
        grouped.setdefault(gate, []).append(name)

    return tuple(
        EvidenceGate(name=gate, checks=tuple(grouped[gate]))
        for gate in _EVIDENCE_GATE_ORDER
        if gate in grouped
    )


def compute_desired_state(
    config: VergilConfig, *, visibility: str, is_org: bool, app_mode: bool = False
) -> DesiredState:
    """Compute the full desired GitHub configuration from a repo's VergilConfig."""
    ghas = ghas_available(config, visibility=visibility)
    rulesets: list[DesiredRuleset] = []

    rulesets.append(desired_branch_protection_ruleset())
    rulesets.append(desired_tag_protection_ruleset())
    rulesets.append(
        desired_ci_gates_ruleset(config.project, config.ci, ghas=ghas, docs=config.publish.docs)
    )

    if app_mode:
        for rs in rulesets:
            rs.bypass_actors = None

    publish = DesiredPublishConfig(
        release=config.publish.release,
        docs=config.publish.docs,
    )

    return DesiredState(
        repo_settings=desired_repo_settings(is_org=is_org, visibility=visibility),
        security=desired_security_settings(ghas=ghas),
        actions_permissions=desired_actions_permissions(
            config.project.primary_language, config.actions.extra_allowed_patterns
        ),
        rulesets=rulesets,
        publish=publish,
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


# API-default fields GitHub injects into rule parameters that the desired state
# never sets. They must be stripped before comparison, else audit reports
# phantom drift that apply can never resolve (GitHub re-injects the default on
# every write). ``dismissal_restriction`` was added when GitHub began emitting
# it as a default on the ``pull_request`` rule (issue #2179).
_STRIP_RULE_PARAMS = frozenset({"do_not_enforce_on_create", "dismissal_restriction"})


def _normalize_rules(rules: Sequence[object]) -> list[dict[str, object]]:
    """Strip API-default fields from rule parameters for clean comparison."""
    normalized: list[dict[str, object]] = []
    for raw in rules:
        if not isinstance(raw, dict):
            continue
        rule = cast("dict[str, object]", raw)
        params = rule.get("parameters")
        if isinstance(params, dict):
            cleaned_params = {k: v for k, v in params.items() if k not in _STRIP_RULE_PARAMS}
            normalized.append({**rule, "parameters": cleaned_params})
        else:
            normalized.append(dict(rule))
    return normalized


def fetch_actual_state(repo: str) -> FetchResult:
    """Fetch the current GitHub configuration for a repo via gh api."""
    repo_data = github.read_json("api", f"repos/{repo}")

    visibility = (
        str(repo_data.get("visibility", "private")) if isinstance(repo_data, dict) else "private"
    )

    owner_raw = repo_data.get("owner") if isinstance(repo_data, dict) else None
    owner: dict[str, object] = (
        cast("dict[str, object]", owner_raw) if isinstance(owner_raw, dict) else {}
    )
    owner_type = str(owner.get("type", "User"))

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
        has_wiki=bool(repo_data.get("has_wiki", False)) if isinstance(repo_data, dict) else False,
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
            publish=DesiredPublishConfig(release=False, docs=False),
        ),
        visibility=visibility,
        owner_type=owner_type,
    )


# ---------------------------------------------------------------------------
# Diff formatting
# ---------------------------------------------------------------------------


def _extract_status_checks(
    rules: Sequence[object],
) -> list[dict[str, object]] | None:
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rd = cast("dict[str, object]", rule)
        if rd.get("type") != "required_status_checks":
            continue
        params = rd.get("parameters")
        if not isinstance(params, dict):
            continue
        pd = cast("dict[str, object]", params)
        checks = pd.get("required_status_checks")
        if isinstance(checks, list):
            return cast("list[dict[str, object]]", checks)
    return None


def _format_check(check: dict[str, object]) -> str:
    ctx = check.get("context", "?")
    iid = check.get("integration_id", "?")
    return f"{ctx} (integration_id: {iid})"


def format_rules_delta(expected: object, actual: object) -> str | None:
    if not isinstance(expected, list) or not isinstance(actual, list):
        return None
    expected_checks = _extract_status_checks(cast("Sequence[object]", expected))
    actual_checks = _extract_status_checks(cast("Sequence[object]", actual))
    if expected_checks is None and actual_checks is None:
        return None

    expected_set = {(c.get("context"), c.get("integration_id")) for c in (expected_checks or [])}
    actual_set = {(c.get("context"), c.get("integration_id")) for c in (actual_checks or [])}

    extra_keys = actual_set - expected_set
    missing_keys = expected_set - actual_set

    extra_labels = sorted(
        _format_check(c)
        for c in (actual_checks or [])
        if (c.get("context"), c.get("integration_id")) in extra_keys
    )
    missing_labels = sorted(
        _format_check(c)
        for c in (expected_checks or [])
        if (c.get("context"), c.get("integration_id")) in missing_keys
    )

    lines = [f"extra ({len(extra_labels)}):"]
    if extra_labels:
        for label in extra_labels:
            lines.append(f"  - {label}")
    else:
        lines.append("  none")
    lines.append(f"missing ({len(missing_labels)}):")
    if missing_labels:
        for label in missing_labels:
            lines.append(f"  - {label}")
    else:
        lines.append("  none")

    return "\n".join(lines)


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
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def is_compliant(self) -> bool:
        # Warnings are advisory (e.g. a deprecated-but-tolerated marketplace
        # ref mid-migration, #1974) and deliberately do not affect compliance.
        return len(self.items) == 0


def _canonical_key(obj: object) -> str:
    """Stable, order-independent sort key for a rule or check dict."""
    return json.dumps(obj, sort_keys=True, default=str)


def _canonicalize_rules(rules: Sequence[object]) -> list[object]:
    """Normalize a ruleset's ``rules`` for order-independent comparison.

    GitHub does not assign meaning to the order of rules within a ruleset,
    nor to the order of contexts within a ``required_status_checks`` rule.
    Comparing the raw lists with ``!=`` therefore reports spurious drift on
    a pure reordering while a genuine added/removed required check is the
    real concern (issue #1368).  Sorting both the outer rule list and the
    nested check list makes the comparison reflect set membership: an
    extra or missing required check always differs, a reordering never does.
    """
    canonical: list[object] = []
    for rule in rules:
        if not isinstance(rule, dict):
            canonical.append(rule)
            continue
        rule_copy = copy.deepcopy(cast("dict[str, object]", rule))
        params = rule_copy.get("parameters")
        if isinstance(params, dict):
            params_dict = cast("dict[str, object]", params)
            checks = params_dict.get("required_status_checks")
            if isinstance(checks, list):
                params_dict["required_status_checks"] = sorted(
                    cast("list[object]", checks), key=_canonical_key
                )
        canonical.append(rule_copy)
    return sorted(canonical, key=_canonical_key)


def _values_differ(prefix: str, desired: object, actual: object) -> bool:
    """Compare two leaf values, using set semantics for ruleset rules."""
    if prefix.endswith(".rules") and isinstance(desired, list) and isinstance(actual, list):
        return _canonicalize_rules(desired) != _canonicalize_rules(actual)
    return desired != actual


def _diff_dataclass(
    prefix: str,
    desired: object,
    actual: object,
    items: list[DiffItem],
    skipped: list[str],
) -> None:
    if not hasattr(desired, "__dataclass_fields__"):
        if desired is None:
            skipped.append(prefix)
            return
        if _values_differ(prefix, desired, actual):
            items.append(DiffItem(field=prefix, expected=desired, actual=actual))
        return
    for field_name in cast("dict[str, object]", desired.__dataclass_fields__):
        d_val = getattr(desired, field_name)
        a_val = getattr(actual, field_name)
        _diff_dataclass(f"{prefix}.{field_name}", d_val, a_val, items, skipped)


def _diff_rulesets(
    desired: list[DesiredRuleset],
    actual: list[DesiredRuleset],
    items: list[DiffItem],
    skipped: list[str],
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
                skipped,
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
    skipped: list[str] = []
    _diff_dataclass("repo_settings", desired.repo_settings, actual.repo_settings, items, skipped)
    _diff_dataclass("security", desired.security, actual.security, items, skipped)
    _diff_dataclass(
        "actions_permissions",
        desired.actions_permissions,
        actual.actions_permissions,
        items,
        skipped,
    )
    _diff_rulesets(desired.rulesets, actual.rulesets, items, skipped)
    return ConfigDiff(items=items, skipped=skipped)


# ---------------------------------------------------------------------------
# Apply desired state via GitHub API
# ---------------------------------------------------------------------------


_PRIVATE_FORKING_DENIED = "does not allow private repository forking"

_PRIVATE_FORKING_HELP = (
    "{repo}: cannot apply repository settings because the organization disallows "
    "forking private repositories. The tooling requires repos to be forkable. "
    "Enable the org-level setting 'members_can_fork_private_repositories' "
    "(Settings > Member privileges > Repository forking), then re-run. Org-level "
    "enforcement is tracked in vergil-tooling#1268."
)


def _is_private_forking_error(exc: github.GitHubAPIError) -> bool:
    """True when a settings PATCH was rejected for the org-level fork policy.

    GitHub returns 422 with this message when ``allow_forking`` is included in a
    private repo's settings PATCH while the org disallows private forking — even
    when the value is ``False``.
    """
    blob = f"{exc.stderr or ''}\n{exc.stdout or ''}"
    return _PRIVATE_FORKING_DENIED in blob


def _apply_repo_settings(repo: str, settings: DesiredRepoSettings) -> None:
    body: dict[str, object] = {
        "default_branch": settings.default_branch,
        "allow_auto_merge": settings.allow_auto_merge,
        "delete_branch_on_merge": settings.delete_branch_on_merge,
        "allow_merge_commit": settings.allow_merge_commit,
        "allow_squash_merge": settings.allow_squash_merge,
        "allow_rebase_merge": settings.allow_rebase_merge,
        "has_issues": settings.has_issues,
        "has_projects": settings.has_projects,
        "has_wiki": settings.has_wiki,
        "allow_update_branch": settings.allow_update_branch,
        "has_downloads": settings.has_downloads,
        "merge_commit_title": settings.merge_commit_title,
        "merge_commit_message": settings.merge_commit_message,
        "squash_merge_commit_title": settings.squash_merge_commit_title,
        "squash_merge_commit_message": settings.squash_merge_commit_message,
        "web_commit_signoff_required": settings.web_commit_signoff_required,
    }
    if settings.allow_forking is not None:
        body["allow_forking"] = settings.allow_forking
    try:
        github.write_json("PATCH", f"repos/{repo}", body)
    except github.GitHubAPIError as exc:
        if "allow_forking" in body and _is_private_forking_error(exc):
            raise RuntimeError(_PRIVATE_FORKING_HELP.format(repo=repo)) from exc
        raise


def _apply_security_settings(repo: str, security: DesiredSecuritySettings) -> None:
    sa: dict[str, object] = {}
    if security.secret_scanning is not None:
        sa["secret_scanning"] = {"status": security.secret_scanning}
    if security.secret_scanning_push_protection is not None:
        sa["secret_scanning_push_protection"] = {
            "status": security.secret_scanning_push_protection,
        }
    sa["dependabot_security_updates"] = {"status": security.dependabot_security_updates}
    github.write_json(
        "PATCH",
        f"repos/{repo}",
        {"security_and_analysis": sa},
    )
    if security.vulnerability_alerts:
        github.write_json("PUT", f"repos/{repo}/vulnerability-alerts", {})
    else:
        github.delete(f"repos/{repo}/vulnerability-alerts")


def _apply_actions_permissions(repo: str, perms: DesiredActionsPermissions) -> None:
    github.write_json(
        "PUT",
        f"repos/{repo}/actions/permissions",
        {"enabled": True, "allowed_actions": perms.allowed_actions},
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
        "bypass_actors": ruleset.bypass_actors if ruleset.bypass_actors is not None else [],
        "rules": ruleset.rules,
    }


def _apply_rulesets(repo: str, desired: list[DesiredRuleset]) -> None:
    raw_rulesets = github.read_json("api", f"repos/{repo}/rulesets")
    existing: dict[str, int] = {}
    if isinstance(raw_rulesets, list):
        for raw_rs in raw_rulesets:
            if isinstance(raw_rs, dict):
                rs = cast("dict[str, object]", raw_rs)
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


def _cleanup_classic_branch_protection(repo: str, rulesets: list[DesiredRuleset]) -> list[str]:
    """Remove legacy branch protection for branches covered by rulesets.

    Returns list of branches where legacy protection was removed.
    """
    branches: set[str] = set()
    for ruleset in rulesets:
        if ruleset.target != "branch":
            continue
        for ref in ruleset.ref_include:
            if ref.startswith("refs/heads/"):
                branches.add(ref.removeprefix("refs/heads/"))

    removed: list[str] = []
    for branch in sorted(branches):
        endpoint = f"repos/{repo}/branches/{branch}/protection"
        if github.delete_if_exists(endpoint):
            removed.append(branch)
    return removed


def apply_desired_state(repo: str, desired: DesiredState) -> list[str]:
    """Apply the desired configuration to a GitHub repo via the API.

    Returns list of branches where legacy branch protection was removed.
    """
    _apply_repo_settings(repo, desired.repo_settings)
    _apply_security_settings(repo, desired.security)
    _apply_actions_permissions(repo, desired.actions_permissions)
    _apply_rulesets(repo, desired.rulesets)
    return _cleanup_classic_branch_protection(repo, desired.rulesets)
