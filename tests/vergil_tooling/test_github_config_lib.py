"""Tests for vergil_tooling.lib.github_config."""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import json
import re
import subprocess
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github_config
from vergil_tooling.lib.config import (
    DEFAULT_VALIDATION_COMMAND,
    CiConfig,
    ContainerConfig,
    MarkdownlintConfig,
    PackageConfig,
    PackagePythonConfig,
    ProjectConfig,
    PublishConfig,
    ValidationConfig,
    VergilConfig,
)
from vergil_tooling.lib.github import GitHubAPIError
from vergil_tooling.lib.github_config import (
    _PRODUCIBLE_GATE_CATALOG,
    CI_GATES_DEVELOP,
    CI_GATES_MAIN,
    CI_GATES_RULESET_NAMES,
    GITHUB_ACTIONS_INTEGRATION_ID,
    LEGACY_CI_GATES_RULESET_NAME,
    MERGE_SETTINGS_READ,
    SETTING_RETURNED,
    BypassUnreadable,
    ClassicProtectionReport,
    ConfigDiff,
    DesiredRepoSettings,
    DesiredRuleset,
    DesiredState,
    DiffItem,
    EvidenceGate,
    FetchResult,
    NotChecked,
    UndeclaredParam,
    _apply_actions_permissions,
    _apply_environments,
    _apply_repo_settings,
    _apply_rulesets,
    _apply_security_settings,
    _branch_from_ref,
    _canonicalize_rules,
    _classic_required_contexts,
    _cleanup_classic_branch_protection,
    _fetch_vulnerability_alerts,
    _is_stale_ci_context,
    _lang_has_check,
    _normalize_rules,
    _preserved_classic_settings,
    _read_classic_protection,
    _ruleset_body,
    _without_headers,
    apply_desired_state,
    assert_ci_gates_producible,
    classify_evidence_gate,
    compute_desired_state,
    compute_diff,
    desired_actions_permissions,
    desired_audit_marker_ruleset,
    desired_automation_branch_ruleset,
    desired_automation_environment,
    desired_branch_protection_ruleset,
    desired_ci_gates_rulesets,
    desired_main_update_ruleset,
    desired_repo_settings,
    desired_rolling_tag_branch_ruleset,
    desired_security_settings,
    desired_tag_protection_ruleset,
    fetch_actual_state,
    format_rules_delta,
    required_evidence_gates,
    required_status_contexts,
    resolved_integration_versions,
    unproducible_ci_yaml_contexts,
    unproducible_required_contexts,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator


def _paginated_via_read_json(endpoint: str, key: str | None = None) -> list[object]:
    """Route ``read_json_paginated`` through the test's ``read_json`` mock.

    The rulesets listing is paginated (issue #3162), but most tests here model
    the whole GitHub API as one ``read_json`` endpoint→payload mock. This shim
    resolves the listing through that mock (looked up at call time, so each
    test's patch applies) and keeps the helper's contract: a non-list page
    raises ``ValueError``, as does an envelope page with no *key* list (the
    environment's branch policies). Pagination itself is exercised end to end
    by ``test_*_rulesets_reads_every_page``.
    """
    page: object = github_config.github.read_json("api", endpoint)
    if key is not None:
        if not isinstance(page, dict) or key not in page:
            msg = f"paginated response from {endpoint!r} has no {key!r} list"
            raise ValueError(msg)
        page = page[key]
    if not isinstance(page, list):
        msg = f"paginated response from {endpoint!r} is not a list: {type(page).__name__}"
        raise ValueError(msg)
    return page


_REAL_READ_JSON_PAGINATED = github_config.github.read_json_paginated


@pytest.fixture(autouse=True)
def _route_paginated_rulesets() -> Iterator[None]:
    with patch(
        "vergil_tooling.lib.github_config.github.read_json_paginated",
        side_effect=_paginated_via_read_json,
    ):
        yield


@contextlib.contextmanager
def _real_pagination(fake_read_output: Callable[..., str]) -> Generator[None]:
    """Undo the autouse shim: run the real helper over a faked ``gh api`` stream."""
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json_paginated",
            side_effect=_REAL_READ_JSON_PAGINATED,
        ),
        patch("vergil_tooling.lib.github.read_output", side_effect=fake_read_output),
    ):
        yield


def test_desired_repo_settings_are_fixed() -> None:
    s = desired_repo_settings()
    assert s.default_branch == "develop"
    assert s.allow_auto_merge is True
    assert s.delete_branch_on_merge is True
    assert s.allow_merge_commit is True
    assert s.allow_squash_merge is True
    assert s.allow_rebase_merge is True
    assert s.has_issues is True
    assert s.has_projects is True
    assert s.has_wiki is True


def test_desired_settings_have_no_visibility() -> None:
    """Every repo is public: nothing in the desired state depends on visibility.

    The private-only ``allow_forking`` setting is gone (GitHub accepts it only on
    an org-owned private repo), and the desired state takes no visibility.
    """
    assert list(inspect.signature(desired_repo_settings).parameters) == []
    assert "allow_forking" not in {f.name for f in dataclasses.fields(DesiredRepoSettings)}
    assert "visibility" not in inspect.signature(compute_desired_state).parameters
    assert "visibility" not in {f.name for f in dataclasses.fields(FetchResult)}


def test_desired_repo_settings_new_hardcoded_values() -> None:
    s = desired_repo_settings()
    assert s.allow_update_branch is True
    assert s.has_downloads is False
    assert s.merge_commit_title == "MERGE_MESSAGE"
    assert s.merge_commit_message == "PR_TITLE"
    assert s.squash_merge_commit_title == "COMMIT_OR_PR_TITLE"
    assert s.squash_merge_commit_message == "COMMIT_MESSAGES"
    assert s.web_commit_signoff_required is True


def test_desired_security_settings_enable_secret_scanning() -> None:
    s = desired_security_settings()
    assert s.secret_scanning == "enabled"  # noqa: S105
    assert s.secret_scanning_push_protection == "enabled"  # noqa: S105
    assert s.vulnerability_alerts is True
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
        "actions/*",
        "docker/*",
        "github/*",
        "swatinem/*",
        "vergil-project/*",
    ]


def test_desired_actions_permissions_python() -> None:
    # Python adds no language patterns: its CI runs through Vergil's
    # reusable workflows (vergil-tooling#4577).
    a = desired_actions_permissions("python")
    assert a.patterns_allowed == [
        "actions/*",
        "docker/*",
        "github/*",
        "vergil-project/*",
    ]


def test_desired_actions_permissions_extra_patterns() -> None:
    a = desired_actions_permissions("go", ["dataaxiom/ghcr-cleanup-action@*"])
    assert a.patterns_allowed == [
        "actions/*",
        "dataaxiom/ghcr-cleanup-action@*",
        "docker/*",
        "github/*",
        "vergil-project/*",
    ]


def test_desired_actions_permissions_extra_patterns_unioned_not_duplicated() -> None:
    # An extra pattern that overlaps a language default is unioned, not duplicated.
    a = desired_actions_permissions("rust", ["swatinem/*", "dataaxiom/ghcr-cleanup-action@*"])
    assert a.patterns_allowed == [
        "actions/*",
        "dataaxiom/ghcr-cleanup-action@*",
        "docker/*",
        "github/*",
        "swatinem/*",
        "vergil-project/*",
    ]


def test_compute_desired_state_threads_extra_action_patterns() -> None:
    cfg = _vergil_config()
    cfg.actions.extra_allowed_patterns = ["dataaxiom/ghcr-cleanup-action@*"]
    state = _state(cfg)
    assert "dataaxiom/ghcr-cleanup-action@*" in state.actions_permissions.patterns_allowed


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
    # GitHub began emitting this default as True server-side; we explicitly
    # declare it False so the audit owns the setting and keeps it off (#2860).
    assert params["require_extra_approval_for_unattributed_changes"] is False


def test_tag_protection_ruleset() -> None:
    r = desired_tag_protection_ruleset(app_id=123)
    assert r.name == "Tag protection"
    assert r.target == "tag"
    assert r.enforcement == "active"
    assert r.ref_include == ["refs/tags/v*.*.*", "refs/tags/v*.*"]


def test_tag_protection_blocks_creation() -> None:
    rules = {r["type"] for r in desired_tag_protection_ruleset(app_id=123).rules}
    assert rules == {"creation", "update", "deletion", "non_fast_forward"}


def test_tag_protection_bypass_is_admin_and_release_identity_only() -> None:
    actors = desired_tag_protection_ruleset(app_id=123).bypass_actors
    assert actors is not None
    assert {(a["actor_type"], a["actor_id"]) for a in actors} == {
        ("RepositoryRole", 5),
        ("Integration", 123),
    }
    assert all(a["bypass_mode"] == "always" for a in actors)


def test_tag_protection_bypass_never_includes_github_actions() -> None:
    # The GitHub Actions integration as a bypass actor would let every
    # workflow's GITHUB_TOKEN create a release tag.
    actors = desired_tag_protection_ruleset(app_id=123).bypass_actors
    assert actors is not None
    assert GITHUB_ACTIONS_INTEGRATION_ID not in {a["actor_id"] for a in actors}


def test_tag_protection_without_app_id_is_skipped() -> None:
    assert desired_tag_protection_ruleset(None).bypass_actors is None


# The literal the rolling-tag branch ruleset matches: a ``v`` then a digit, so a
# rolling-tag-shaped name such as ``v2.1`` is covered while ``vm-...`` is not.
BRANCH_PATTERN = "refs/heads/v[0-9]*"


def test_branch_from_ref_skips_glob_includes() -> None:
    assert _branch_from_ref("refs/heads/v*") is None
    assert _branch_from_ref("refs/heads/v?") is None
    assert _branch_from_ref(BRANCH_PATTERN) is None
    assert _branch_from_ref("refs/heads/main") == "main"
    assert _branch_from_ref("refs/tags/v1.2.3") is None


def test_rolling_tag_branch_ruleset() -> None:
    r = desired_rolling_tag_branch_ruleset()
    assert r.name == "Rolling tag branch names"
    assert r.target == "branch"
    assert r.enforcement == "active"
    assert r.ref_include == [BRANCH_PATTERN]
    assert r.bypass_actors == []
    assert [rule["type"] for rule in r.rules] == ["creation", "update", "non_fast_forward"]


# ---------------------------------------------------------------------------
# CI gates ruleset tests
# ---------------------------------------------------------------------------


def _project(
    *,
    language: str | None = "python",
    release_model: str = "tagged-release",
    branching_model: str = "library-release",
) -> ProjectConfig:
    return ProjectConfig(
        repository_type="library",
        versioning_scheme="semver",
        branching_model=branching_model,
        release_model=release_model,
        primary_language=language,
    )


def _ci(
    *,
    versions: list[str] | None = None,
    integration_tests: bool = False,
    images: bool = False,
) -> CiConfig:
    return CiConfig(
        versions=versions or ["3.14"],
        integration_tests=integration_tests,
        images=images,
    )


def _check_names(ruleset: DesiredRuleset) -> list[str]:
    """Extract check context names from a CI gates ruleset."""
    return [str(c["context"]) for c in _checks(ruleset)]


def _checks(ruleset: DesiredRuleset) -> list[dict[str, object]]:
    """Extract all check dicts from a CI gates ruleset."""
    status_rule = next(rule for rule in ruleset.rules if rule["type"] == "required_status_checks")
    params = cast("dict[str, list[dict[str, object]]]", status_rule["parameters"])
    return params["required_status_checks"]


def _ci_gates_develop(project: ProjectConfig, ci: CiConfig, **kwargs: Any) -> DesiredRuleset:
    """The develop CI-gates ruleset: the one every repo has, so the checks tests use it."""
    develop = desired_ci_gates_rulesets(project, ci, **kwargs)[0]
    assert develop.name == CI_GATES_DEVELOP
    return develop


def _strict(ruleset: DesiredRuleset) -> object:
    status_rule = next(rule for rule in ruleset.rules if rule["type"] == "required_status_checks")
    params = cast("dict[str, object]", status_rule["parameters"])
    return params["strict_required_status_checks_policy"]


def test_ci_gates_split_develop_not_strict_main_strict() -> None:
    """One ruleset per branch (epic .github#1218, spec §6.1): develop drops the
    up-to-date rule and main keeps it, with the same required checks."""
    develop, main = desired_ci_gates_rulesets(_project(), _ci())
    assert (develop.name, main.name) == (CI_GATES_DEVELOP, CI_GATES_MAIN)
    assert CI_GATES_RULESET_NAMES == (CI_GATES_DEVELOP, CI_GATES_MAIN)
    assert develop.ref_include == ["refs/heads/develop"]
    assert main.ref_include == ["refs/heads/main"]
    assert _strict(develop) is False
    assert _strict(main) is True
    for r in (develop, main):
        assert r.target == "branch"
        assert r.enforcement == "active"
        assert r.bypass_actors == []
    assert _checks(develop) == _checks(main)
    # Each ruleset owns its check dicts: editing one never changes the other.
    _checks(develop)[0]["context"] = "edited"
    assert _checks(main)[0]["context"] != "edited"


def test_docs_single_branch_gets_develop_only() -> None:
    """A docs-single-branch repo has no main, so no main CI-gates ruleset."""
    rulesets = desired_ci_gates_rulesets(_project(branching_model="docs-single-branch"), _ci())
    assert [r.name for r in rulesets] == [CI_GATES_DEVELOP]
    assert _strict(rulesets[0]) is False


@pytest.mark.parametrize("branching_model", ["library-release", "application-promotion"])
def test_ci_gates_main_ruleset_for_models_with_main(branching_model: str) -> None:
    rulesets = desired_ci_gates_rulesets(_project(branching_model=branching_model), _ci())
    assert [r.name for r in rulesets] == [CI_GATES_DEVELOP, CI_GATES_MAIN]


def test_ci_gates_always_includes_quality_evidence_and_security() -> None:
    r = _ci_gates_develop(_project(), _ci())
    check_names = _check_names(r)
    assert "quality / evidence" in check_names
    assert "quality / common" not in check_names
    assert "security / trivy" in check_names
    assert "security / semgrep" in check_names
    assert "security / standards" not in check_names
    assert "Trivy" in check_names
    assert "Semgrep OSS" in check_names


def test_ci_gates_requires_docs_check_when_publishing_docs() -> None:
    """A docs-publishing repo (`[publish].docs = true`) emits `docs / docs` from
    the reusable CI, so it must be a required PR gate."""
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True)
    assert "docs / docs" in _check_names(ruleset)


def test_ci_gates_omits_docs_check_when_not_publishing_docs() -> None:
    """A repo with `[publish].docs = false` (e.g. an org `.github` repo) has no
    docs job in its CI, so `docs / docs` is never reported. Requiring it pins a
    status that never resolves and blocks every merge with "the base branch
    policy prohibits the merge" (vergil-project/vergil-tooling#2647)."""
    ruleset = _ci_gates_develop(_project(), _ci(), docs=False)
    assert "docs / docs" not in _check_names(ruleset)


def test_ci_gates_requires_package_evidence_when_package_section_present() -> None:
    """A repo whose `vergil.toml` has `[package]` calls `ci-package.yml`, which
    emits the stable `package / evidence` gate, so it is a required PR check
    (epic vergil-project/.github#356, spec section 8.1)."""
    ruleset = _ci_gates_develop(_project(), _ci(), package=True)
    assert "package / evidence" in _check_names(ruleset)


def test_ci_gates_requires_images_evidence_when_ci_images_on() -> None:
    """A repo with ``[ci].images = true`` calls the repo-local ``ci-images.yml``
    as its ``images`` job, whose ``evidence`` aggregate is then a required PR
    check (vergil-project/vergil-tooling#4132)."""
    ruleset = _ci_gates_develop(_project(), _ci(images=True))
    assert "images / evidence" in _check_names(ruleset)


def test_ci_gates_omits_images_evidence_by_default() -> None:
    """A repo that never runs ``ci-images.yml`` must never require its gate: the
    status would never report and every merge would block."""
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True, package=True)
    assert "images / evidence" not in _check_names(ruleset)


def test_ci_gates_omits_package_evidence_without_package_section() -> None:
    """A repo without `[package]` never calls `ci-package.yml`; requiring
    `package / evidence` there would pin a never-reported status."""
    ruleset = _ci_gates_develop(_project(), _ci())
    assert "package / evidence" not in _check_names(ruleset)
    ruleset = _ci_gates_develop(_project(), _ci(), package=False)
    assert "package / evidence" not in _check_names(ruleset)


# `docs / docs` is deliberately absent here — it is conditional on
# `[publish].docs` (see the two tests above), not universal.
UNIVERSAL_REUSABLE_CI_CHECKS = frozenset(
    {
        "quality / evidence",
        "security / trivy",
        "security / semgrep",
        "version / version-bump",
    }
)


