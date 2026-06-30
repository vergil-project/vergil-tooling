"""Audit, diff, and apply repository configuration for managed repos.

Combines local filesystem checks (vergil.toml, CLAUDE.md,
.claude/settings.json, .claude/hooks/) with GitHub API configuration
auditing.
"""

from __future__ import annotations

import argparse
import base64
import sys
import tomllib
from pathlib import Path

from vergil_tooling.lib import github
from vergil_tooling.lib.config import VergilConfig, _parse_raw_config
from vergil_tooling.lib.github_config import (
    ConfigDiff,
    DiffItem,
    apply_desired_state,
    compute_desired_state,
    compute_diff,
    fetch_actual_state,
    format_rules_delta,
)
from vergil_tooling.lib.output import emit_error
from vergil_tooling.lib.repo_config import audit_local_config


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
_CONFIG_REFS = ("main", "develop")


def _is_not_found(exc: github.GitHubAPIError) -> bool:
    """Return True when a GitHub API error is an HTTP 404 (Not Found)."""
    return "404" in (exc.stderr or "") or "404" in (exc.stdout or "")


def _decode_config(
    repo: str, ref: str, content_data: dict[str, object] | list[object]
) -> VergilConfig:
    """Decode a GitHub contents API response into a VergilConfig."""
    if not isinstance(content_data, dict):
        msg = f"Unexpected response fetching config from {repo}@{ref}"
        raise RuntimeError(msg)
    content = content_data.get("content")
    if not isinstance(content, str):
        msg = f"No content field in config response from {repo}@{ref}"
        raise RuntimeError(msg)
    raw_bytes = base64.b64decode(content)
    raw = tomllib.loads(raw_bytes.decode())
    return _parse_raw_config(raw, source=f"{repo}@{ref}:vergil.toml")


def _fetch_config_from_ref(repo: str, ref: str) -> VergilConfig | None:
    """Fetch and parse vergil.toml from a specific branch of a remote repo.

    Returns ``None`` when the file is absent on that branch (HTTP 404).
    Non-404 API errors propagate unchanged.
    """
    try:
        content_data = github.read_json(
            "api",
            f"repos/{repo}/contents/vergil.toml?ref={ref}",
        )
    except github.GitHubAPIError as exc:
        if _is_not_found(exc):
            return None
        raise
    return _decode_config(repo, ref, content_data)


def _load_cwd_config() -> VergilConfig | None:
    """Load vergil.toml from the current working directory, or None if absent."""
    path = Path.cwd() / "vergil.toml"
    if not path.exists():
        return None
    return _load_local_config(str(path))


def _resolve_config(repo: str) -> VergilConfig:
    """Resolve the canonical vergil.toml for ``repo``.

    Resolution order: the ``main`` branch, then ``develop``, then the local
    working-directory copy when the cwd is a checkout of ``repo``. The remote
    branches are the canonical source; the local fallback makes bootstrapping
    ergonomic — run ``apply`` from the repo root before the file has landed on
    any canonical branch — and is safe under the trust model: agents may *write*
    vergil.toml, but only a human runs ``apply`` and owns that action, so the
    local copy can never be applied without human oversight. The fallback is
    gated on the cwd matching the target repo so a ``--repo other/repo`` run can
    never apply the current directory's unrelated config.
    """
    for ref in _CONFIG_REFS:
        config = _fetch_config_from_ref(repo, ref)
        if config is not None:
            return config
    cwd_is_repo = _cwd_matches_repo(repo)
    if cwd_is_repo:
        local = _load_cwd_config()
        if local is not None:
            return local
    locations = "the 'main' and 'develop' branches"
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
        print(f"  {repo}: NON-COMPLIANT ({len(diff.items)} issues)")
        for item in diff.items:
            print(_format_item(item))
    for field_name in diff.skipped:
        if field_name.startswith("security."):
            print(
                f"    {field_name}: skipped (requires GitHub Advanced Security for private repos)"
            )
        elif field_name.endswith(".bypass_actors"):
            print(f"    {field_name}: skipped (not visible with GitHub App credentials)")
    _print_warnings(diff)


def _apply_repo(repo: str, config: VergilConfig) -> list[str]:
    """Apply desired state to a repo. Returns branches with legacy protection removed."""
    result = fetch_actual_state(repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(config, visibility=result.visibility, is_org=is_org)
    return apply_desired_state(repo, desired)


def _cwd_matches_repo(repo: str) -> bool:
    """Check whether CWD's git origin matches the target repo."""
    try:
        cwd_repo = github.current_repo()
    except Exception:
        return False
    return cwd_repo == repo


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    all_compliant = True

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

    if not local_compliant:
        all_compliant = False

    repo = _resolve_repo(args)
    # Exit codes are a contract for callers (e.g. vrg-release): 0 = compliant,
    # 1 = genuinely non-compliant, 2 = the audit could not complete. Config
    # resolution and the GitHub state fetch can fail operationally (missing
    # vergil.toml, HTTP 403 reading actions/permissions under an App token);
    # those are exit 2 with a clean diagnostic, never a false "non-compliant"
    # verdict or a raw traceback (#1691).
    try:
        config = _load_local_config(args.config) if args.config else _resolve_config(repo)
    except RuntimeError as exc:
        emit_error(str(exc))
        return 2

    try:
        github_diff = _audit_repo(repo, config)
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
    else:
        print(f"  Applying to {repo}...")
        try:
            removed = _apply_repo(repo, config)
        except RuntimeError as exc:
            emit_error(str(exc))
            return 1
        if removed:
            print(f"  {repo}: applied (legacy protection removed: {', '.join(removed)})")
        else:
            print(f"  {repo}: applied")

    if not local_compliant:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
