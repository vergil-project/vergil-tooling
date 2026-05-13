"""Audit, diff, and apply GitHub configuration for managed repos."""

from __future__ import annotations

import argparse
import base64
import sys
import tomllib
from pathlib import Path

from vergil_tooling.lib import github
from vergil_tooling.lib.config import StConfig, _parse_raw_config
from vergil_tooling.lib.github_config import (
    ConfigDiff,
    apply_desired_state,
    compute_desired_state,
    compute_diff,
    fetch_actual_state,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enforce canonical GitHub configuration.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("audit", "diff", "apply"):
        sp = sub.add_parser(name)
        sp.add_argument(
            "--repo",
            help="Single repo (OWNER/REPO); defaults to current git remote",
        )
        sp.add_argument("--owner", help="GitHub owner (project mode)")
        sp.add_argument("--project", help="GitHub Project number")
        sp.add_argument(
            "--config",
            help="Local path to vergil.toml (overrides remote fetch)",
        )

    args = parser.parse_args(argv)

    has_owner = getattr(args, "owner", None)
    has_project = getattr(args, "project", None)
    if bool(has_owner) != bool(has_project):
        parser.error("--owner and --project must be specified together")

    return args


def _resolve_repos(args: argparse.Namespace) -> list[str]:
    """Return list of repos to operate on."""
    if args.repo:
        return [args.repo]
    if args.owner and args.project:
        return github.list_project_repos(args.owner, args.project)
    return [github.current_repo()]


def _load_local_config(path: str) -> StConfig:
    """Load and parse vergil.toml from a local file path."""
    with Path(path).open("rb") as f:
        raw = tomllib.load(f)
    return _parse_raw_config(raw)


def _fetch_remote_config(repo: str) -> StConfig:
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


def _audit_repo(repo: str, config: StConfig) -> ConfigDiff:
    """Compute diff between desired and actual state for a repo."""
    result = fetch_actual_state(repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(config, visibility=result.visibility, is_org=is_org)
    return compute_diff(desired=desired, actual=result.state)


def _print_diff(repo: str, diff: ConfigDiff) -> None:
    """Print diff results for a repo."""
    if diff.is_compliant():
        print(f"  {repo}: compliant")
        return
    print(f"  {repo}: NON-COMPLIANT ({len(diff.items)} issues)")
    for item in diff.items:
        print(f"    {item.field}: expected={item.expected!r}, actual={item.actual!r}")


def _apply_repo(repo: str, config: StConfig) -> list[str]:
    """Apply desired state to a repo. Returns branches with legacy protection removed."""
    result = fetch_actual_state(repo)
    is_org = result.owner_type == "Organization"
    desired = compute_desired_state(config, visibility=result.visibility, is_org=is_org)
    return apply_desired_state(repo, desired)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repos = _resolve_repos(args)
    all_compliant = True

    local_config = _load_local_config(args.config) if args.config else None

    def get_config(repo: str) -> StConfig:
        return local_config if local_config is not None else _fetch_remote_config(repo)

    for repo in repos:
        config = get_config(repo)
        diff = _audit_repo(repo, config)
        _print_diff(repo, diff)
        if not diff.is_compliant():
            all_compliant = False

    if args.command == "audit":
        return 0 if all_compliant else 1
    if args.command == "diff":
        return 0

    non_compliant = [r for r in repos if not _audit_repo(r, get_config(r)).is_compliant()]
    if not non_compliant:
        print("All repos compliant, nothing to apply.")
        return 0

    for repo in non_compliant:
        config = get_config(repo)
        print(f"  Applying to {repo}...")
        removed = _apply_repo(repo, config)
        if removed:
            print(f"  {repo}: applied (legacy protection removed: {', '.join(removed)})")
        else:
            print(f"  {repo}: applied")

    return 0


if __name__ == "__main__":
    sys.exit(main())