def test_universal_reusable_ci_checks_are_all_required() -> None:
    """Pins the vergil-actions reusable-CI universal checks. Update this set
    deliberately when the reusable CI adds/removes a universal PR job."""
    ruleset = _ci_gates_develop(_project(), _ci())
    contexts = {
        str(c["context"])
        for rule in ruleset.rules
        for c in cast(
            "dict[str, list[dict[str, object]]]",
            rule["parameters"],
        )["required_status_checks"]
    }
    assert contexts >= UNIVERSAL_REUSABLE_CI_CHECKS


def test_ci_gates_ghas_checks_use_ghas_integration_id() -> None:
    r = _ci_gates_develop(_project(), _ci())
    checks = _checks(r)
    ghas_names = ("Trivy", "Semgrep OSS", "CodeQL")
    ghas_checks = {c["context"]: c["integration_id"] for c in checks if c["context"] in ghas_names}
    assert ghas_checks == {"Trivy": 57789, "Semgrep OSS": 57789, "CodeQL": 57789}


def test_ci_gates_codeql_for_supported_language() -> None:
    r = _ci_gates_develop(_project(language="python"), _ci())
    names = _check_names(r)
    assert "security / codeql" in names
    assert "CodeQL" in names


def test_ci_gates_codeql_for_cpp() -> None:
    # cpp is CodeQL-supported (epic vergil-project/.github#207 T7).
    r = _ci_gates_develop(_project(language="cpp"), _ci(versions=["clang-20"]))
    names = _check_names(r)
    assert "security / codeql" in names
    assert "CodeQL" in names


def test_ci_gates_codeql_for_typescript() -> None:
    # typescript is CodeQL-supported (epic vergil-project/.github#284 T6). It is
    # keyed by the *primary-language* name (`typescript`), NOT the CodeQL
    # analysis identifier `javascript-typescript`: this set is tested against
    # `project.primary_language`, and the `typescript → javascript-typescript`
    # mapping belongs to the reusable CI Action (T7), not here.
    r = _ci_gates_develop(_project(language="typescript"), _ci(versions=["node-24", "node-22"]))
    names = _check_names(r)
    assert "security / codeql" in names
    assert "CodeQL" in names


def test_ci_gates_codeql_for_rust() -> None:
    # CodeQL analyzes Rust, so a GHAS Rust repo requires both CodeQL checks;
    # repo-init's ci.yml runs CodeQL for it from the same set (issue #4381).
    r = _ci_gates_develop(_project(language="rust"), _ci())
    names = _check_names(r)
    assert "security / codeql" in names
    assert "CodeQL" in names


def test_codeql_languages_are_primary_languages() -> None:
    # CODEQL_LANGUAGES is keyed by primary-language name: an entry outside the
    # enum can never match a repo, so it would only hide a drift (issue #4381).
    from vergil_tooling.lib.config import _ENUMS
    from vergil_tooling.lib.github_config import CODEQL_LANGUAGES

    assert CODEQL_LANGUAGES.issubset(_ENUMS["primary-language"])


def test_ci_gates_no_codeql_without_language() -> None:
    r = _ci_gates_develop(_project(language=None), _ci())
    names = _check_names(r)
    assert "security / codeql" not in names
    assert "CodeQL" not in names


def test_ci_gates_require_evidence_not_per_version() -> None:
    """The matrixed kinds (audit/lint/typecheck/unit) are gated on the stable,
    version-agnostic ``<kind> / evidence`` aggregate each reusable workflow
    emits — one per workflow — never per-version check names. A matrix change
    (including a reduction) then merges through the same gate instead of
    deadlocking on a per-version check the reduced matrix can never report
    (epic vergil-project/.github#338)."""
    cfg = _ci(versions=["3.12", "3.13", "3.14"])
    ruleset = _ci_gates_develop(_project(), cfg)
    checks = {str(c["context"]) for c in _checks(ruleset)}
    assert {"audit / evidence", "quality / evidence", "test / evidence"} <= checks
    assert not any(re.search(r"/ 3\.\d+$", c) for c in checks)
    # The retired per-version names are gone entirely.
    retired = (
        "quality / lint /",
        "quality / typecheck /",
        "test / unit /",
        "audit / dependencies /",
    )
    assert not any(c.startswith(retired) for c in checks)


def test_ci_gates_integration_tests_when_enabled() -> None:
    ci = _ci(versions=["3.12", "3.13"], integration_tests=True)
    r = _ci_gates_develop(_project(), ci, integration_versions=("3.12", "3.13"))
    names = _check_names(r)
    assert "test / integration / 3.12" in names
    assert "test / integration / 3.13" in names


def test_ci_gates_no_integration_tests_when_disabled() -> None:
    ci = _ci(versions=["3.12"], integration_tests=False)
    r = _ci_gates_develop(_project(), ci)
    names = _check_names(r)
    assert not any("test / integration" in n for n in names)


def test_ci_gates_release_version_bump_present() -> None:
    r = _ci_gates_develop(_project(release_model="tagged-release"), _ci())
    assert "version / version-bump" in _check_names(r)


def test_ci_gates_no_release_when_none() -> None:
    r = _ci_gates_develop(_project(release_model="none"), _ci())
    assert "version / version-bump" not in _check_names(r)


def test_ci_gates_no_language_has_no_versioned_checks() -> None:
    r = _ci_gates_develop(_project(language=None), _ci(versions=["latest"]))
    names = _check_names(r)
    assert "quality / evidence" in names
    assert not any("quality / lint" in n for n in names)
    assert not any("test / unit" in n for n in names)


@pytest.mark.parametrize("language", [None, "python"])
def test_ci_gates_require_quality_evidence_not_common(language: str | None) -> None:
    """Since vergil-actions v2.1.58, ci-quality.yml's ``evidence`` job needs
    ``common``, ``custom``, ``lint`` and ``typecheck`` in every repo whatever
    its language, so ``quality / evidence`` is the one quality gate: required
    for a language-less repo (where a red ``quality / custom`` would otherwise
    merge) and for a Python repo alike, replacing ``quality / common`` outright
    (vergil-project/vergil-tooling#3660, epic vergil-project/.github#488)."""
    names = _check_names(_ci_gates_develop(_project(language=language), _ci()))
    assert names.count("quality / evidence") == 1
    assert "quality / common" not in names


def test_lang_has_check_returns_false_for_unknown_check() -> None:
    assert _lang_has_check("python", "nonexistent") is False


# ---------------------------------------------------------------------------
# CI-gate producibility cross-check tests — ci.yml parsing path (issue #2720)
# ---------------------------------------------------------------------------


def _ci_yaml(*, jobs: dict[str, str]) -> str:
    """A minimal generated-ci.yml fragment: one `<job>:` → reusable-workflow use.

    Mirrors the shape `render_ci_workflow` emits (2-space job id, 4-space
    ``uses:`` pinned to a ``vergil-actions`` reusable workflow), enough for the
    caller-job parser without pulling in the full renderer.
    """
    lines = ["name: CI\n", "on:\n", "  pull_request:\n", "jobs:\n"]
    for job, reusable in jobs.items():
        lines.append(f"  {job}:\n")
        lines.append(f"    uses: vergil-project/vergil-actions/.github/workflows/{reusable}@v2.1\n")
    return "".join(lines)


_FULL_CI_JOBS = {
    "audit": "ci-audit.yml",
    "quality": "ci-quality.yml",
    "security": "ci-security.yml",
    "docs": "ci-docs.yml",
    "test": "ci-test.yml",
    "version": "ci-version-bump.yml",
}

_PACKAGE_CI_JOBS = {**_FULL_CI_JOBS, "package": "ci-package.yml"}


def test_required_status_contexts_extracts_names() -> None:
    ruleset = _ci_gates_develop(_project(), _ci())
    contexts = required_status_contexts(ruleset)
    assert "quality / evidence" in contexts
    assert "quality / common" not in contexts
    assert "test / evidence" in contexts
    # No empty strings from malformed entries.
    assert all(contexts)


def test_producible_when_all_required_contexts_have_a_pr_job() -> None:
    """A docs-publishing python repo whose ci.yml emits every required context
    (including `docs / docs`) has nothing unproducible."""
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True)
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS)
    assert unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset)) == []


def test_integration_context_is_flagged_unproducible() -> None:
    """`test / integration / <v>` is required when integration-tests=true, but
    ci-test.yml emits only `unit` — so the context can never report (#2720)."""
    ruleset = _ci_gates_develop(
        _project(),
        _ci(integration_tests=True),
        docs=True,
        integration_versions=("3.14",),
    )
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS)
    missing = unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset))
    assert missing == ["test / integration / 3.14"]


def test_docs_context_flagged_when_ci_lacks_docs_job() -> None:
    """The pre-fix `publish-docs` defect: the ruleset requires `docs / docs`, but
    a ci.yml with no docs job never reports it on a PR (#2720)."""
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True)
    jobs_without_docs = {k: v for k, v in _FULL_CI_JOBS.items() if k != "docs"}
    ci_yaml = _ci_yaml(jobs=jobs_without_docs)
    missing = unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset))
    assert missing == ["docs / docs"]


def test_ghas_literal_contexts_are_producible() -> None:
    """The GHAS-app checks (`Trivy`, `Semgrep OSS`, `CodeQL`) the ruleset requires
    report on a PR, since every repo is public and uploads SARIF."""
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS)
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True)
    required = required_status_contexts(ruleset)
    assert {"Trivy", "Semgrep OSS", "CodeQL"} <= set(required)
    assert unproducible_ci_yaml_contexts(ci_yaml, required) == []


def test_evidence_gates_are_producible() -> None:
    """The stable ``<kind> / evidence`` gates branch protection now requires are
    each emitted by their reusable workflow, so nothing is unproducible."""
    ruleset = _ci_gates_develop(_project(), _ci(versions=["3.12", "3.13", "3.14"]), docs=True)
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS)
    assert unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset)) == []


def test_per_version_legs_remain_producible_via_prefix() -> None:
    """The matrixed workflows still emit their per-version legs as (non-required)
    PR check runs, so a per-version context is producible via the version-family
    prefixes even though the ruleset no longer *requires* them."""
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS)
    legs = [
        "quality / lint / 3.14",
        "quality / typecheck / 3.12",
        "test / unit / 3.13",
        "audit / dependencies / 3.14",
    ]
    assert unproducible_ci_yaml_contexts(ci_yaml, legs) == []


def test_package_evidence_producible_when_ci_calls_ci_package() -> None:
    """`ci-package.yml` under a caller job keyed `package` emits the
    `package / evidence` gate on a PR, so a `[package]` repo is fully
    producible (epic #356, task A1's interface)."""
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True, package=True)
    ci_yaml = _ci_yaml(jobs=_PACKAGE_CI_JOBS)
    assert unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset)) == []


def test_package_evidence_flagged_when_ci_lacks_package_job() -> None:
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True, package=True)
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS)
    missing = unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset))
    assert missing == ["package / evidence"]


# The shape of vergil-containers' ci.yml: a PR-only ``images`` job calling the
# repo-local ``ci-images.yml`` (no ``@<ref>``), its ``if:`` line before ``uses:``.
_IMAGES_CI_YAML = (
    _ci_yaml(jobs=_FULL_CI_JOBS)
    + "  images:\n"
    + "    if: github.event_name == 'pull_request'\n"
    + "    uses: ./.github/workflows/ci-images.yml\n"
)


def test_images_evidence_producible_when_ci_calls_local_ci_images() -> None:
    """The ``images`` caller job running the repo-local ``ci-images.yml`` emits
    ``images / evidence`` on ``pull_request`` (vergil-tooling#4132)."""
    ruleset = _ci_gates_develop(_project(), _ci(images=True), docs=True)
    required = required_status_contexts(ruleset)
    assert unproducible_ci_yaml_contexts(_IMAGES_CI_YAML, required) == []


def test_images_evidence_flagged_when_ci_lacks_images_job() -> None:
    ruleset = _ci_gates_develop(_project(), _ci(images=True), docs=True)
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS)
    missing = unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset))
    assert missing == ["images / evidence"]


def test_images_evidence_flagged_when_ci_images_runs_under_another_job_id() -> None:
    """The context is ``<caller job> / evidence``: ``ci-images.yml`` called as
    ``image-builds`` emits ``image-builds / evidence``, never ``images / evidence``."""
    ruleset = _ci_gates_develop(_project(), _ci(images=True), docs=True)
    ci_yaml = _ci_yaml(jobs=_FULL_CI_JOBS) + (
        "  image-builds:\n    uses: ./.github/workflows/ci-images.yml\n"
    )
    missing = unproducible_ci_yaml_contexts(ci_yaml, required_status_contexts(ruleset))
    assert missing == ["images / evidence"]


def test_unrecognized_reusable_workflow_produces_no_contexts() -> None:
    """A job wired to an unknown reusable workflow contributes nothing to the
    producible set, so any required context is flagged (the safe default)."""
    ci_yaml = _ci_yaml(jobs={"mystery": "ci-mystery.yml"})
    assert unproducible_ci_yaml_contexts(ci_yaml, ["quality / common"]) == ["quality / common"]


# -- assert_ci_gates_producible: the guard shared by repo-init and apply (#2788)


def _desired_with(rulesets: list[DesiredRuleset]) -> Any:
    """A desired-state stand-in: the guard reads only ``.rulesets``."""
    from types import SimpleNamespace

    return SimpleNamespace(rulesets=rulesets)


def test_assert_ci_gates_producible_passes_when_every_context_is_emitted() -> None:
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True)
    assert_ci_gates_producible(
        _desired_with([ruleset]), _ci_yaml(jobs=_FULL_CI_JOBS), source="ci.yml"
    )


def test_assert_ci_gates_producible_passes_for_images_repo() -> None:
    """``apply`` against a vergil-containers-shaped ci.yml with ``[ci].images``
    on is not refused (vergil-tooling#4132)."""
    ruleset = _ci_gates_develop(_project(), _ci(images=True), docs=True)
    assert_ci_gates_producible(_desired_with([ruleset]), _IMAGES_CI_YAML, source="ci.yml")


def test_assert_ci_gates_producible_names_source_and_missing_context() -> None:
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True)
    jobs_without_docs = {k: v for k, v in _FULL_CI_JOBS.items() if k != "docs"}
    with pytest.raises(RuntimeError) as exc:
        assert_ci_gates_producible(
            _desired_with([ruleset]),
            _ci_yaml(jobs=jobs_without_docs),
            source="o/r@develop:.github/workflows/ci.yml",
        )
    message = str(exc.value)
    assert "o/r@develop:.github/workflows/ci.yml never emits" in message
    assert "Unproducible required contexts: docs / docs" in message


def test_assert_ci_gates_producible_names_integration_tests_as_the_cause() -> None:
    """The integration contexts are put down to ``[ci].integration-tests = true``
    directly, with the reason, not offered as one guess among several (#4448)."""
    ruleset = _ci_gates_develop(
        _project(),
        _ci(integration_tests=True),
        docs=True,
        integration_versions=("3.13", "3.14"),
    )
    with pytest.raises(RuntimeError) as exc:
        assert_ci_gates_producible(
            _desired_with([ruleset]), _ci_yaml(jobs=_FULL_CI_JOBS), source="ci.yml"
        )
    message = str(exc.value)
    assert (
        "Unproducible required contexts: test / integration / 3.13, test / integration / 3.14"
        in message
    )
    assert (
        "Cause of test / integration / 3.13, test / integration / 3.14: "
        "[ci].integration-tests = true makes the CI-gates ruleset require" in message
    )
    assert "no vergil-actions CI job produces that check" in message
    assert "vergil-project/vergil-tooling#2721" in message
    assert "Likely causes" not in message
    # No other context is unproducible, so no other cause is offered.
    assert "regenerate or fix ci.yml" not in message


def test_assert_ci_gates_producible_separates_integration_from_other_causes() -> None:
    ruleset = _ci_gates_develop(
        _project(),
        _ci(integration_tests=True),
        docs=True,
        integration_versions=("3.14",),
    )
    jobs_without_docs = {k: v for k, v in _FULL_CI_JOBS.items() if k != "docs"}
    with pytest.raises(RuntimeError) as exc:
        assert_ci_gates_producible(
            _desired_with([ruleset]), _ci_yaml(jobs=jobs_without_docs), source="ci.yml"
        )
    message = str(exc.value)
    assert "Cause of test / integration / 3.14: [ci].integration-tests = true" in message
    assert "Cause of docs / docs: every required context must correspond" in message
    assert "regenerate or fix ci.yml first." in message


