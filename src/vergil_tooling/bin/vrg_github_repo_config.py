"""Audit, diff, and apply repository configuration for managed repos.

Combines local filesystem checks (vergil.toml, CLAUDE.md,
.claude/settings.json, .claude/hooks/) with GitHub API configuration
auditing.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

from vergil_tooling.lib import github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import VergilConfig, _parse_raw_config
from vergil_tooling.lib.github_config import (
    ClassicProtectionReport,
    ConfigDiff,
    DiffItem,
    UndeclaredParam,
    apply_desired_state,
    assert_ci_gates_producible,
    compute_desired_state,
    compute_diff,
    fetch_actual_state,
    format_rules_delta,
    ghas_available,
)
from vergil_tooling.lib.output import emit_error
from vergil_tooling.lib.remote_config import decode_contents as _decode_contents
from vergil_tooling.lib.remote_config import fetch_config_from_ref as _fetch_config_from_ref
from vergil_tooling.lib.remote_config import is_not_found as _is_not_found
from vergil_tooling.lib.repo_config import audit_local_config
from vergil_tooling.lib.versions import VersionsError


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    local_compliant = True
    if args.repo and not _cwd_matches_repo(args.repo):
        print(
            f"\n  WARNING: --repo {args.repo} does not match the current directory's "
            f"git remote.\n"
            f"  Local checks skipped — cd into the repo's checkout to audit local files.\n",
            file=sys.stderr,
        )
        print("  local: skipped (not in local checkout)")
    else:
        local_diff = audit_local_config(Path.cwd())
        _print_local_diff(local_diff)
        local_compliant = local_diff.is_compliant()

    repo = _resolve_repo(args)
    # Every GitHub call below targets *repo*, so mint the App token for its
    # owner rather than the cwd org (#3220). Under target_org a missing
    # installation for that owner raises NoInstallationError instead of
    # falling back to ambient auth.
    try:
        with github.target_org(repo.split("/", 1)[0]):
            return _audit_and_apply(args, repo, local_compliant=local_compliant)
    except (github.NoInstallationError, github.AppAuthError) as exc:
        # The audit could not complete: exit 2, never the "non-compliant"
        # exit 1 that cli_main would otherwise give an auth failure (#1691).
        if isinstance(exc, github.NoInstallationError):
            emit_error(f"Could not audit {repo}: {github.no_installation_message(exc)}")
        else:
            emit_error(f"Could not audit {repo}: {github.app_auth_message(exc)}")
        return 2


def _audit_and_apply(args: argparse.Namespace, repo: str, *, local_compliant: bool) -> int:
    """Audit *repo*'s GitHub config and, for ``apply``, converge it.

    Runs inside the caller's ``github.target_org`` scope for *repo*'s owner.
    """
    all_compliant = local_compliant
    # Exit codes are a contract for callers (e.g. vrg-release): 0 = compliant,
    # 1 = genuinely non-compliant, 2 = the audit could not complete. Config
    # resolution and the GitHub state fetch can fail operationally (missing
    # vergil.toml, a [ci].versions pin outside the published version window,
    # HTTP 403 reading actions/permissions under an App token); those are exit
    # 2 with a clean diagnostic, never a false "non-compliant" verdict or a raw
    # traceback (#1691, #3670).
    try:
        config = _load_local_config(args.config) if args.config else _resolve_config(repo)
    except github.GitHubAPIError as exc:
        emit_error(f"Could not resolve vergil.toml for {repo}: {exc}")
        return 2
    except RuntimeError as exc:
        emit_error(str(exc))
        return 2

    try:
        github_diff = _audit_repo(repo, config)
    except VersionsError as exc:
        # The desired state carries the CI versions resolved from the published
        # window; the error already says what to fix, so report it on one line.
        emit_error(f"Could not resolve the CI versions for {repo}: {exc}")
        return 2
    except (github.GitHubAPIError, RuntimeError) as exc:
        emit_error(f"Could not audit {repo}: {exc}")
        return 2
    _print_diff(repo, github_diff)
    if not github_diff.is_compliant():
        all_compliant = False

    if args.command == "audit":
        return 0 if all_compliant else 1
    if args.command == "diff":
        return 0

    if github_diff.is_compliant():
        print("GitHub config compliant, nothing to apply.")
    elif github_diff.items:
        print(f"  Applying to {repo}...")
        try:
            reports = _apply_repo(repo, config)
        except VersionsError as exc:
            emit_error(f"Could not resolve the CI versions for {repo}: {exc}")
            return 2
        except RuntimeError as exc:
            emit_error(str(exc))
            return 1
        print(f"  {repo}: applied")
        for report in reports:
            preserved = ", ".join(report.preserved_settings) or "none"
            print(
                f"  {repo}: classic protection on {report.branch} — removed stale CI "
                f"contexts [{', '.join(report.removed_contexts)}]; preserved [{preserved}]"
            )

    if github_diff.undeclared:
        # apply cannot converge an undeclared parameter: the PUT body omits
        # the key, so GitHub re-applies its server-side default and the repo
        # stays non-compliant until the spec decides on/off (#3548).
        print(
            f"  {repo}: apply cannot resolve {len(github_diff.undeclared)} undeclared "
            f"ruleset parameter(s); declare each on or off in the desired spec, or "
            f"add it to _STRIP_RULE_PARAMS, then re-run."
        )
        return 1

    if not local_compliant:
        return 1

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enforce canonical repository configuration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("audit", "diff", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument(
            "--repo",
            help="Single repo (OWNER/REPO); defaults to current git remote",
        )
        sp.add_argument(
            "--config",
            help="Local path to vergil.toml (overrides remote fetch)",
        )

    return parser.parse_args(argv)


def _resolve_repo(args: argparse.Namespace) -> str:
    """Return the repo to operate on."""
    if args.repo:
        return str(args.repo)
    return github.current_repo()


def _load_local_config(path: str) -> VergilConfig:
    """Load and parse vergil.toml from a local file path."""
    with Path(path).open("rb") as f:
        raw = tomllib.load(f)
    return _parse_raw_config(raw, source=path)


# Canonical config branches, in resolution order: the released/stable branch
# first, then the integration branch as a bootstrap fallback.
_DEFAULT_BRANCH = "develop"


_CI_WORKFLOW_PATH = ".github/workflows/ci.yml"


def _fetch_ci_workflow(repo: str, ref: str) -> str:
    """Fetch the committed ``.github/workflows/ci.yml`` of ``repo`` at ``ref``.

    An absent ci.yml (HTTP 404) is a hard error, never a skip: the ``CI gates``
    ruleset always requires status checks, and with no ci.yml nothing posts them
    on a pull_request, so applying the ruleset would block every merge — the
    exact failure the producibility guard exists to prevent (issue #2788).
    Non-404 API errors propagate unchanged.
    """
    try:
        content_data = github.read_json(
            "api",
            f"repos/{repo}/contents/{_CI_WORKFLOW_PATH}?ref={ref}",
        )
    except github.GitHubAPIError as exc:
        if _is_not_found(exc):
            msg = (
                f"Refusing to apply the CI-gates ruleset to {repo}: "
                f"{_CI_WORKFLOW_PATH} is absent from the '{ref}' branch, so no "
                f"required status check could ever report on a pull_request and "
                f"every merge would be blocked. Commit a ci.yml to '{ref}' "
                f"(vrg-github-repo-init generates the canonical one) before applying."
            )
            raise RuntimeError(msg) from exc
        raise
    return _decode_contents(repo, ref, _CI_WORKFLOW_PATH, content_data)


def _load_cwd_config() -> VergilConfig | None:
    """Load vergil.toml from the current working directory, or None if absent."""
    path = Path.cwd() / "vergil.toml"
    if not path.exists():
        return None
    return _load_local_config(str(path))


def _resolve_config(repo: str) -> VergilConfig:
    """Resolve the canonical vergil.toml for ``repo`` from the default branch.

    Resolution order: the ``develop`` branch (the fleet default branch), then
    the local working-directory copy when the cwd is a checkout of ``repo``.
    Config tracks the **default branch**: everything else GitHub does off a
    repo — workflows above all — runs from the default branch, so a config
    change merged to ``develop`` must apply immediately, the same paradigm.

    ``main`` is deliberately NOT consulted. Under GitFlow, content reaches
    ``main`` only by way of ``develop``, so ``main`` can never hold a
    vergil.toml that ``develop`` lacks — a ``main`` fallback could only ever
    return the same file or a staler one, never a uniquely-available one. And
    reading ``main`` *first* was the original bug: it silently ignored a
    merged-to-develop change until a release carried it to ``main``, and
    ``main`` may not exist at all in a new or unreleased repo
    (vergil-tooling#2530).

    The local copy is the final bootstrap fallback — run ``apply`` from the
    repo root before the file has landed on ``develop`` — safe under the trust
    model: agents may *write* vergil.toml, but only a human runs ``apply`` and
    owns that action, so the local copy can never be applied without human
    oversight. The fallback is gated on the cwd matching the target repo so a
    ``--repo other/repo`` run can never apply the current directory's unrelated
    config.
    """
    config = _fetch_config_from_ref(repo, _DEFAULT_BRANCH)
    if config is not None:
        return config
    cwd_is_repo = _cwd_matches_repo(repo)
    if cwd_is_repo:
        local = _load_cwd_config()
        if local is not None:
            return local
    locations = "the 'develop' branch"
    if cwd_is_repo:
        locations += " and the current directory"
    msg = (
        f"vergil.toml could not be resolved for {repo}: it is absent from "
        f"{locations}. Confirm the file exists on a canonical branch or in this "
        f"checkout, that the repo exists, and that your credentials have access. "
        f"To apply from a specific file, pass --config <path/to/vergil.toml>."
    )
    raise RuntimeError(msg)


def _audit_repo(repo: str, config: VergilConfig) -> ConfigDiff:
    """Compute diff between desired and actual GitHub state for a repo."""
    result = fetch_actual_state(repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(
        config, visibility=result.visibility, is_org=is_org, app_mode=github.is_app_mode()
    )
    return compute_diff(desired=desired, actual=result.state)


def _format_item(item: DiffItem) -> str:
    if item.field.startswith("rulesets.") and item.field.endswith(".rules"):
        delta = format_rules_delta(item.expected, item.actual)
        if delta is not None:
            indented = "\n".join(f"      {line}" for line in delta.splitlines())
            return f"    {item.field}:\n{indented}"
    return f"    {item.field}: expected={item.expected!r}, actual={item.actual!r}"


def _format_undeclared(param: UndeclaredParam) -> str:
    """Render an undeclared ruleset parameter as an actionable line (#3548)."""
    return (
        f"    UNDECLARED PARAMETER: rulesets.{param.ruleset} rule {param.rule_type!r} "
        f"has {param.key!r} = {param.value!r} live, but the desired spec does not "
        f"declare it (likely a new GitHub default). Declare it on or off in the "
        f"desired ruleset (lib/github_config.py), or add it to _STRIP_RULE_PARAMS "
        f"if it should be ignored."
    )


def _print_local_diff(diff: ConfigDiff) -> None:
    """Print local config audit results."""
    if diff.is_compliant():
        print("  local: compliant")
    else:
        print(f"  local: NON-COMPLIANT ({len(diff.items)} issues)")
        for item in diff.items:
            print(_format_item(item))
    _print_warnings(diff)


def _print_warnings(diff: ConfigDiff) -> None:
    """Print advisory warnings (non-compliance-affecting) for a diff."""
    for warning in diff.warnings:
        print(f"    WARNING: {warning}")


def _print_diff(repo: str, diff: ConfigDiff) -> None:
    """Print GitHub config diff results for a repo."""
    if diff.is_compliant():
        print(f"  {repo}: compliant")
    else:
        print(f"  {repo}: NON-COMPLIANT ({len(diff.items) + len(diff.undeclared)} issues)")
        for item in diff.items:
            print(_format_item(item))
        for param in diff.undeclared:
            print(_format_undeclared(param))
    for field_name in diff.skipped:
        if field_name.startswith("security."):
            print(
                f"    {field_name}: skipped (requires GitHub Advanced Security for private repos)"
            )
        elif field_name.endswith(".bypass_actors"):
            print(f"    {field_name}: skipped (not visible with GitHub App credentials)")
    _print_warnings(diff)


def _apply_repo(repo: str, config: VergilConfig) -> list[ClassicProtectionReport]:
    """Apply desired state to a repo. Returns the classic-protection cleanup report.

    Before anything is written, the desired ``CI gates`` ruleset is
    cross-checked against the repo's committed ci.yml on the default branch —
    the same producibility guard repo-init runs — so a routine sync can never
    (re)apply a required status check no pull_request job emits (issue #2788).
    """
    result = fetch_actual_state(repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(config, visibility=result.visibility, is_org=is_org)
    ci_yaml = _fetch_ci_workflow(repo, _DEFAULT_BRANCH)
    assert_ci_gates_producible(
        desired,
        ci_yaml,
        ghas=ghas_available(config, visibility=result.visibility),
        source=f"{repo}@{_DEFAULT_BRANCH}:{_CI_WORKFLOW_PATH}",
    )
    return apply_desired_state(repo, desired)


def _cwd_matches_repo(repo: str) -> bool:
    """Check whether CWD's git origin matches the target repo."""
    try:
        cwd_repo = github.current_repo()
    except Exception:
        return False
    return cwd_repo == repo


if __name__ == "__main__":
    sys.exit(main())
