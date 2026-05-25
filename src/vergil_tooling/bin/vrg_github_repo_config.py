"""Audit, diff, and apply repository configuration for managed repos.

Combines local filesystem checks (vergil.toml, CLAUDE.md,
.claude/settings.json, .githooks) with GitHub API configuration
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
    apply_desired_state,
    compute_desired_state,
    compute_diff,
    fetch_actual_state,
)
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
    return _parse_raw_config(raw)


def _fetch_remote_config(repo: str) -> VergilConfig:
    """Fetch and parse vergil.toml from a remote repo."""
    content_data = github.read_json(
        "api",
        f"repos/{repo}/contents/vergil.toml",
    )
    if not isinstance(content_data, dict):
        msg = f"Unexpected response fetching config from {repo}"
        raise RuntimeError(msg)
    content = content_data.get("content")
    if not isinstance(content, str):
        msg = f"No content field in config response from {repo}"
        raise RuntimeError(msg)
    raw_bytes = base64.b64decode(content)
    raw = tomllib.loads(raw_bytes.decode())
    return _parse_raw_config(raw)


def _audit_repo(repo: str, config: VergilConfig) -> ConfigDiff:
    """Compute diff between desired and actual GitHub state for a repo."""
    result = fetch_actual_state(repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(config, visibility=result.visibility, is_org=is_org)
    return compute_diff(desired=desired, actual=result.state)


def _print_local_diff(diff: ConfigDiff) -> None:
    """Print local config audit results."""
    if diff.is_compliant():
        print("  local: compliant")
        return
    print(f"  local: NON-COMPLIANT ({len(diff.items)} issues)")
    for item in diff.items:
        print(f"    {item.field}: expected={item.expected!r}, actual={item.actual!r}")


def _print_diff(repo: str, diff: ConfigDiff) -> None:
    """Print GitHub config diff results for a repo."""
    if diff.is_compliant():
        print(f"  {repo}: compliant")
    else:
        print(f"  {repo}: NON-COMPLIANT ({len(diff.items)} issues)")
        for item in diff.items:
            print(f"    {item.field}: expected={item.expected!r}, actual={item.actual!r}")
    for field_name in diff.skipped:
        if field_name.startswith("security."):
            print(
                f"    {field_name}: skipped (requires GitHub Advanced Security for private repos)"
            )


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
    config = _load_local_config(args.config) if args.config else _fetch_remote_config(repo)

    github_diff = _audit_repo(repo, config)
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
        removed = _apply_repo(repo, config)
        if removed:
            print(f"  {repo}: applied (legacy protection removed: {', '.join(removed)})")
        else:
            print(f"  {repo}: applied")

    if not local_compliant:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