def test_assert_ci_gates_producible_other_cause_does_not_blame_integration_tests() -> None:
    ruleset = _ci_gates_develop(_project(), _ci(), docs=True)
    jobs_without_docs = {k: v for k, v in _FULL_CI_JOBS.items() if k != "docs"}
    with pytest.raises(RuntimeError) as exc:
        assert_ci_gates_producible(
            _desired_with([ruleset]), _ci_yaml(jobs=jobs_without_docs), source="ci.yml"
        )
    message = str(exc.value)
    assert "integration-tests" not in message
    assert "Cause of docs / docs: " in message


@pytest.mark.parametrize("name", ["", LEGACY_CI_GATES_RULESET_NAME, CI_GATES_MAIN])
def test_assert_ci_gates_producible_without_develop_ruleset_is_an_error(name: str) -> None:
    """With no ``CI gates (develop)`` ruleset (none at all, only the legacy name,
    or main's alone) the guard refuses rather than checking nothing (spec §6.1)."""
    rulesets = [dataclasses.replace(_ci_gates_develop(_project(), _ci()), name=name)]
    with pytest.raises(RuntimeError, match=r"no 'CI gates \(develop\)' ruleset"):
        assert_ci_gates_producible(_desired_with(rulesets if name else []), "", source="ci.yml")


def test_assert_ci_gates_producible_checks_the_main_ruleset() -> None:
    """A context only main's ruleset requires is still checked."""
    develop, main = desired_ci_gates_rulesets(_project(), _ci(), docs=False)
    _checks(main).append({"context": "docs / docs", "integration_id": 15368})
    jobs_without_docs = {k: v for k, v in _FULL_CI_JOBS.items() if k != "docs"}
    with pytest.raises(RuntimeError, match="Unproducible required contexts: docs / docs"):
        assert_ci_gates_producible(
            _desired_with([develop, main]),
            _ci_yaml(jobs=jobs_without_docs),
            source="ci.yml",
        )


# -- Local reusable-workflow calls (``uses: ./.github/workflows/...``, #3454)


def _local_ci_yaml(*, jobs: dict[str, str]) -> str:
    """`_ci_yaml`, but each job calls its reusable workflow by local path.

    vergil-actions dogfoods its own reusable workflows this way
    (``uses: ./.github/workflows/<name>.yml``, no ``@<ref>``) — issue #3454.
    """
    lines = ["name: CI\n", "on:\n", "  pull_request:\n", "jobs:\n"]
    for job, reusable in jobs.items():
        lines.append(f"  {job}:\n")
        lines.append(f"    uses: ./.github/workflows/{reusable}\n")
    return "".join(lines)


# vergil-actions' real ``.github/workflows/ci.yml`` (develop), verbatim: every
# job calls its reusable workflow by local path (issue #3454).
_VERGIL_ACTIONS_CI_YAML = """\
name: CI

on:
  pull_request:

permissions:
  contents: read
  security-events: write
  actions: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  docs:
    uses: ./.github/workflows/ci-docs.yml

  quality:
    uses: ./.github/workflows/ci-quality.yml
    with:
      language: shell
      container-prefix: dev
      container-suffix: base

  security:
    uses: ./.github/workflows/ci-security.yml
    permissions:
      contents: read
      security-events: write
      actions: read
    with:
      language: shell
      run-codeql: false
      container-prefix: dev
      container-suffix: base

  version:
    uses: ./.github/workflows/ci-version-bump.yml
    with:
      language: shell
      container-prefix: dev
      container-suffix: base
"""


def test_local_reusable_workflow_calls_produce_same_contexts_as_remote() -> None:
    """A ci.yml calling its reusable workflows by local path is exactly as
    producible as the equivalent remote-call ci.yml (issue #3454)."""
    contexts = [
        "Semgrep OSS",
        "Trivy",
        "CodeQL",
        "docs / docs",
        "quality / common",
        "quality / evidence",
        "quality / lint / 3.14",
        "security / semgrep",
        "security / trivy",
        "security / codeql",
        "audit / evidence",
        "audit / dependencies / 3.14",
        "test / evidence",
        "test / unit / 3.14",
        "test / integration / 3.14",
        "version / version-bump",
    ]
    local = unproducible_ci_yaml_contexts(_local_ci_yaml(jobs=_FULL_CI_JOBS), contexts)
    remote = unproducible_ci_yaml_contexts(_ci_yaml(jobs=_FULL_CI_JOBS), contexts)
    assert local == remote == ["test / integration / 3.14"]


def test_vergil_actions_real_ci_yaml_passes_the_guard() -> None:
    """vergil-actions' committed ci.yml emits every context its desired CI-gates
    ruleset requires — the exact set `apply` falsely refused (issue #3454)."""
    ruleset = _ci_gates_develop(_project(language="shell"), _ci(), docs=True)
    assert sorted(required_status_contexts(ruleset)) == [
        "Semgrep OSS",
        "Trivy",
        "docs / docs",
        "quality / evidence",
        "security / semgrep",
        "security / trivy",
        "version / version-bump",
    ]
    assert_ci_gates_producible(
        _desired_with([ruleset]),
        _VERGIL_ACTIONS_CI_YAML,
        source="vergil-project/vergil-actions@develop:.github/workflows/ci.yml",
    )


def test_vergil_actions_real_ci_yaml_still_refuses_unproducible_context() -> None:
    """Local calls widen nothing: a context no local job emits is still refused
    (vergil-actions has no test job, so `test / evidence` cannot report)."""
    missing = unproducible_ci_yaml_contexts(
        _VERGIL_ACTIONS_CI_YAML, ["docs / docs", "test / evidence"]
    )
    assert missing == ["test / evidence"]


def test_local_uses_outside_workflows_dir_is_not_a_reusable_call() -> None:
    """Only ``./.github/workflows/<name>.yml`` is a local reusable-workflow call;
    any other local path contributes nothing to the producible set."""
    ci_yaml = "jobs:\n  quality:\n    uses: ./actions/ci-quality.yml\n"
    assert unproducible_ci_yaml_contexts(ci_yaml, ["quality / common"]) == ["quality / common"]


# ---------------------------------------------------------------------------
# Producibility cross-check in the live audit — set-based path
# (epic vergil-project/.github#338, Task 2)
# ---------------------------------------------------------------------------


def test_unproducible_required_contexts_returns_required_minus_produced() -> None:
    """The audit helper is a plain set difference: required contexts that no
    known workflow gate produces."""
    required = {"audit / evidence", "audit / dependencies / 3.12"}
    produced = {"audit / evidence", "quality / evidence"}
    assert unproducible_required_contexts(required, produced) == {"audit / dependencies / 3.12"}


def test_unproducible_required_contexts_empty_when_all_produced() -> None:
    """Nothing is flagged when every required context is in the produced set."""
    required = {"audit / evidence", "quality / evidence"}
    produced = {"audit / evidence", "quality / evidence", "test / evidence"}
    assert unproducible_required_contexts(required, produced) == set()


def test_producible_gate_catalog_covers_desired_stable_gates() -> None:
    """Drift guard: every stable (non-per-version) gate a fully-enabled desired
    ruleset requires must be in the produced catalog, so the catalog can never
    silently fall behind ``desired_ci_gates_ruleset``."""
    ruleset = _ci_gates_develop(
        _project(), _ci(integration_tests=True, images=True), docs=True, package=True
    )
    assert "images / evidence" in required_status_contexts(ruleset)
    stable = {
        c for c in required_status_contexts(ruleset) if not c.startswith("test / integration / ")
    }
    assert stable <= _PRODUCIBLE_GATE_CATALOG


def test_audit_flags_stale_per_version_required_context() -> None:
    """A live CI-gates ruleset still requiring a per-version leg the evidence
    model dropped (e.g. ``audit / dependencies / 3.12``) is flagged as an
    unproducible required context — a check no workflow produces as a durable
    gate, deadlocking every merge with green checks (epic #338)."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual_gates = next(r for r in actual.rulesets if r.name == CI_GATES_DEVELOP)
    _checks(actual_gates).append(
        {"context": "audit / dependencies / 3.12", "integration_id": 15368}
    )

    diff = compute_diff(desired=desired, actual=actual)

    assert not diff.is_compliant()
    unproducible = [
        item
        for item in diff.items
        if item.field == "rulesets.CI gates (develop).unproducible_required_context"
    ]
    assert [item.actual for item in unproducible] == ["audit / dependencies / 3.12"]


def test_audit_compliant_when_required_are_evidence_gates() -> None:
    """When the live ruleset requires exactly the ``<kind> / evidence`` gates the
    reusable workflows produce, nothing is unproducible and the audit is clean."""
    state = _state(_vergil_config(versions=["3.12", "3.13", "3.14"]))
    diff = compute_diff(desired=state, actual=state)
    assert diff.is_compliant()
    assert not any(
        item.field == f"rulesets.{CI_GATES_DEVELOP}.unproducible_required_context"
        for item in diff.items
    )


def test_audit_does_not_mislabel_producible_gate_as_unproducible() -> None:
    """A gate the workflows genuinely emit (``version / version-bump``) that the
    repo simply does not desire is drift, not an *unproducible* context — the
    producibility check must not mislabel it (only the ruleset diff flags it)."""
    desired = _state(_vergil_config(release_model="none"))
    actual = _state(_vergil_config(release_model="none"))
    actual_gates = next(r for r in actual.rulesets if r.name == CI_GATES_DEVELOP)
    _checks(actual_gates).append({"context": "version / version-bump", "integration_id": 15368})

    diff = compute_diff(desired=desired, actual=actual)

    assert not diff.is_compliant()  # still drift, via the ruleset diff
    assert not any(
        item.field == f"rulesets.{CI_GATES_DEVELOP}.unproducible_required_context"
        for item in diff.items
    )


# ---------------------------------------------------------------------------
# Version-agnostic matrixed gates (epic vergil-project/.github#338)
# ---------------------------------------------------------------------------


def test_ci_gates_matrixed_kinds_emit_one_evidence_gate_each() -> None:
    """Each matrixed reusable workflow contributes exactly one stable evidence
    gate: quality (common, custom, lint, typecheck), test (unit), audit
    (dependencies)."""
    ci = _ci(versions=["3.12", "3.13", "3.14"])
    names = _check_names(_ci_gates_develop(_project(), ci))
    assert sorted(n for n in names if n.endswith("/ evidence")) == [
        "audit / evidence",
        "quality / evidence",
        "test / evidence",
    ]


def test_ci_gates_matrixed_gates_are_independent_of_version_count() -> None:
    """The required set does not churn when ``[ci].versions`` changes — the whole
    point of requiring the evidence aggregate instead of per-version legs."""
    one = _check_names(_ci_gates_develop(_project(), _ci(versions=["3.14"])))
    three = _check_names(_ci_gates_develop(_project(), _ci(versions=["3.12", "3.13", "3.14"])))
    assert one == three


# ---------------------------------------------------------------------------
# Regression snapshot — existing languages' gates must stay identical
# ---------------------------------------------------------------------------
#
# Every existing library language emits lint/typecheck/unit/dependencies checks
# and is CodeQL-supported, so its required gates collapse to the same
# version-agnostic list. This snapshot pins the exact ordered check list; any
# drift in the order, naming, or count of the existing languages' gates fails
# here.

_EXPECTED_GATES_TWO_VERSIONS = [
    "quality / evidence",
    "security / trivy",
    "security / semgrep",
    "docs / docs",
    "Trivy",
    "Semgrep OSS",
    "security / codeql",
    "CodeQL",
    "audit / evidence",
    "test / evidence",
    "version / version-bump",
]


@pytest.mark.parametrize("language", ["python", "go", "rust"])
def test_ci_gates_existing_languages_snapshot(language: str) -> None:
    ci = _ci(versions=["v1", "v2"])
    r = _ci_gates_develop(_project(language=language), ci)
    assert _check_names(r) == _EXPECTED_GATES_TWO_VERSIONS


# ---------------------------------------------------------------------------
# compute_desired_state tests
# ---------------------------------------------------------------------------


# The org automation App's numeric ID in these tests' desired states.
_APP_ID = 1234567


def _state(config: VergilConfig) -> DesiredState:
    """The desired state of a run that manages bypass_actors (a human run)."""
    return compute_desired_state(
        config,
        skip_bypass=False,
        automation_app_id=_APP_ID,
    )


def _vergil_config(
    *,
    language: str = "python",
    release_model: str = "tagged-release",
    versions: list[str] | None = None,
    integration_tests: bool = False,
    images: bool = False,
    docs: bool = True,
    package: PackageConfig | None = None,
) -> VergilConfig:
    return VergilConfig(
        project=_project(language=language, release_model=release_model),
        dependencies={"vergil": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=_ci(versions=versions or ["3.14"], integration_tests=integration_tests, images=images),
        publish=PublishConfig(release=False, docs=docs, install=None),
        container=ContainerConfig(env_prefixes=[], system_packages=[]),
        validation=ValidationConfig(container_command=DEFAULT_VALIDATION_COMMAND),
        packages=(package,) if package is not None else (),
    )


def _package_config() -> PackageConfig:
    return PackageConfig(
        builder="python",
        vendor="vergil",
        summary="s",
        smoke="true",
        python=PackagePythonConfig(runtime="3.14.4"),
    )


def test_resolved_integration_versions_empty_when_disabled() -> None:
    assert resolved_integration_versions(_vergil_config()) == ()


def test_resolved_integration_versions_follow_the_resolver() -> None:
    # The pin resolves against the fixture window (conftest), newest first.
    cfg = _vergil_config(versions=["3.12", "3.14"], integration_tests=True)
    assert resolved_integration_versions(cfg) == ("3.14", "3.12")


def test_resolved_integration_versions_no_language_is_latest() -> None:
    cfg = _vergil_config(integration_tests=True)
    cfg.project.primary_language = None
    assert resolved_integration_versions(cfg) == ("latest",)


def test_compute_desired_state_requires_resolved_integration_checks() -> None:
    cfg = _vergil_config(versions=["3.13"], integration_tests=True)
    state = _state(cfg)
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "test / integration / 3.13" in _check_names(gates)


def test_compute_desired_state_has_eight_rulesets() -> None:
    state = _state(_vergil_config())
    assert [r.name for r in state.rulesets] == [
        "Branch protection",
        "Tag protection",
        "Rolling tag branch names",
        "Automation branches",
        "Main updates",
        "Agent audit markers",
        CI_GATES_DEVELOP,
        CI_GATES_MAIN,
    ]


def test_legacy_ci_gates_is_undesired() -> None:
    """The shared ``CI gates`` ruleset is no longer desired, so the audit
    reports it as drift and apply deletes it (spec §6.1)."""
    state = _state(_vergil_config())
    assert LEGACY_CI_GATES_RULESET_NAME == "CI gates"
    assert LEGACY_CI_GATES_RULESET_NAME not in [r.name for r in state.rulesets]


def _legacy_ci_gates(desired: DesiredState) -> DesiredRuleset:
    """The pre-split shared ruleset, as a repo not yet migrated still has it."""
    develop = next(r for r in desired.rulesets if r.name == CI_GATES_DEVELOP)
    return dataclasses.replace(
        develop,
        name=LEGACY_CI_GATES_RULESET_NAME,
        ref_include=["refs/heads/main", "refs/heads/develop"],
        rules=[
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": _checks(develop),
                },
            }
        ],
    )


def test_drift_report_names_ruleset_split() -> None:
    """A repo still on the shared ruleset reports it as replaced by the pair,
    and the pair as missing, so the drift report names the change."""
    desired = _state(_vergil_config())
    others = [r for r in desired.rulesets if r.name not in CI_GATES_RULESET_NAMES]
    actual = dataclasses.replace(desired, rulesets=[*others, _legacy_ci_gates(desired)])

    diff = compute_diff(desired=desired, actual=actual)

    by_field = {item.field: item for item in diff.items}
    legacy = by_field["rulesets.CI gates"]
    assert legacy.expected == "absent (replaced by 'CI gates (develop)' and 'CI gates (main)')"
    assert legacy.actual == "present"
    for name in CI_GATES_RULESET_NAMES:
        assert by_field[f"rulesets.{name}"].actual == "missing"


def test_drift_report_names_develop_only_replacement() -> None:
    """A docs-single-branch repo's shared ruleset is replaced by develop's alone."""
    cfg = _vergil_config()
    cfg.project.branching_model = "docs-single-branch"
    desired = _state(cfg)
    actual = dataclasses.replace(desired, rulesets=[*desired.rulesets, _legacy_ci_gates(desired)])

    diff = compute_diff(desired=desired, actual=actual)

    legacy = next(item for item in diff.items if item.field == "rulesets.CI gates")
    assert legacy.expected == "absent (replaced by 'CI gates (develop)')"


def test_other_undesired_ruleset_is_plain_absent() -> None:
    desired = _state(_vergil_config())
    stray = dataclasses.replace(desired.rulesets[0], name="Stray")
    actual = dataclasses.replace(desired, rulesets=[*desired.rulesets, stray])

    diff = compute_diff(desired=desired, actual=actual)

    assert [(i.field, i.expected) for i in diff.items] == [("rulesets.Stray", "absent")]


def test_legacy_name_without_desired_pair_is_plain_absent() -> None:
    """With no CI-gates ruleset desired, nothing replaces the legacy one."""
    desired = _state(_vergil_config())
    others = [r for r in desired.rulesets if r.name not in CI_GATES_RULESET_NAMES]
    legacy = _legacy_ci_gates(desired)
    diff = compute_diff(
        desired=dataclasses.replace(desired, rulesets=others),
        actual=dataclasses.replace(desired, rulesets=[*others, legacy]),
    )
    assert [(i.field, i.expected) for i in diff.items] == [("rulesets.CI gates", "absent")]


def test_producibility_checks_both_rulesets() -> None:
    """An unproducible context is reported on whichever CI-gates ruleset requires it."""
    desired = _state(_vergil_config())
    for name in CI_GATES_RULESET_NAMES:
        actual = _state(_vergil_config())
        gates = next(r for r in actual.rulesets if r.name == name)
        _checks(gates).append({"context": "audit / dependencies / 3.12", "integration_id": 15368})

        diff = compute_diff(desired=desired, actual=actual)

        unproducible = [
            (item.field, item.actual)
            for item in diff.items
            if item.field.endswith(".unproducible_required_context")
        ]
        assert unproducible == [
            (f"rulesets.{name}.unproducible_required_context", "audit / dependencies / 3.12")
        ]


@pytest.mark.parametrize("name", CI_GATES_RULESET_NAMES)
def test_producibility_missing_ruleset_is_error(name: str) -> None:
    """A desired CI-gates ruleset with no live counterpart is a
    ``missing_ci_gates_ruleset`` item, never a silent skip."""
    desired = _state(_vergil_config())
    actual = dataclasses.replace(desired, rulesets=[r for r in desired.rulesets if r.name != name])

    diff = compute_diff(desired=desired, actual=actual)

    item = next(i for i in diff.items if i.field == f"rulesets.{name}.missing_ci_gates_ruleset")
    assert (item.expected, item.actual) == ("desired", "missing")


def test_producibility_undesired_live_ruleset_is_error() -> None:
    """The reverse: a live CI-gates ruleset the desired state has no counterpart
    for (a main ruleset on a docs-single-branch repo) is reported too."""
    cfg = _vergil_config()
    cfg.project.branching_model = "docs-single-branch"
    desired = _state(cfg)
    actual = _state(_vergil_config())

    diff = compute_diff(desired=desired, actual=actual)

    item = next(
        i for i in diff.items if i.field == f"rulesets.{CI_GATES_MAIN}.missing_ci_gates_ruleset"
    )
    assert (item.expected, item.actual) == ("not desired", "present")


def test_compute_desired_state_omits_docs_gate_when_publish_docs_false() -> None:
    """`[publish].docs = false` must flow through to the CI-gates ruleset, so a
    non-docs repo's branch protection never requires the never-reported
    `docs / docs` check (vergil-project/vergil-tooling#2647)."""
    state = _state(_vergil_config(docs=False))
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "docs / docs" not in _check_names(gates)


def test_compute_desired_state_keeps_docs_gate_when_publish_docs_true() -> None:
    state = _state(_vergil_config(docs=True))
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "docs / docs" in _check_names(gates)


def test_compute_desired_state_requires_package_gate_with_package_section() -> None:
    """`[package]` in `vergil.toml` must flow through to the CI-gates ruleset so
    branch protection requires `package / evidence` (epic #356, spec 8.1)."""
    cfg = _vergil_config(package=_package_config())
    state = _state(cfg)
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "package / evidence" in _check_names(gates)


def test_compute_desired_state_requires_package_gate_with_several_packages() -> None:
    """Several `[[package]]` entries are still one `package / evidence` gate
    (epic vergil-project/.github#472); presence reads `packages`, never `package`."""
    cfg = _vergil_config()
    cfg.packages = (_package_config(), _package_config())
    state = _state(cfg)
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "package / evidence" in _check_names(gates)


def test_compute_desired_state_requires_images_gate_with_ci_images() -> None:
    """``[ci].images`` in ``vergil.toml`` flows through to the CI-gates ruleset,
    and the audit then finds a live ruleset requiring it compliant."""
    state = _state(_vergil_config(images=True))
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "images / evidence" in _check_names(gates)
    assert compute_diff(desired=state, actual=state).is_compliant()


def test_compute_desired_state_omits_images_gate_by_default() -> None:
    state = _state(_vergil_config())
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "images / evidence" not in _check_names(gates)


def test_audit_reports_images_gate_drift_on_non_images_repo() -> None:
    """A repo without ``[ci].images`` whose live ruleset was hand-edited to
    require ``images / evidence`` is reported as drift, not silently accepted."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config(images=True))
    assert not compute_diff(desired=desired, actual=actual).is_compliant()


def test_compute_desired_state_omits_package_gate_without_package_section() -> None:
    state = _state(_vergil_config())
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    assert "package / evidence" not in _check_names(gates)


def test_compute_desired_state_keeps_full_ghas_posture() -> None:
    """Every repo is public, so GHAS is always on (epic vergil-project/.github#1476)."""
    state = _state(_vergil_config())
    assert state.security.secret_scanning == "enabled"  # noqa: S105
    gates = next(r for r in state.rulesets if r.name == CI_GATES_DEVELOP)
    names = _check_names(gates)
    assert "Trivy" in names
    assert "Semgrep OSS" in names
    assert "security / codeql" in names


def test_compute_desired_state_includes_repo_settings() -> None:
    state = _state(_vergil_config())
    assert state.repo_settings.default_branch == "develop"


def test_compute_desired_state_includes_security() -> None:
    state = _state(_vergil_config())
    assert state.security.secret_scanning == "enabled"  # noqa: S105


def test_compute_desired_state_includes_actions() -> None:
    state = _state(_vergil_config(language="rust"))
    assert state.actions_permissions.allowed_actions == "selected"
    assert "swatinem/*" in state.actions_permissions.patterns_allowed


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
    """Cover branches: non-dict summary, missing id, non-dict detail."""
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
    """A non-list rulesets listing raises rather than reading as no rulesets (#3162)."""
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
        pytest.raises(ValueError, match="not a list"),
    ):
        fetch_actual_state("o/r")


def test_fetch_actual_state_rulesets_reads_every_page() -> None:
    """Issue #3162: a ruleset listed on page 2 is still fetched and reported."""
    page1: list[object] = [{"id": n} for n in range(1, 31)]
    page2: list[object] = [{"id": 99}]
    repo_json: dict[str, object] = {"default_branch": "develop", "security_and_analysis": {}}

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets/99":
            return {"name": "Late", "target": "branch", "enforcement": "active"}
        if endpoint.startswith("repos/o/r/branches/"):
            raise GitHubAPIError(1, ("gh", "api", endpoint), "", "gh: (HTTP 404)")
        return {}

    def fake_read_output(*args: str, **_: object) -> str:
        assert args == ("api", "--paginate", "repos/o/r/rulesets?per_page=100")
        return json.dumps(page1) + json.dumps(page2)

    with (
        _real_pagination(fake_read_output),
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=mock_read_json),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=False,
        ),
    ):
        result = fetch_actual_state("o/r")

    names = [r.name for r in result.state.rulesets]
    assert len(names) == 31
    assert names[-1] == "Late"


