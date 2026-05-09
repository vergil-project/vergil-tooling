"""Ensure labels exist in GitHub repositories.

Supports three modes:

1. **Single-label** — create/update one label in one repo.
2. **Sync** — provision every label from the canonical registry into a repo.
3. **Project** — discover repos via a GitHub Project and sync each one.
"""

from __future__ import annotations

import argparse
import sys

from standard_tooling.lib import github
from standard_tooling.lib.github import list_project_repos
from standard_tooling.lib.labels import load_labels


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Ensure GitHub labels exist.")

    # Single-label mode
    parser.add_argument("--repo", help="Repository (OWNER/REPO)")
    parser.add_argument("--label", help="Label name (single-label mode)")
    parser.add_argument("--color", help="Label color hex (no #)")
    parser.add_argument("--description", help="Label description")

    # Sync mode
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Provision all labels from the canonical registry",
    )

    # Project mode
    parser.add_argument("--owner", help="GitHub owner (project mode)")
    parser.add_argument("--project", help="GitHub Project number (project mode)")

    args = parser.parse_args(argv)

    # Validation
    if args.owner or args.project:
        if not (args.owner and args.project and args.sync):
            parser.error("--owner and --project require each other and --sync")
    elif args.sync:
        if not args.repo:
            parser.error("--sync requires --repo (or --owner/--project)")
    else:
        if not (args.repo and args.label):
            parser.error("--repo and --label are required in single-label mode")

    return args


def _ensure_single(repo: str, name: str, color: str | None, description: str | None) -> None:
    """Create or update a single label."""
    cmd: list[str] = ["label", "create", name, "--repo", repo, "--force"]
    if color:
        cmd.extend(["--color", color])
    if description:
        cmd.extend(["--description", description])
    github.run(*cmd)
    print(f"  {name}")


def _delete_label(repo: str, name: str) -> None:
    """Delete a label, ignoring errors if it doesn't exist."""
    try:
        github.run("label", "delete", name, "--repo", repo, "--yes")
        print(f"  deleted {name}")
    except Exception:  # noqa: BLE001, S110
        pass  # label didn't exist — nothing to do


def sync_repo(repo: str) -> None:
    """Provision all canonical labels and delete deprecated ones."""
    registry = load_labels()
    print(f"Syncing labels for {repo}:")
    for label in registry["labels"]:
        _ensure_single(repo, label["name"], label["color"], label["description"])
    for name in registry.get("delete", []):
        _delete_label(repo, name)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.owner and args.project:
        # Project mode: discover repos, sync each
        repos = list_project_repos(args.owner, args.project)
        print(f"Found {len(repos)} repos in project {args.project}")
        for repo in repos:
            sync_repo(repo)
    elif args.sync:
        # Sync mode: single repo
        sync_repo(args.repo)
    else:
        # Single-label mode
        _ensure_single(args.repo, args.label, args.color, args.description)

    return 0


if __name__ == "__main__":
    sys.exit(main())