def test_apply_rulesets_reads_every_page() -> None:
    """Issue #3162: a ruleset on page 2 is updated in place, not re-POSTed."""
    page1: list[object] = [{"name": f"Other {n}", "id": n} for n in range(1, 31)]
    page2: list[object] = [{"name": "Branch protection", "id": 42}]

    def fake_read_output(*args: str, **_: object) -> str:
        assert args == ("api", "--paginate", "repos/o/r/rulesets?per_page=100")
        return json.dumps(page1) + json.dumps(page2)

    with (
        _real_pagination(fake_read_output),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_rulesets("o/r", [desired_branch_protection_ruleset()])

    mock_write.assert_called_once()
    assert mock_write.call_args[0][:2] == ("PUT", "repos/o/r/rulesets/42")
    assert mock_del.call_count == 30


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
        "allow_update_branch": True,
        "has_downloads": False,
        "merge_commit_title": "MERGE_MESSAGE",
        "merge_commit_message": "PR_TITLE",
        "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
        "squash_merge_commit_message": "COMMIT_MESSAGES",
        "web_commit_signoff_required": True,
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
    assert s.allow_update_branch is True
    assert s.has_downloads is False
    assert s.merge_commit_title == "MERGE_MESSAGE"
    assert s.merge_commit_message == "PR_TITLE"
    assert s.squash_merge_commit_title == "COMMIT_OR_PR_TITLE"
    assert s.squash_merge_commit_message == "COMMIT_MESSAGES"
    assert s.web_commit_signoff_required is True


@pytest.fixture
def _ambient_gh_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_enabled() -> None:
    cp = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 204 No Content\n", stderr=""
    )
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert _fetch_vulnerability_alerts("o/r") is True


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_disabled() -> None:
    cp = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 404 Not Found\n", stderr=""
    )
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert _fetch_vulnerability_alerts("o/r") is False


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_disabled_on_404_exit() -> None:
    # gh api -i exits 1 on a 404; that status is the answer, not an error.
    cp = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="HTTP/2.0 404 Not Found\n", stderr="gh: Not Found (HTTP 404)"
    )
    with (
        patch("vergil_tooling.lib.github.subprocess.run", return_value=cp) as mock_run,
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
    ):
        assert _fetch_vulnerability_alerts("o/r") is False
    assert mock_run.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_retries_transient_error() -> None:
    blip = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="HTTP/2.0 502 Bad Gateway\n", stderr="gh: HTTP 502"
    )
    ok = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 204 No Content\n", stderr=""
    )
    with (
        patch("vergil_tooling.lib.github.subprocess.run", side_effect=[blip, ok]) as mock_run,
        patch("vergil_tooling.lib.github.time.sleep"),
    ):
        assert _fetch_vulnerability_alerts("o/r") is True
    assert mock_run.call_count == 2


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_raises_on_unexpected_status() -> None:
    # Previously any non-204 status (e.g. 403) silently read as "disabled".
    cp = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="HTTP/2.0 403 Forbidden\n", stderr="gh: HTTP 403"
    )
    with (
        patch("vergil_tooling.lib.github.subprocess.run", return_value=cp),
        pytest.raises(GitHubAPIError, match="403"),
    ):
        _fetch_vulnerability_alerts("o/r")


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_none_when_permission_missing() -> None:
    # The agent App lacks administration=read: report "not checked", no retry (#3816).
    cp = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=(
            "HTTP/2.0 403 Forbidden\nX-Accepted-Github-Permissions: administration=read\n\n"
            '{"message":"Resource not accessible by integration"}'
        ),
        stderr="gh: Resource not accessible by integration (HTTP 403)",
    )
    with (
        patch("vergil_tooling.lib.github.subprocess.run", return_value=cp) as mock_run,
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
    ):
        assert _fetch_vulnerability_alerts("o/r") is None
    assert mock_run.call_count == 1
    mock_sleep.assert_not_called()


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_error_drops_headers() -> None:
    cp = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=(
            "HTTP/2.0 403 Forbidden\nX-Github-Request-Id: ABC\n\n"
            '{"message":"API rate limit exceeded"}'
        ),
        stderr="gh: API rate limit exceeded (HTTP 403)",
    )
    with (
        patch("vergil_tooling.lib.github.subprocess.run", return_value=cp),
        pytest.raises(GitHubAPIError) as excinfo,
    ):
        _fetch_vulnerability_alerts("o/r")
    message = str(excinfo.value)
    assert "API rate limit exceeded" in message
    assert "HTTP/2.0 403 Forbidden" in message
    assert "X-Github-Request-Id" not in message


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_unexpected_status_drops_headers() -> None:
    cp = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="HTTP/2.0 200 OK\nServer: github.com\n\n{}", stderr=""
    )
    with (
        patch("vergil_tooling.lib.github.subprocess.run", return_value=cp),
        pytest.raises(GitHubAPIError) as excinfo,
    ):
        _fetch_vulnerability_alerts("o/r")
    assert "Server: github.com" not in str(excinfo.value)


def test_without_headers_keeps_non_http_output() -> None:
    assert _without_headers("plain text") == "plain text"
    assert _without_headers("") == ""
    assert _without_headers(None) == ""


@pytest.mark.usefixtures("_ambient_gh_auth")
def test_fetch_vulnerability_alerts_raises_on_unexpected_success_status() -> None:
    cp = subprocess.CompletedProcess(args=[], returncode=0, stdout="HTTP/2.0 200 OK\n", stderr="")
    with (
        patch("vergil_tooling.lib.github.subprocess.run", return_value=cp),
        pytest.raises(GitHubAPIError, match="unexpected vulnerability-alerts status"),
    ):
        _fetch_vulnerability_alerts("o/r")


# ---------------------------------------------------------------------------
# Diff computation tests
# ---------------------------------------------------------------------------


def test_diff_identical_states_is_empty() -> None:
    state = _state(_vergil_config())
    diff = compute_diff(desired=state, actual=state)
    assert diff.is_compliant()
    assert diff.items == []


def test_diff_detects_repo_setting_mismatch() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.repo_settings.allow_auto_merge = False
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field == "repo_settings.allow_auto_merge" for d in diff.items)


def test_diff_detects_missing_ruleset() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.rulesets = []
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field.startswith("rulesets.") for d in diff.items)


def test_diff_detects_extra_ruleset() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
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
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.actions_permissions.default_workflow_permissions = "write"
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field == "actions_permissions.default_workflow_permissions" for d in diff.items)


def test_diff_detects_security_mismatch() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.security.vulnerability_alerts = False
    diff = compute_diff(desired=desired, actual=actual)
    assert not diff.is_compliant()
    assert any(d.field == "security.vulnerability_alerts" for d in diff.items)


def test_diff_detects_new_repo_setting_drift() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.repo_settings.merge_commit_title = "PR_TITLE"
    actual.repo_settings.web_commit_signoff_required = False
    diff = compute_diff(desired=desired, actual=actual)
    fields = {item.field for item in diff.items}
    assert "repo_settings.merge_commit_title" in fields
    assert "repo_settings.web_commit_signoff_required" in fields


def test_diff_reports_secret_scanning_off_as_drift() -> None:
    """Secret scanning is managed on every repo, so a repo with it off drifts."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.security.secret_scanning = None
    actual.security.secret_scanning_push_protection = "disabled"  # noqa: S105
    diff = compute_diff(desired=desired, actual=actual)
    assert {d.field for d in diff.items} == {
        "security.secret_scanning",
        "security.secret_scanning_push_protection",
    }
    assert not any(f.startswith("security.") for f in diff.skipped)


def test_diff_has_no_security_skipped() -> None:
    state = _state(_vergil_config())
    diff = compute_diff(desired=state, actual=state)
    assert diff.is_compliant()
    assert not any(s.startswith("security.") for s in diff.skipped)


# -- Issue #1368: required-status-check drift must never read as compliant ----


def test_diff_detects_extra_required_check() -> None:
    """A required check on the live ruleset but absent from desired is drift.

    This is the issue #1368 scenario: release-model ``none`` drops
    ``version / version-bump`` from desired, but the live ruleset still
    requires it.  The audit must not report compliant.
    """
    desired = _state(_vergil_config(release_model="none"))
    actual = _state(_vergil_config(release_model="none"))
    actual_gates = next(r for r in actual.rulesets if r.name == CI_GATES_DEVELOP)
    _checks(actual_gates).append({"context": "version / version-bump", "integration_id": 15368})

    diff = compute_diff(desired=desired, actual=actual)

    assert not diff.is_compliant()
    assert any(d.field == "rulesets.CI gates (develop).rules" for d in diff.items)


def test_diff_detects_missing_required_check() -> None:
    """A required check desired but absent from the live ruleset is drift."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual_gates = next(r for r in actual.rulesets if r.name == CI_GATES_DEVELOP)
    _checks(actual_gates).pop()

    diff = compute_diff(desired=desired, actual=actual)

    assert not diff.is_compliant()
    assert any(d.field == "rulesets.CI gates (develop).rules" for d in diff.items)


def test_diff_ignores_required_check_ordering() -> None:
    """Reordered-but-identical check sets are not drift (no false positive)."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual_gates = next(r for r in actual.rulesets if r.name == CI_GATES_DEVELOP)
    _checks(actual_gates).reverse()

    diff = compute_diff(desired=desired, actual=actual)

    assert diff.is_compliant()


def test_diff_ignores_rule_ordering() -> None:
    """Reordered-but-identical rule lists are not drift (no false positive)."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual_bp = next(r for r in actual.rulesets if r.name == "Branch protection")
    actual_bp.rules.reverse()

    diff = compute_diff(desired=desired, actual=actual)

    assert diff.is_compliant()


def _pull_request_params(state: Any) -> dict[str, object]:
    bp = next(r for r in state.rulesets if r.name == "Branch protection")
    rule = next(r for r in bp.rules if r.get("type") == "pull_request")
    return cast("dict[str, object]", rule["parameters"])


def test_diff_reports_undeclared_rule_parameter_distinctly() -> None:
    """A live parameter the desired rule never declares is its own category.

    This is the #2860 footprint: GitHub introduces a new ``pull_request``
    parameter and defaults it server-side. It must surface as an undeclared
    parameter naming the rule, key and live value, not as an opaque
    ``.rules`` value mismatch (issue #3548).
    """
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    _pull_request_params(actual)["require_brand_new_knob"] = True

    diff = compute_diff(desired=desired, actual=actual)

    assert not diff.is_compliant()
    assert diff.items == []
    assert diff.undeclared == [
        UndeclaredParam(
            ruleset="Branch protection",
            rule_type="pull_request",
            key="require_brand_new_knob",
            value=True,
        )
    ]


def test_diff_undeclared_parameter_does_not_mask_value_mismatch() -> None:
    """A declared value drifting alongside an undeclared key reports both."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    params = _pull_request_params(actual)
    params["require_brand_new_knob"] = True
    params["required_approving_review_count"] = 2

    diff = compute_diff(desired=desired, actual=actual)

    assert [d.field for d in diff.items] == ["rulesets.Branch protection.rules"]
    assert [u.key for u in diff.undeclared] == ["require_brand_new_knob"]
    # The value-mismatch item no longer carries the undeclared key.
    actual_rules = cast("list[dict[str, object]]", diff.items[0].actual)
    pr_rule = next(r for r in actual_rules if r.get("type") == "pull_request")
    assert "require_brand_new_knob" not in cast("dict[str, object]", pr_rule["parameters"])


def test_diff_undeclared_parameters_on_parameterless_desired_rule() -> None:
    """Parameters on a rule the desired spec declares bare are all undeclared."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    bp = next(r for r in actual.rulesets if r.name == "Branch protection")
    deletion = next(r for r in bp.rules if r.get("type") == "deletion")
    deletion["parameters"] = {"new_knob": "x"}

    diff = compute_diff(desired=desired, actual=actual)

    assert diff.items == []
    assert diff.undeclared == [
        UndeclaredParam(
            ruleset="Branch protection", rule_type="deletion", key="new_knob", value="x"
        )
    ]


def test_diff_strip_listed_parameter_is_not_undeclared() -> None:
    """A strip-listed GitHub default is ignored, never reported as undeclared."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    bp = next(r for r in actual.rulesets if r.name == "Branch protection")
    _pull_request_params(actual)["dismissal_restriction"] = {"enabled": False}
    bp.rules = _normalize_rules(bp.rules)

    diff = compute_diff(desired=desired, actual=actual)

    assert diff.is_compliant()
    assert diff.undeclared == []


def test_diff_extra_rule_type_is_value_drift_not_undeclared() -> None:
    """A whole live rule absent from desired is rule drift, not a parameter."""
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    bp = next(r for r in actual.rulesets if r.name == "Branch protection")
    bp.rules.append({"type": "required_linear_history", "parameters": {"k": 1}})
    bp.rules.append(cast("dict[str, object]", "not-a-dict"))

    diff = compute_diff(desired=desired, actual=actual)

    assert [d.field for d in diff.items] == ["rulesets.Branch protection.rules"]
    assert diff.undeclared == []


def test_canonicalize_rules_preserves_non_dict_entries() -> None:
    """Non-dict rule entries are passed through unchanged before sorting."""
    assert _canonicalize_rules(["z", "a"]) == ["a", "z"]


# ---------------------------------------------------------------------------
# Apply tests
# ---------------------------------------------------------------------------


def test_apply_repo_settings_calls_write_json() -> None:
    settings = desired_repo_settings()
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
    settings = desired_repo_settings()
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


def test_apply_rulesets_non_list_response_raises_without_writing() -> None:
    """A malformed listing aborts; it used to read as empty and re-POST every ruleset."""
    ruleset = desired_branch_protection_ruleset()
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value={"error": "unexpected"},
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
        pytest.raises(ValueError, match="not a list"),
    ):
        _apply_rulesets("o/r", [ruleset])
    mock_write.assert_not_called()
    mock_del.assert_not_called()


def test_ruleset_body_structure() -> None:
    ruleset = desired_tag_protection_ruleset(_APP_ID)
    body = _ruleset_body(ruleset)
    assert body["name"] == "Tag protection"
    assert body["target"] == "tag"
    assert body["enforcement"] == "active"
    assert body["conditions"] == {
        "ref_name": {"include": ["refs/tags/v*.*.*", "refs/tags/v*.*"], "exclude": []},
    }
    assert body["bypass_actors"] == ruleset.bypass_actors
    assert body["rules"] == ruleset.rules


def test_apply_desired_state_orchestrates_all() -> None:
    state = _state(_vergil_config())
    with (
        patch("vergil_tooling.lib.github_config._apply_repo_settings") as mock_repo,
        patch("vergil_tooling.lib.github_config._apply_security_settings") as mock_sec,
        patch("vergil_tooling.lib.github_config._apply_actions_permissions") as mock_actions,
        patch("vergil_tooling.lib.github_config._apply_rulesets") as mock_rulesets,
        patch("vergil_tooling.lib.github_config._apply_environments") as mock_envs,
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
    mock_envs.assert_called_once_with("o/r", state.environments)
    mock_cleanup.assert_called_once_with("o/r", state.rulesets)
    assert result == []


# ---------------------------------------------------------------------------
# fetch_actual_state: classic branch protection
# ---------------------------------------------------------------------------


# Every repo setting the audit compares, as GitHub returns it to a token that
# can see them all: the desired values (#4322).
_REPO_SETTINGS_JSON: dict[str, object] = dataclasses.asdict(desired_repo_settings())


def _fetch_with_classic_protection(
    protection_by_branch: dict[str, object],
    *,
    default_branch: str = "develop",
    rulesets_summary: list[object] | None = None,
    ruleset_details: dict[str, object] | None = None,
) -> FetchResult:
    """Run ``fetch_actual_state`` with mocked classic protection per branch.

    ``protection_by_branch`` maps a branch name to the object returned for its
    ``branches/<b>/protection`` read; a branch absent from the map raises a 404
    (no classic protection), the real-world default.
    """
    repo_json: dict[str, object] = {
        **_REPO_SETTINGS_JSON,
        "default_branch": default_branch,
        "security_and_analysis": {},
    }
    ruleset_details = ruleset_details or {}

    def mock_read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1] if len(args) > 1 else ""
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return rulesets_summary or []
        if endpoint.startswith("repos/o/r/rulesets/"):
            return cast("dict[str, object]", ruleset_details.get(endpoint.rsplit("/", 1)[-1], {}))
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "all"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        if endpoint.startswith("repos/o/r/branches/") and endpoint.endswith("/protection"):
            branch = endpoint[len("repos/o/r/branches/") : -len("/protection")]
            if branch in protection_by_branch:
                return cast("dict[str, object]", protection_by_branch[branch])
            raise GitHubAPIError(1, ("gh", "api", endpoint), "", "gh: (HTTP 404)")
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
        return fetch_actual_state("o/r")


def test_fetch_actual_state_flags_stale_classic_ci_context_as_drift() -> None:
    protection: dict[str, object] = {
        "required_status_checks": {
            "strict": True,
            "contexts": ["audit / dependencies / 3.12"],
        },
        "required_pull_request_reviews": {"required_approving_review_count": 1},
    }
    result = _fetch_with_classic_protection({"develop": protection})

    # The stale version-suffixed leg is surfaced in the fetched actual state.
    stale = [
        rs
        for rs in result.state.rulesets
        if "audit / dependencies / 3.12" in required_status_contexts(rs)
    ]
    assert stale, "classic stale CI context must be included in the fetched state"
    assert stale[0].ref_include == ["refs/heads/develop"]

    # And it shows up as drift against the evidence-only desired ruleset.
    desired = _state(
        _vergil_config(versions=["3.12", "3.13", "3.14"]),
    )
    diff = compute_diff(desired=desired, actual=result.state)
    assert not diff.is_compliant()
    assert any("classic branch protection" in item.field for item in diff.items)


def test_fetch_actual_state_ignores_non_stale_classic_protection() -> None:
    protection: dict[str, object] = {
        # No version-suffixed CI leg — only the stable gate and a review rule.
        "required_status_checks": {"strict": True, "contexts": ["audit / evidence"]},
        "required_pull_request_reviews": {"required_approving_review_count": 2},
    }
    result = _fetch_with_classic_protection({"develop": protection})
    assert not any(rs.name.startswith("classic branch protection") for rs in result.state.rulesets)


def test_fetch_actual_state_fail_soft_when_no_classic_protection() -> None:
    # No branch mapped → every protection read 404s; fetch must not raise.
    result = _fetch_with_classic_protection({})
    assert not any(rs.name.startswith("classic branch protection") for rs in result.state.rulesets)


def test_fetch_actual_state_classic_drift_without_default_branch() -> None:
    # A repo with no reported default branch still inspects ruleset-covered
    # branches for stale classic legs.
    summary: list[object] = [{"id": 3}]
    detail: dict[str, object] = {
        "name": "Branch protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
        "bypass_actors": [],
        "rules": [{"type": "deletion"}],
    }
    protection: dict[str, object] = {
        "required_status_checks": {"strict": True, "contexts": ["audit / dependencies / 3.12"]},
    }
    result = _fetch_with_classic_protection(
        {"main": protection},
        default_branch="",
        rulesets_summary=summary,
        ruleset_details={"3": detail},
    )
    synthetic = [
        rs for rs in result.state.rulesets if rs.name.startswith("classic branch protection")
    ]
    assert [rs.ref_include for rs in synthetic] == [["refs/heads/main"]]


def test_fetch_actual_state_inspects_ruleset_covered_branches() -> None:
    # A branch-target ruleset covering main pulls main into the inspected set,
    # even though the default branch is develop.
    summary: list[object] = [{"id": 7}]
    detail: dict[str, object] = {
        "name": "Branch protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
        "bypass_actors": [],
        "rules": [{"type": "deletion"}],
    }
    protection: dict[str, object] = {
        "required_status_checks": {"strict": True, "contexts": ["test / unit / 3.12"]},
    }
    result = _fetch_with_classic_protection(
        {"main": protection},
        rulesets_summary=summary,
        ruleset_details={"7": detail},
    )
    synthetic = [
        rs for rs in result.state.rulesets if rs.name.startswith("classic branch protection")
    ]
    assert len(synthetic) == 1
    assert synthetic[0].ref_include == ["refs/heads/main"]
    assert required_status_contexts(synthetic[0]) == ["test / unit / 3.12"]


# ---------------------------------------------------------------------------
# Classic branch protection cleanup tests
# ---------------------------------------------------------------------------


def _classic_protection_with_stale_leg() -> dict[str, object]:
    """A classic protection carrying a stale version-suffixed CI leg plus an
    unrelated setting the scoped cleanup must preserve."""
    return {
        "required_status_checks": {
            "strict": True,
            "contexts": ["audit / dependencies / 3.12", "audit / evidence"],
        },
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "enforce_admins": {"enabled": True},
    }


def test_cleanup_removes_only_stale_ci_contexts_and_preserves_the_rest() -> None:
    rulesets = [desired_branch_protection_ruleset()]  # covers main + develop
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value=_classic_protection_with_stale_leg(),
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_delete,
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)

    # Never deletes the whole protection object — scoped writes only.
    mock_delete.assert_not_called()
    assert sorted(r.branch for r in reports) == ["develop", "main"]
    for r in reports:
        assert r.removed_contexts == ["audit / dependencies / 3.12"]
        assert "required_pull_request_reviews" in r.preserved_settings
        assert "enforce_admins" in r.preserved_settings

    # Each branch is PATCHed on the required-status-checks sub-resource with only
    # the surviving (non-stale) contexts — the evidence gate stays required.
    # develop's PATCH also clears strict; main's keeps it (epic #1218).
    assert mock_write.call_count == 2
    bodies: dict[str, object] = {}
    for call in mock_write.call_args_list:
        method, endpoint, body = call[0][0], call[0][1], call[0][2]
        assert method == "PATCH"
        assert endpoint.endswith("/protection/required_status_checks")
        bodies[endpoint] = body
    assert bodies == {
        "repos/o/r/branches/develop/protection/required_status_checks": {
            "contexts": ["audit / evidence"],
            "strict": False,
        },
        "repos/o/r/branches/main/protection/required_status_checks": {
            "contexts": ["audit / evidence"],
        },
    }
    by_branch = {r.branch: r for r in reports}
    assert by_branch["develop"].cleared_strict is True
    assert by_branch["main"].cleared_strict is False


def test_cleanup_empties_contexts_when_every_leg_is_stale() -> None:
    rulesets = [
        DesiredRuleset(
            name="CI gates",
            target="branch",
            enforcement="active",
            ref_include=["refs/heads/main"],
            bypass_actors=[],
            rules=[],
        ),
    ]
    protection: dict[str, object] = {
        "required_status_checks": {
            "strict": True,
            "contexts": ["test / unit / 3.12", "test / unit / 3.13"],
        },
    }
    with (
        patch("vergil_tooling.lib.github_config.github.read_json", return_value=protection),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)
    assert [r.branch for r in reports] == ["main"]
    assert reports[0].removed_contexts == ["test / unit / 3.12", "test / unit / 3.13"]
    assert mock_write.call_args[0][2] == {"contexts": []}


def test_cleanup_leaves_branch_untouched_when_no_stale_contexts() -> None:
    rulesets = [desired_branch_protection_ruleset()]
    protection: dict[str, object] = {
        "required_status_checks": {"strict": False, "contexts": ["audit / evidence"]},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
    }
    with (
        patch("vergil_tooling.lib.github_config.github.read_json", return_value=protection),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)
    assert reports == []
    mock_write.assert_not_called()


def test_cleanup_skips_branch_without_classic_protection() -> None:
    rulesets = [desired_branch_protection_ruleset()]
    not_found = GitHubAPIError(
        1,
        ("gh", "api", "repos/o/r/branches/develop/protection"),
        "",
        "gh: Branch not protected (HTTP 404)",
    )
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=not_found,
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)
    assert reports == []
    mock_write.assert_not_called()


def test_cleanup_skips_tag_rulesets() -> None:
    rulesets = [desired_tag_protection_ruleset(_APP_ID)]
    with (
        patch("vergil_tooling.lib.github_config.github.read_json") as mock_read,
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)
    mock_read.assert_not_called()
    mock_write.assert_not_called()
    assert reports == []


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
    protection: dict[str, object] = {
        "required_status_checks": {"strict": True, "contexts": ["quality / lint / 3.14"]},
    }
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value=protection,
        ) as mock_read,
        patch("vergil_tooling.lib.github_config.github.write_json"),
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)
    mock_read.assert_called_once_with("api", "repos/o/r/branches/main/protection")
    assert [r.branch for r in reports] == ["main"]
    assert reports[0].removed_contexts == ["quality / lint / 3.14"]


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
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            return_value=_classic_protection_with_stale_leg(),
        ) as mock_read,
        patch("vergil_tooling.lib.github_config.github.write_json"),
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)
    # Each unique branch is inspected exactly once.
    assert mock_read.call_count == 2
    assert sorted(r.branch for r in reports) == ["develop", "main"]


# ---------------------------------------------------------------------------
# Classic protection's up-to-date flag (strict) on develop (epic #1218)
# ---------------------------------------------------------------------------


def _strict_protection(*, strict: bool) -> dict[str, object]:
    """A classic protection with no stale CI leg and the given ``strict``."""
    return {
        "required_status_checks": {"strict": strict, "contexts": ["audit / evidence"]},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "enforce_admins": {"enabled": True},
    }


def _desired_for_strict_tests() -> DesiredState:
    return _state(_vergil_config())


def _branch_protection_items(diff: ConfigDiff) -> list[DiffItem]:
    return [item for item in diff.items if item.field.startswith("branch_protection.")]


def test_classic_strict_on_develop_is_drift() -> None:
    result = _fetch_with_classic_protection({"develop": _strict_protection(strict=True)})
    assert result.state.classic_develop_strict is True

    diff = compute_diff(desired=_desired_for_strict_tests(), actual=result.state)
    assert _branch_protection_items(diff) == [
        DiffItem(field="branch_protection.develop.strict", expected=False, actual=True)
    ]
    assert not diff.is_compliant()


def test_classic_strict_on_develop_read_when_not_default_or_ruleset_covered() -> None:
    # develop is always inspected, even when it is neither the default branch
    # nor covered by a live ruleset.
    result = _fetch_with_classic_protection(
        {"develop": _strict_protection(strict=True)}, default_branch="main"
    )
    assert result.state.classic_develop_strict is True


def test_classic_strict_false_on_develop_no_item() -> None:
    result = _fetch_with_classic_protection({"develop": _strict_protection(strict=False)})
    diff = compute_diff(desired=_desired_for_strict_tests(), actual=result.state)
    assert _branch_protection_items(diff) == []


def test_apply_clears_only_strict() -> None:
    rulesets = [desired_branch_protection_ruleset()]  # covers main + develop

    def read_json(*args: str) -> dict[str, object]:
        if args[1] == "repos/o/r/branches/develop/protection":
            return _strict_protection(strict=True)
        raise GitHubAPIError(1, ("gh", "api", args[1]), "", "gh: Branch not protected (HTTP 404)")

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_delete,
    ):
        reports = _cleanup_classic_branch_protection("o/r", rulesets)

    # One scoped PATCH that names only strict: the contexts and every other
    # classic setting are left as they are.
    mock_delete.assert_not_called()
    mock_write.assert_called_once_with(
        "PATCH",
        "repos/o/r/branches/develop/protection/required_status_checks",
        {"strict": False},
    )
    assert reports == [
        ClassicProtectionReport(
            branch="develop",
            removed_contexts=[],
            preserved_settings=["enforce_admins", "required_pull_request_reviews"],
            cleared_strict=True,
        )
    ]
    assert reports[0].summary() == (
        "cleared the up-to-date requirement (strict); "
        "preserved [enforce_admins, required_pull_request_reviews]"
    )


def test_classic_strict_on_main_untouched() -> None:
    # Audit: main's strict is not drift.
    summary: list[object] = [{"id": 7}]
    detail: dict[str, object] = {
        "name": "Branch protection",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
        "bypass_actors": [],
        "rules": [{"type": "deletion"}],
    }
    result = _fetch_with_classic_protection(
        {"main": _strict_protection(strict=True)},
        rulesets_summary=summary,
        ruleset_details={"7": detail},
    )
    assert result.state.classic_develop_strict is False
    diff = compute_diff(desired=_desired_for_strict_tests(), actual=result.state)
    assert _branch_protection_items(diff) == []

    # Apply: main's classic protection is never written.
    def read_json(*args: str) -> dict[str, object]:
        if args[1] == "repos/o/r/branches/main/protection":
            return _strict_protection(strict=True)
        raise GitHubAPIError(1, ("gh", "api", args[1]), "", "gh: Branch not protected (HTTP 404)")

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
    ):
        reports = _cleanup_classic_branch_protection("o/r", [desired_branch_protection_ruleset()])
    mock_write.assert_not_called()
    assert reports == []


def test_no_classic_protection_no_item() -> None:
    result = _fetch_with_classic_protection({})
    assert result.state.classic_develop_strict is False
    diff = compute_diff(desired=_desired_for_strict_tests(), actual=result.state)
    assert _branch_protection_items(diff) == []

    not_found = GitHubAPIError(1, ("gh", "api", "x"), "", "gh: Branch not protected (HTTP 404)")
    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=not_found),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
    ):
        reports = _cleanup_classic_branch_protection("o/r", [desired_branch_protection_ruleset()])
    mock_write.assert_not_called()
    assert reports == []


def test_classic_develop_unreadable_is_not_checked_not_drift() -> None:
    # A token that cannot read develop's classic protection reports it as not
    # checked, never as strict drift (#3816).
    def read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return {**_REPO_SETTINGS_JSON, "default_branch": "", "security_and_analysis": {}}
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "all"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {"default_workflow_permissions": "read"}
        raise _forbidden(endpoint)

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config._fetch_vulnerability_alerts", return_value=True),
    ):
        result = fetch_actual_state("o/r")
    # The automation environment's read is refused the same way.
    assert [nc.field for nc in result.not_checked] == [
        "classic_protection.develop",
        "environments.automation",
    ]
    diff = compute_diff(
        desired=_desired_for_strict_tests(), actual=result.state, not_checked=result.not_checked
    )
    assert _branch_protection_items(diff) == []
    assert not diff.is_compliant()


def test_cleanup_report_summary_names_removed_contexts_and_strict() -> None:
    report = ClassicProtectionReport(
        branch="develop",
        removed_contexts=["audit / dependencies / 3.12"],
        preserved_settings=[],
        cleared_strict=True,
    )
    assert report.summary() == (
        "removed stale CI contexts [audit / dependencies / 3.12]; "
        "cleared the up-to-date requirement (strict); preserved [none]"
    )


# ---------------------------------------------------------------------------
# Classic-protection helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context",
    [
        "audit / dependencies / 3.12",
        "test / unit / 3.13",
        "quality / lint / 3.14",
        "quality / typecheck / 3.12",
    ],
)
def test_is_stale_ci_context_true_for_version_suffixed_matrixed_legs(context: str) -> None:
    assert _is_stale_ci_context(context) is True


@pytest.mark.parametrize(
    "context",
    [
        "audit / evidence",  # the stable gate the ruleset now owns
        "quality / evidence",
        "security / codeql",  # not version-suffixed
        "CodeQL",  # GHAS literal
        "version / version-bump",  # non-evidence
        "deploy / prod / 3.12",  # version-suffixed but not a CI evidence kind
    ],
)
def test_is_stale_ci_context_false_for_everything_else(context: str) -> None:
    assert _is_stale_ci_context(context) is False


def test_classic_required_contexts_unions_contexts_and_checks() -> None:
    protection: dict[str, object] = {
        "required_status_checks": {
            "contexts": ["audit / dependencies / 3.12"],
            "checks": [
                {"context": "audit / dependencies / 3.12", "app_id": 1},  # dup by name
                {"context": "test / unit / 3.13", "app_id": 2},
                {"not_a_context": True},  # ignored
                "not-a-dict-check",  # ignored (non-dict entry)
            ],
        },
    }
    assert _classic_required_contexts(protection) == [
        "audit / dependencies / 3.12",
        "test / unit / 3.13",
    ]


def test_classic_required_contexts_from_checks_only() -> None:
    # Newer API shape: only ``checks`` present, no legacy ``contexts`` list.
    protection: dict[str, object] = {
        "required_status_checks": {
            "checks": [{"context": "quality / typecheck / 3.14", "app_id": 9}],
        },
    }
    assert _classic_required_contexts(protection) == ["quality / typecheck / 3.14"]


def test_classic_required_contexts_empty_without_status_checks() -> None:
    assert _classic_required_contexts({"required_pull_request_reviews": {}}) == []
    assert _classic_required_contexts({"required_status_checks": "nope"}) == []


def test_setting_is_active_for_non_dict_values() -> None:
    from vergil_tooling.lib.github_config import _setting_is_active

    assert _setting_is_active(True) is True
    assert _setting_is_active(0) is False
    assert _setting_is_active("x") is True


def test_preserved_classic_settings_lists_active_non_status_check_keys() -> None:
    protection: dict[str, object] = {
        "url": "https://api.github.com/...",  # metadata, never reported
        "required_status_checks": {"contexts": []},  # the modified resource
        "required_pull_request_reviews": {"required_approving_review_count": 2},
        "enforce_admins": {"enabled": True},
        "required_linear_history": {"enabled": False},  # inactive → dropped
        "required_signatures": {"enabled": True},
    }
    assert _preserved_classic_settings(protection) == [
        "enforce_admins",
        "required_pull_request_reviews",
        "required_signatures",
    ]


def test_read_classic_protection_returns_dict() -> None:
    protection: dict[str, object] = {"required_status_checks": {"contexts": []}}
    with patch("vergil_tooling.lib.github_config.github.read_json", return_value=protection):
        assert _read_classic_protection("o/r", "develop") == protection


def test_read_classic_protection_none_on_non_dict() -> None:
    with patch("vergil_tooling.lib.github_config.github.read_json", return_value=[]):
        assert _read_classic_protection("o/r", "develop") is None


def test_read_classic_protection_none_when_absent() -> None:
    err = GitHubAPIError(
        1,
        ("gh", "api", "repos/o/r/branches/develop/protection"),
        "",
        "gh: Branch not protected (HTTP 404)",
    )
    with patch("vergil_tooling.lib.github_config.github.read_json", side_effect=err):
        assert _read_classic_protection("o/r", "develop") is None


def test_read_classic_protection_raises_on_forbidden() -> None:
    # A 403 is "cannot read", never "unprotected" (#3816).
    err = _forbidden("repos/o/r/branches/develop/protection")
    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=err),
        pytest.raises(GitHubAPIError, match="403"),
    ):
        _read_classic_protection("o/r", "develop")


def _forbidden(
    endpoint: str, message: str = "Resource not accessible by integration"
) -> GitHubAPIError:
    return GitHubAPIError(1, ("gh", "api", endpoint), "", f"gh: {message} (HTTP 403)")


def _admin_gap_read_json(repo_json: dict[str, object]) -> Callable[..., object]:
    """read_json for an App with metadata but no administration access."""

    def read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return []
        raise _forbidden(endpoint)

    return read_json


def test_fetch_actual_state_records_unreadable_admin_settings() -> None:
    # Under the agent App every admin-scoped read is a 403 and the repo object
    # carries no security_and_analysis block (#3816, #3869).
    repo_json: dict[str, object] = dict(_REPO_SETTINGS_JSON)
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=_admin_gap_read_json(repo_json),
        ),
        patch(
            "vergil_tooling.lib.github_config._fetch_vulnerability_alerts",
            return_value=None,
        ),
    ):
        result = fetch_actual_state("o/r")
    assert {nc.field for nc in result.not_checked} == {
        "security.secret_scanning",
        "security.secret_scanning_push_protection",
        "security.dependabot_security_updates",
        "security.vulnerability_alerts",
        "actions_permissions.allowed_actions",
        "actions_permissions.patterns_allowed",
        "actions_permissions.default_workflow_permissions",
        "actions_permissions.can_approve_pull_request_reviews",
        "classic_protection.develop",
        "environments.automation",
    }
    assert {nc.permission for nc in result.not_checked} == {"administration=read", "actions=read"}

    desired = _state(_vergil_config())
    diff = compute_diff(desired=desired, actual=result.state, not_checked=result.not_checked)
    # The placeholders are never compared, so nothing unread reads as drift...
    assert not any(item.field.startswith(("security.", "actions_")) for item in diff.items)
    assert len(diff.not_checked) == len(result.not_checked)
    # ...and an audit with unread settings is never compliant.
    assert not diff.is_compliant()


def test_fetch_actual_state_selected_actions_unreadable() -> None:
    def read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return {**_REPO_SETTINGS_JSON, "default_branch": "", "security_and_analysis": {}}
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "selected"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {"default_workflow_permissions": "read"}
        raise _forbidden(endpoint)

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config._fetch_vulnerability_alerts", return_value=True),
    ):
        result = fetch_actual_state("o/r")
    # develop's classic protection is always read (epic #1218), so its 403 is
    # recorded too.
    assert [nc.field for nc in result.not_checked] == [
        "actions_permissions.patterns_allowed",
        "classic_protection.develop",
        "environments.automation",
    ]


def test_fetch_actual_state_non_permission_403_propagates() -> None:
    # A 403 that names no missing permission (a rate limit) is a real failure.
    def read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return {"default_branch": "develop", "security_and_analysis": {}}
        raise _forbidden(endpoint, "API rate limit exceeded")

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config._fetch_vulnerability_alerts", return_value=True),
        pytest.raises(GitHubAPIError, match="rate limit"),
    ):
        fetch_actual_state("o/r")


def test_fetch_actual_state_classic_non_permission_403_propagates() -> None:
    def read_json(*args: str) -> dict[str, object] | list[object]:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return {"default_branch": "develop", "security_and_analysis": {}}
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint.endswith("/protection"):
            raise _forbidden(endpoint, "API rate limit exceeded")
        return {}

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config._fetch_vulnerability_alerts", return_value=True),
        pytest.raises(GitHubAPIError, match="rate limit"),
    ):
        fetch_actual_state("o/r")


def test_fetch_actual_state_readable_records_nothing_unread() -> None:
    result = _fetch_with_classic_protection({})
    assert result.not_checked == []


def _fetch_with_repo_json(repo_json: object) -> FetchResult:
    """Run ``fetch_actual_state`` with every read but ``repos/o/r`` readable."""

    def read_json(*args: str) -> object:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return repo_json
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == "repos/o/r/actions/permissions":
            return {"allowed_actions": "all"}
        if endpoint == "repos/o/r/actions/permissions/workflow":
            return {"default_workflow_permissions": "read"}
        raise GitHubAPIError(1, ("gh", "api", endpoint), "", "gh: (HTTP 404)")

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config._fetch_vulnerability_alerts", return_value=True),
    ):
        return fetch_actual_state("o/r")


def test_fetch_actual_state_missing_merge_setting_is_not_checked() -> None:
    # The scoped token got no merge settings back (vergil-actions#1154): a
    # missing field is not checked, never read as False or '' (#4322).
    repo_json = {**_REPO_SETTINGS_JSON, "security_and_analysis": {}}
    del repo_json["allow_merge_commit"]
    del repo_json["merge_commit_title"]
    result = _fetch_with_repo_json(repo_json)
    assert result.not_checked == [
        NotChecked(field="repo_settings.allow_merge_commit", permission=MERGE_SETTINGS_READ),
        NotChecked(field="repo_settings.merge_commit_title", permission=MERGE_SETTINGS_READ),
    ]

    desired = _state(_vergil_config())
    diff = compute_diff(desired=desired, actual=result.state, not_checked=result.not_checked)
    assert not any(item.field.startswith("repo_settings.") for item in diff.items)
    assert {nc.field for nc in diff.not_checked} == {
        "repo_settings.allow_merge_commit",
        "repo_settings.merge_commit_title",
    }
    assert not diff.is_compliant()


def test_fetch_actual_state_null_repo_setting_is_not_checked() -> None:
    repo_json = {**_REPO_SETTINGS_JSON, "security_and_analysis": {}, "has_wiki": None}
    result = _fetch_with_repo_json(repo_json)
    # has_wiki is not a merge setting: the docs name no permission for it (#4326).
    assert result.not_checked == [
        NotChecked(field="repo_settings.has_wiki", permission=SETTING_RETURNED)
    ]


def test_fetch_actual_state_every_missing_repo_setting_is_not_checked() -> None:
    result = _fetch_with_repo_json({"security_and_analysis": {}})
    assert [nc.field for nc in result.not_checked] == [
        f"repo_settings.{name}" for name in _REPO_SETTINGS_JSON
    ]
    # Only the merge-related settings name Contents read and write (#4326).
    merge_related = {
        "allow_auto_merge",
        "allow_merge_commit",
        "allow_rebase_merge",
        "allow_squash_merge",
        "allow_update_branch",
        "delete_branch_on_merge",
        "merge_commit_message",
        "merge_commit_title",
        "squash_merge_commit_message",
        "squash_merge_commit_title",
    }
    assert {
        nc.field.removeprefix("repo_settings."): nc.permission for nc in result.not_checked
    } == {
        name: MERGE_SETTINGS_READ if name in merge_related else SETTING_RETURNED
        for name in _REPO_SETTINGS_JSON
    }
    assert MERGE_SETTINGS_READ == "contents=read and contents=write"


@pytest.mark.parametrize(
    ("name", "value"),
    [("allow_merge_commit", "true"), ("merge_commit_title", 1)],
)
def test_fetch_actual_state_mistyped_repo_setting_raises(name: str, value: object) -> None:
    repo_json = {**_REPO_SETTINGS_JSON, "security_and_analysis": {}, name: value}
    with pytest.raises(RuntimeError, match=f"repository field '{name}'"):
        _fetch_with_repo_json(repo_json)


def test_fetch_actual_state_non_object_repo_response_raises() -> None:
    with pytest.raises(RuntimeError, match="not a repository object"):
        _fetch_with_repo_json([])


def test_compute_diff_not_checked_drops_unmanaged_fields() -> None:
    # A run that skips bypass_actors leaves them unmanaged (skipped), so their
    # unreadability is not reported as "not checked".
    desired = compute_desired_state(_vergil_config(), skip_bypass=True, automation_app_id=None)
    actual = compute_desired_state(_vergil_config(), skip_bypass=True, automation_app_id=None)
    actual.security.vulnerability_alerts = False
    unread = [
        NotChecked(field="rulesets.Tag protection.bypass_actors", permission="administration=read"),
        NotChecked(field="security.vulnerability_alerts", permission="administration=read"),
    ]
    diff = compute_diff(desired=desired, actual=actual, not_checked=unread)
    assert diff.items == []
    assert [nc.field for nc in diff.not_checked] == ["security.vulnerability_alerts"]
    assert not diff.has_drift()
    assert not diff.is_compliant()


def test_compute_diff_without_not_checked_is_compliant() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    diff = compute_diff(desired=desired, actual=actual)
    assert diff.not_checked == []
    assert diff.is_compliant()


def test_cleanup_raises_on_forbidden_classic_protection() -> None:
    rulesets = [desired_branch_protection_ruleset()]
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json",
            side_effect=_forbidden("repos/o/r/branches/develop/protection"),
        ),
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        pytest.raises(GitHubAPIError, match="403"),
    ):
        _cleanup_classic_branch_protection("o/r", rulesets)
    mock_write.assert_not_called()


def test_read_classic_protection_reraises_unrelated_error() -> None:
    err = GitHubAPIError(
        1,
        ("gh", "api", "repos/o/r/branches/develop/protection"),
        "",
        "gh: Server Error (HTTP 500)",
    )
    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=err),
        pytest.raises(GitHubAPIError),
    ):
        _read_classic_protection("o/r", "develop")


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


def test_normalize_rules_strips_dismissal_restriction() -> None:
    # GitHub's rulesets API injects ``dismissal_restriction`` as a server-side
    # default on the ``pull_request`` rule; the desired state never sets it, so
    # it must be stripped to avoid phantom drift (issue #2179).
    rules: list[object] = [
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 0,
                "dismissal_restriction": {"enabled": False, "allowed_actors": []},
                "allowed_merge_methods": ["merge", "squash", "rebase"],
            },
        },
    ]
    result = _normalize_rules(rules)
    assert len(result) == 1
    params = cast("dict[str, object]", result[0]["parameters"])
    assert "dismissal_restriction" not in params
    assert params["required_approving_review_count"] == 0
    assert params["allowed_merge_methods"] == ["merge", "squash", "rebase"]


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
    state = _state(config)
    assert state.publish is not None
    assert state.publish.release is False
    assert state.publish.docs is True


def test_compute_desired_state_publish_release_true() -> None:
    config = VergilConfig(
        project=_project(),
        dependencies={"vergil": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=_ci(),
        publish=PublishConfig(release=True, docs=True, install=None),
        container=ContainerConfig(env_prefixes=[], system_packages=[]),
        validation=ValidationConfig(container_command=DEFAULT_VALIDATION_COMMAND),
    )
    state = _state(config)
    assert state.publish.release is True
    assert state.publish.docs is True


def test_apply_repo_settings_propagates_unrelated_api_error() -> None:
    settings = desired_repo_settings()
    unrelated = GitHubAPIError(
        1,
        ("gh", "api", "repos/o/r"),
        '{"message":"Server Error","status":"500"}',
        "gh: Server Error (HTTP 500)",
    )
    with (
        patch(
            "vergil_tooling.lib.github_config.github.write_json",
            side_effect=unrelated,
        ),
        pytest.raises(GitHubAPIError),
    ):
        _apply_repo_settings("o/r", settings)


# ---------------------------------------------------------------------------
# Issue #1210: format_rules_delta — human-readable status check diffs
# ---------------------------------------------------------------------------


def _make_rules(checks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Build a minimal rules list containing required_status_checks."""
    return [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": checks,
            },
        },
    ]


def test_format_rules_delta_shows_extra_checks() -> None:
    expected = _make_rules(
        [
            {"context": "quality / common", "integration_id": 15368},
        ]
    )
    actual = _make_rules(
        [
            {"context": "quality / common", "integration_id": 15368},
            {"context": "CodeQL", "integration_id": 57789},
        ]
    )
    result = format_rules_delta(expected, actual)
    assert result is not None
    assert "extra (1)" in result
    assert "CodeQL (integration_id: 57789)" in result
    assert "missing (0)" in result


def test_format_rules_delta_shows_missing_checks() -> None:
    expected = _make_rules(
        [
            {"context": "quality / common", "integration_id": 15368},
            {"context": "security / trivy", "integration_id": 15368},
        ]
    )
    actual = _make_rules(
        [
            {"context": "quality / common", "integration_id": 15368},
        ]
    )
    result = format_rules_delta(expected, actual)
    assert result is not None
    assert "missing (1)" in result
    assert "security / trivy (integration_id: 15368)" in result
    assert "extra (0)" in result


def test_format_rules_delta_shows_both_extra_and_missing() -> None:
    expected = _make_rules(
        [
            {"context": "quality / common", "integration_id": 15368},
            {"context": "security / trivy", "integration_id": 15368},
        ]
    )
    actual = _make_rules(
        [
            {"context": "quality / common", "integration_id": 15368},
            {"context": "CodeQL", "integration_id": 57789},
        ]
    )
    result = format_rules_delta(expected, actual)
    assert result is not None
    assert "extra (1)" in result
    assert "CodeQL (integration_id: 57789)" in result
    assert "missing (1)" in result
    assert "security / trivy (integration_id: 15368)" in result


def test_format_rules_delta_returns_none_for_non_list() -> None:
    assert format_rules_delta("foo", "bar") is None


def test_format_rules_delta_returns_none_without_status_checks() -> None:
    expected = [{"type": "deletion"}]
    actual = [{"type": "deletion"}, {"type": "non_fast_forward"}]
    assert format_rules_delta(expected, actual) is None


def test_format_rules_delta_skips_non_dict_rules() -> None:
    expected: list[object] = ["not-a-dict", *_make_rules([{"context": "a", "integration_id": 1}])]
    actual = _make_rules([{"context": "a", "integration_id": 1}])
    result = format_rules_delta(expected, actual)
    assert result is not None
    assert "extra (0)" in result
    assert "missing (0)" in result


def test_format_rules_delta_skips_non_list_checks() -> None:
    expected: list[dict[str, object]] = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": "not-a-list",
            },
        },
    ]
    actual = _make_rules([{"context": "a", "integration_id": 1}])
    result = format_rules_delta(expected, actual)
    assert result is not None
    assert "missing (0)" in result


def test_format_rules_delta_skips_non_dict_params() -> None:
    expected: list[dict[str, object]] = [
        {"type": "required_status_checks", "parameters": "not-a-dict"},
    ]
    actual = _make_rules([{"context": "a", "integration_id": 1}])
    result = format_rules_delta(expected, actual)
    assert result is not None
    assert "missing (0)" in result


def test_format_rules_delta_identical_checks() -> None:
    checks: list[dict[str, object]] = [{"context": "quality / common", "integration_id": 15368}]
    result = format_rules_delta(_make_rules(checks), _make_rules(checks))
    assert result is not None
    assert "extra (0)" in result
    assert "missing (0)" in result


# ---------------------------------------------------------------------------
# Issue #1288: App mode bypass_actors skipping; epic vergil-project/.github#1064:
# the automation-branch ruleset and fatal bypass reads
# ---------------------------------------------------------------------------


def test_compute_desired_state_skip_bypass_nulls_bypass_actors() -> None:
    state = compute_desired_state(
        _vergil_config(),
        skip_bypass=True,
        automation_app_id=None,
    )
    for rs in state.rulesets:
        assert rs.bypass_actors is None


def test_compute_desired_state_without_skip_keeps_bypass_actors() -> None:
    state = _state(_vergil_config())
    tag_rs = next(rs for rs in state.rulesets if rs.name == "Tag protection")
    assert tag_rs.bypass_actors is not None
    assert {(a["actor_type"], a["actor_id"]) for a in tag_rs.bypass_actors} == {
        ("RepositoryRole", 5),
        ("Integration", _APP_ID),
    }


def test_compute_desired_state_requires_app_id_unless_skipping() -> None:
    with pytest.raises(ValueError, match="automation App's ID is required"):
        compute_desired_state(
            _vergil_config(),
            skip_bypass=False,
            automation_app_id=None,
        )


def test_compute_desired_state_includes_automation_ruleset() -> None:
    state = _state(_vergil_config())
    rs = next(r for r in state.rulesets if r.name == "Automation branches")
    assert rs.bypass_actors is not None
    assert {"actor_id": _APP_ID, "actor_type": "Integration", "bypass_mode": "always"} in (
        rs.bypass_actors
    )


def test_desired_settings_allow_auto_merge() -> None:
    assert desired_repo_settings().allow_auto_merge is True


def test_automation_ruleset_shape() -> None:
    rs = desired_automation_branch_ruleset(4242)
    assert rs == DesiredRuleset(
        name="Automation branches",
        target="branch",
        enforcement="active",
        ref_include=["refs/heads/automation/**"],
        bypass_actors=[
            {"actor_id": 4242, "actor_type": "Integration", "bypass_mode": "always"},
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        ],
        rules=[{"type": "creation"}, {"type": "update"}, {"type": "non_fast_forward"}],
    )
    assert {"type": "deletion"} not in rs.rules


def test_automation_ruleset_without_app_id_is_skipped() -> None:
    assert desired_automation_branch_ruleset(None).bypass_actors is None


def _bypass_ids(rs: DesiredRuleset) -> set[tuple[object, object]]:
    assert rs.bypass_actors is not None
    return {(a["actor_type"], a["actor_id"]) for a in rs.bypass_actors}


def test_main_update_ruleset_restricts_updates() -> None:
    rs = desired_main_update_ruleset(123, app_merges_releases=False)
    assert rs.name == "Main updates"
    assert rs.target == "branch"
    assert rs.enforcement == "active"
    assert rs.ref_include == ["refs/heads/main"]
    assert [r["type"] for r in rs.rules] == ["update"]


def test_main_update_bypass_is_admin_and_optional_app() -> None:
    assert _bypass_ids(desired_main_update_ruleset(123, app_merges_releases=False)) == {
        ("RepositoryRole", 5)
    }
    assert _bypass_ids(desired_main_update_ruleset(123, app_merges_releases=True)) == {
        ("RepositoryRole", 5),
        ("Integration", 123),
    }


def test_audit_marker_ruleset_bypass_is_admin_and_app_only() -> None:
    rs = desired_audit_marker_ruleset(123)
    assert rs.name == "Agent audit markers"
    assert rs.target == "branch"
    assert rs.enforcement == "active"
    assert rs.ref_include == ["refs/heads/vergil/agent-audit/*"]
    assert [r["type"] for r in rs.rules] == ["update", "creation", "deletion"]
    assert _bypass_ids(rs) == {("RepositoryRole", 5), ("Integration", 123)}


def test_new_rulesets_leave_bypass_unmanaged_without_app_id() -> None:
    assert desired_main_update_ruleset(None, app_merges_releases=False).bypass_actors is None
    assert desired_audit_marker_ruleset(None).bypass_actors is None


def test_compute_desired_state_main_updates_bypass_is_the_admin_only() -> None:
    # The human merges every release PR into main (vrg-release refuses every
    # agent identity), so the App is not a bypass actor of "Main updates".
    state = _state(_vergil_config())
    rs = next(r for r in state.rulesets if r.name == "Main updates")
    assert _bypass_ids(rs) == {("RepositoryRole", 5)}


def test_compute_desired_state_audit_markers_bypass_is_admin_and_app() -> None:
    state = _state(_vergil_config())
    rs = next(r for r in state.rulesets if r.name == "Agent audit markers")
    assert _bypass_ids(rs) == {("RepositoryRole", 5), ("Integration", _APP_ID)}


def test_diff_skips_bypass_actors_when_desired_is_none() -> None:
    desired = compute_desired_state(
        _vergil_config(),
        skip_bypass=True,
        automation_app_id=None,
    )
    actual = _state(_vergil_config())
    diff = compute_diff(desired=desired, actual=actual)
    assert not any(d.field.endswith(".bypass_actors") for d in diff.items)
    assert any(s.endswith(".bypass_actors") for s in diff.skipped)


def test_diff_reports_bypass_actors_mismatch_without_app_mode() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.rulesets[1].bypass_actors = []
    diff = compute_diff(desired=desired, actual=actual)
    assert any(d.field == "rulesets.Tag protection.bypass_actors" for d in diff.items)


def test_diff_ignores_bypass_actor_order() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    live = next(r for r in actual.rulesets if r.name == "Automation branches")
    assert live.bypass_actors is not None
    live.bypass_actors = list(reversed(live.bypass_actors))
    diff = compute_diff(desired=desired, actual=actual)
    assert diff.is_compliant()


def test_diff_reports_wrong_automation_app_id() -> None:
    desired = _state(_vergil_config())
    actual = compute_desired_state(
        _vergil_config(),
        skip_bypass=False,
        automation_app_id=_APP_ID + 1,
    )
    diff = compute_diff(desired=desired, actual=actual)
    assert [d.field for d in diff.items] == [
        "rulesets.Tag protection.bypass_actors",
        "rulesets.Automation branches.bypass_actors",
        "rulesets.Agent audit markers.bypass_actors",
    ]


def _with_emptied_bypass(*names: str) -> tuple[Any, Any]:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    for rs in actual.rulesets:
        if rs.name in names:
            rs.bypass_actors = []
    return desired, actual


def test_empty_bypass_read_is_unreadable_under_automation() -> None:
    desired, actual = _with_emptied_bypass("Tag protection", "Automation branches")
    diff = compute_diff(desired=desired, actual=actual, automation=True)
    assert not any(d.field.endswith(".bypass_actors") for d in diff.items)
    assert diff.not_checked == [
        BypassUnreadable(field="rulesets.Tag protection.bypass_actors"),
        BypassUnreadable(field="rulesets.Automation branches.bypass_actors"),
    ]
    assert all(isinstance(nc, NotChecked) for nc in diff.not_checked)
    assert diff.not_checked[0].permission == "administration=write"
    assert not diff.has_drift()
    assert not diff.is_compliant()


def test_empty_bypass_read_keeps_other_drift_under_automation() -> None:
    desired, actual = _with_emptied_bypass("Tag protection")
    actual.repo_settings.allow_auto_merge = False
    diff = compute_diff(desired=desired, actual=actual, automation=True)
    assert [d.field for d in diff.items] == ["repo_settings.allow_auto_merge"]
    assert [nc.field for nc in diff.not_checked] == ["rulesets.Tag protection.bypass_actors"]


def test_empty_bypass_where_desired_is_empty_is_not_unreadable() -> None:
    # Branch protection desires no bypass actor, so an empty read is the truth.
    desired, actual = _with_emptied_bypass("Branch protection")
    diff = compute_diff(desired=desired, actual=actual, automation=True)
    assert diff.is_compliant()


def test_missing_ruleset_under_automation_is_drift_not_unreadable() -> None:
    desired = _state(_vergil_config())
    actual = _state(_vergil_config())
    actual.rulesets = [r for r in actual.rulesets if r.name != "Automation branches"]
    diff = compute_diff(desired=desired, actual=actual, automation=True)
    assert diff.not_checked == []
    assert [(d.field, d.actual) for d in diff.items] == [
        ("rulesets.Automation branches", "missing")
    ]


def test_empty_bypass_read_skipped_for_non_automation_app() -> None:
    desired = compute_desired_state(
        _vergil_config(),
        skip_bypass=True,
        automation_app_id=None,
    )
    _, actual = _with_emptied_bypass("Tag protection", "Automation branches")
    diff = compute_diff(desired=desired, actual=actual)
    assert diff.not_checked == []
    assert not any(d.field.endswith(".bypass_actors") for d in diff.items)
    assert "rulesets.Tag protection.bypass_actors" in diff.skipped
    assert "rulesets.Automation branches.bypass_actors" in diff.skipped


def test_human_run_converges_emptied_bypass_list() -> None:
    desired, actual = _with_emptied_bypass("Tag protection")
    diff = compute_diff(desired=desired, actual=actual)
    assert diff.not_checked == []
    items = [d for d in diff.items if d.field == "rulesets.Tag protection.bypass_actors"]
    assert len(items) == 1
    assert items[0].actual == []
    tag = next(r for r in desired.rulesets if r.name == "Tag protection")
    assert items[0].expected == tag.bypass_actors
    with patch("vergil_tooling.lib.github_config.github") as mock_gh:
        mock_gh.read_json_paginated.return_value = [{"id": 7, "name": "Tag protection"}]
        _apply_rulesets("o/r", [tag])
    mock_gh.write_json.assert_called_once()
    method, endpoint, body = mock_gh.write_json.call_args.args
    assert (method, endpoint) == ("PUT", "repos/o/r/rulesets/7")
    assert body["bypass_actors"] == tag.bypass_actors


def test_ruleset_body_refuses_skipped_bypass_actors() -> None:
    ruleset = DesiredRuleset(
        name="Test",
        target="branch",
        enforcement="active",
        ref_include=[],
        bypass_actors=None,
        rules=[],
    )
    with pytest.raises(ValueError, match="bypass_actors were skipped"):
        _ruleset_body(ruleset)


# ---------------------------------------------------------------------------
# Evidence-gate derivation (issue #2289)
# ---------------------------------------------------------------------------


class TestClassifyEvidenceGate:
    def test_quality_prefix(self) -> None:
        assert classify_evidence_gate("quality / lint / 3.14") == "quality"

    def test_quality_common_prefix(self) -> None:
        assert classify_evidence_gate("quality / common") == "quality"

    def test_test_prefix(self) -> None:
        assert classify_evidence_gate("test / unit / 3.14") == "test"

    def test_audit_prefix(self) -> None:
        assert classify_evidence_gate("audit / dependencies / 3.14") == "audit"

    def test_security_prefixed(self) -> None:
        assert classify_evidence_gate("security / codeql") == "security"

    def test_security_ghas_literal(self) -> None:
        assert classify_evidence_gate("CodeQL") == "security"
        assert classify_evidence_gate("Trivy") == "security"
        assert classify_evidence_gate("Semgrep OSS") == "security"

    def test_package_prefix(self) -> None:
        assert classify_evidence_gate("package / evidence") == "package"

    def test_images_prefix(self) -> None:
        assert classify_evidence_gate("images / evidence") == "images"

    def test_version_is_non_evidence(self) -> None:
        assert classify_evidence_gate("version / version-bump") is None

    def test_unknown_is_non_evidence(self) -> None:
        assert classify_evidence_gate("something / else") is None


class TestRequiredEvidenceGates:
    def test_groups_checks_by_gate(self) -> None:
        gates = required_evidence_gates(_project(), _ci())
        names = {g.name for g in gates}
        assert names == {"security", "test", "audit", "quality"}

    def test_gate_carries_its_classified_checks(self) -> None:
        gates = {g.name: g for g in required_evidence_gates(_project(), _ci())}
        assert "test / evidence" in gates["test"].checks
        assert "audit / evidence" in gates["audit"].checks
        assert gates["quality"].checks == ("quality / evidence",)
        assert "security / trivy" in gates["security"].checks

    def test_security_gate_carries_codeql(self) -> None:
        gates = {g.name: g for g in required_evidence_gates(_project(), _ci())}
        assert "CodeQL" in gates["security"].checks
        assert "security / codeql" in gates["security"].checks

    def test_package_gate_required_with_package_section(self) -> None:
        gates = {g.name: g for g in required_evidence_gates(_project(), _ci(), package=True)}
        assert gates["package"].checks == ("package / evidence",)

    def test_package_gate_absent_without_package_section(self) -> None:
        gates = {g.name for g in required_evidence_gates(_project(), _ci())}
        assert "package" not in gates

    def test_images_gate_required_with_ci_images(self) -> None:
        gates = {g.name: g for g in required_evidence_gates(_project(), _ci(images=True))}
        assert gates["images"].checks == ("images / evidence",)

    def test_images_gate_absent_by_default(self) -> None:
        gates = {g.name for g in required_evidence_gates(_project(), _ci(), package=True)}
        assert "images" not in gates

    def test_images_gate_ordered_last(self) -> None:
        gates = required_evidence_gates(_project(), _ci(images=True), package=True)
        assert [g.name for g in gates] == [
            "security",
            "test",
            "audit",
            "quality",
            "package",
            "images",
        ]

    def test_package_gate_ordered_after_quality(self) -> None:
        gates = required_evidence_gates(_project(), _ci(), package=True)
        assert [g.name for g in gates] == ["security", "test", "audit", "quality", "package"]

    def test_version_check_is_never_an_evidence_gate(self) -> None:
        gates = {g.name for g in required_evidence_gates(_project(), _ci())}
        assert "version" not in gates

    def test_gate_absent_when_no_required_check(self) -> None:
        # A project with no primary language emits neither ``test / unit`` nor
        # ``audit / dependencies`` checks, so those evidence gates are absent —
        # the evidence-required set tracks the enforced set exactly.
        gates = {
            g.name
            for g in required_evidence_gates(_project(language=None), _ci(versions=["latest"]))
        }
        assert "test" not in gates
        assert "audit" not in gates

    def test_evidence_gate_carries_name_and_checks(self) -> None:
        gate = EvidenceGate(name="test", checks=("test / unit / 3.14",))
        assert gate.name == "test"
        assert gate.checks == ("test / unit / 3.14",)


# ---------------------------------------------------------------------------
# The automation environment (epic vergil-project/.github#1384, Task 3b)
# ---------------------------------------------------------------------------

_ENV = "repos/o/r/environments/automation"
_ENV_POLICIES = f"{_ENV}/deployment-branch-policies"


def _not_found(endpoint: str) -> GitHubAPIError:
    return GitHubAPIError(1, ("gh", "api", endpoint), "", "gh: Not Found (HTTP 404)")


def _custom_env_json(**extra: object) -> dict[str, object]:
    return {
        "name": "automation",
        "protection_rules": [{"id": 1, "node_id": "n", "type": "branch_policy"}],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
        **extra,
    }


def _policy(name: str, kind: str = "branch", policy_id: int = 1) -> dict[str, object]:
    return {"id": policy_id, "node_id": "n", "name": name, "type": kind}


def _fetch_environment_with(
    env: object, policies: list[object] | Exception | None = None
) -> FetchResult:
    """Run ``fetch_actual_state`` with the automation environment mocked."""

    def read_json(*args: str) -> object:
        endpoint = args[1]
        if endpoint == "repos/o/r":
            return {**_REPO_SETTINGS_JSON, "security_and_analysis": {}}
        if endpoint == "repos/o/r/rulesets":
            return []
        if endpoint == _ENV:
            if isinstance(env, Exception):
                raise env
            return env
        if endpoint == _ENV_POLICIES:
            if isinstance(policies, Exception):
                raise policies
            return {"total_count": len(policies or []), "branch_policies": policies or []}
        if endpoint.endswith("/protection"):
            raise _not_found(endpoint)
        return {}

    with (
        patch("vergil_tooling.lib.github_config.github.read_json", side_effect=read_json),
        patch("vergil_tooling.lib.github_config._fetch_vulnerability_alerts", return_value=True),
    ):
        return fetch_actual_state("o/r")


def _env_items(diff: ConfigDiff) -> dict[str, DiffItem]:
    return {i.field: i for i in diff.items if i.field.startswith("environments.")}


def test_automation_environment_allows_only_develop_and_main() -> None:
    env = desired_automation_environment()
    assert env.name == "automation"
    assert env.deployment_branch_policy == "custom"
    assert set(env.custom_branches) == {"develop", "main"}
    assert env.custom_tags == []
    assert env.reviewers == []
    assert env.wait_timer == 0


def test_desired_state_carries_the_automation_environment() -> None:
    assert _state(_vergil_config()).environments == [desired_automation_environment()]
    # A docs-single-branch repo gets the same policy: one naming a branch the
    # repo does not have admits nothing.
    docs_only = _vergil_config(release_model="docs-single-branch")
    assert _state(docs_only).environments == [desired_automation_environment()]


def test_environment_drift_is_reported() -> None:
    result = _fetch_environment_with(_custom_env_json(), [_policy("*")])
    diff = compute_diff(desired=_state(_vergil_config()), actual=result.state)
    items = _env_items(diff)
    assert items["environments.automation.custom_branches"].expected == ["develop", "main"]
    assert items["environments.automation.custom_branches"].actual == ["*"]
    assert diff.has_drift()


def test_converged_environment_is_compliant() -> None:
    result = _fetch_environment_with(
        _custom_env_json(), [_policy("main", policy_id=2), _policy("develop")]
    )
    assert result.state.environments == [desired_automation_environment()]
    diff = compute_diff(desired=_state(_vergil_config()), actual=result.state)
    assert _env_items(diff) == {}


def test_missing_environment_is_drift() -> None:
    result = _fetch_environment_with(_not_found(_ENV))
    assert result.state.environments == []
    assert result.not_checked == []
    diff = compute_diff(desired=_state(_vergil_config()), actual=result.state)
    item = _env_items(diff)["environments.automation"]
    assert (item.expected, item.actual) == ("present", "missing")


def test_environment_allowing_all_branches_is_drift() -> None:
    # GitHub returns a null policy for "all branches"; no policies are listed.
    result = _fetch_environment_with(_custom_env_json(deployment_branch_policy=None))
    diff = compute_diff(desired=_state(_vergil_config()), actual=result.state)
    items = _env_items(diff)
    assert items["environments.automation.deployment_branch_policy"].actual == "all"
    assert items["environments.automation.custom_branches"].actual == []


def test_environment_protected_branches_policy_is_drift() -> None:
    result = _fetch_environment_with(
        _custom_env_json(
            deployment_branch_policy={"protected_branches": True, "custom_branch_policies": False}
        )
    )
    diff = compute_diff(desired=_state(_vergil_config()), actual=result.state)
    item = _env_items(diff)["environments.automation.deployment_branch_policy"]
    assert item.actual == "protected"


def test_environment_tag_policy_reviewers_and_wait_timer_are_drift() -> None:
    env = _custom_env_json(
        protection_rules=[
            {"id": 1, "type": "wait_timer", "wait_timer": 30},
            {
                "id": 2,
                "type": "required_reviewers",
                "reviewers": [
                    {"type": "User", "reviewer": {"login": "octocat", "id": 9}},
                    {"type": "Team", "reviewer": {"slug": "admins", "id": 8}},
                ],
            },
        ]
    )
    policies: list[object] = [
        _policy("develop"),
        _policy("main", policy_id=2),
        _policy("v*", "tag", 3),
    ]
    result = _fetch_environment_with(env, policies)
    diff = compute_diff(desired=_state(_vergil_config()), actual=result.state)
    items = _env_items(diff)
    assert "environments.automation.custom_branches" not in items
    assert items["environments.automation.custom_tags"].actual == ["v*"]
    assert items["environments.automation.reviewers"].actual == ["Team:admins", "User:octocat"]
    assert items["environments.automation.wait_timer"].actual == 30


def test_unreadable_environment_is_not_checked() -> None:
    result = _fetch_environment_with(_forbidden(_ENV))
    assert NotChecked(field="environments.automation", permission="actions=read") in (
        result.not_checked
    )
    diff = compute_diff(
        desired=_state(_vergil_config()), actual=result.state, not_checked=result.not_checked
    )
    assert _env_items(diff) == {}
    assert not diff.is_compliant()


def test_unreadable_branch_policies_are_not_checked() -> None:
    result = _fetch_environment_with(_custom_env_json(), _forbidden(_ENV_POLICIES))
    fields = {nc.field for nc in result.not_checked if nc.field.startswith("environments.")}
    assert fields == {
        "environments.automation.custom_branches",
        "environments.automation.custom_tags",
    }
    diff = compute_diff(
        desired=_state(_vergil_config()), actual=result.state, not_checked=result.not_checked
    )
    assert _env_items(diff) == {}
    assert not diff.is_compliant()


@pytest.mark.parametrize(
    "error",
    [
        GitHubAPIError(1, ("gh", "api", _ENV), "", "gh: API rate limit exceeded (HTTP 403)"),
        GitHubAPIError(1, ("gh", "api", _ENV), "", "gh: Server Error (HTTP 500)"),
    ],
)
def test_environment_read_failure_propagates(error: GitHubAPIError) -> None:
    with pytest.raises(GitHubAPIError):
        _fetch_environment_with(error)


@pytest.mark.parametrize(
    ("env", "policies"),
    [
        (["not", "an", "object"], None),
        (_custom_env_json(deployment_branch_policy="custom"), None),
        (
            _custom_env_json(
                deployment_branch_policy={
                    "protected_branches": True,
                    "custom_branch_policies": True,
                }
            ),
            None,
        ),
        (_custom_env_json(protection_rules={"type": "wait_timer"}), None),
        (_custom_env_json(protection_rules=["wait_timer"]), None),
        (_custom_env_json(protection_rules=[{"type": "custom_app_gate"}]), None),
        (_custom_env_json(protection_rules=[{"type": "wait_timer", "wait_timer": "30"}]), None),
        (_custom_env_json(protection_rules=[{"type": "wait_timer", "wait_timer": True}]), None),
        (_custom_env_json(protection_rules=[{"type": "required_reviewers"}]), None),
        (
            _custom_env_json(
                protection_rules=[
                    {"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {}}]}
                ]
            ),
            None,
        ),
        (_custom_env_json(), [{"name": "develop", "type": "ref"}]),
        (_custom_env_json(), [{"name": 7, "type": "branch"}]),
    ],
)
def test_malformed_environment_raises(env: object, policies: list[object] | None) -> None:
    # A malformed answer fails closed, never reads as a converged environment.
    with pytest.raises((RuntimeError, ValueError)):
        _fetch_environment_with(env, policies)


def test_apply_environment_creates_and_converges_policies() -> None:
    live: list[object] = [_policy("*", policy_id=11), _policy("develop", policy_id=12)]
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json_paginated",
            return_value=live,
        ) as mock_list,
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        patch("vergil_tooling.lib.github_config.github.delete") as mock_del,
    ):
        _apply_environments("o/r", [desired_automation_environment()])
    assert mock_write.call_args_list[0].args == (
        "PUT",
        _ENV,
        {
            "wait_timer": 0,
            "reviewers": [],
            "deployment_branch_policy": {
                "protected_branches": False,
                "custom_branch_policies": True,
            },
        },
    )
    mock_list.assert_called_once_with(_ENV_POLICIES, "branch_policies")
    assert [c.args for c in mock_write.call_args_list[1:]] == [
        ("POST", _ENV_POLICIES, {"name": "main", "type": "branch"}),
    ]
    mock_del.assert_called_once_with(f"{_ENV_POLICIES}/11")


def test_apply_environment_creates_before_it_deletes() -> None:
    # The missing policies are written before the stray ones go, so the
    # policy set never passes through empty.
    calls: list[str] = []
    with (
        patch(
            "vergil_tooling.lib.github_config.github.read_json_paginated",
            return_value=[_policy("*", policy_id=11)],
        ),
        patch(
            "vergil_tooling.lib.github_config.github.write_json",
            side_effect=lambda method, *_: calls.append(method),
        ),
        patch(
            "vergil_tooling.lib.github_config.github.delete",
            side_effect=lambda *_: calls.append("DELETE"),
        ),
    ):
        _apply_environments("o/r", [desired_automation_environment()])
    assert calls == ["PUT", "POST", "POST", "DELETE"]


def test_branch_policies_read_failure_propagates() -> None:
    error = GitHubAPIError(1, ("gh", "api", _ENV_POLICIES), "", "gh: Server Error (HTTP 500)")
    with pytest.raises(GitHubAPIError, match="500"):
        _fetch_environment_with(_custom_env_json(), error)


@pytest.mark.parametrize(
    ("policy", "body"),
    [
        ("all", None),
        ("protected", {"protected_branches": True, "custom_branch_policies": False}),
    ],
)
def test_apply_environment_without_custom_policies_lists_none(
    policy: str, body: dict[str, bool] | None
) -> None:
    env = dataclasses.replace(
        desired_automation_environment(), deployment_branch_policy=policy, custom_branches=[]
    )
    with (
        patch("vergil_tooling.lib.github_config.github.read_json_paginated") as mock_list,
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
    ):
        _apply_environments("o/r", [env])
    mock_write.assert_called_once_with(
        "PUT", _ENV, {"wait_timer": 0, "reviewers": [], "deployment_branch_policy": body}
    )
    mock_list.assert_not_called()


def test_apply_environment_refuses_unknown_policy_before_writing() -> None:
    good = desired_automation_environment()
    bad = dataclasses.replace(good, name="other", deployment_branch_policy="selected")
    with (
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        pytest.raises(ValueError, match="unknown deployment branch policy"),
    ):
        _apply_environments("o/r", [good, bad])
    mock_write.assert_not_called()


def test_apply_environment_refuses_reviewers_it_cannot_name() -> None:
    env = dataclasses.replace(desired_automation_environment(), reviewers=["User:octocat"])
    with (
        patch("vergil_tooling.lib.github_config.github.write_json") as mock_write,
        pytest.raises(ValueError, match="reviewers"),
    ):
        _apply_environments("o/r", [env])
    mock_write.assert_not_called()


def test_apply_desired_state_applies_environments() -> None:
    state = _state(_vergil_config())
    with (
        patch("vergil_tooling.lib.github_config._apply_repo_settings"),
        patch("vergil_tooling.lib.github_config._apply_security_settings"),
        patch("vergil_tooling.lib.github_config._apply_actions_permissions"),
        patch("vergil_tooling.lib.github_config._apply_rulesets"),
        patch("vergil_tooling.lib.github_config._apply_environments") as mock_envs,
        patch(
            "vergil_tooling.lib.github_config._cleanup_classic_branch_protection",
            return_value=[],
        ),
    ):
        apply_desired_state("o/r", state)
    mock_envs.assert_called_once_with("o/r", state.environments)
