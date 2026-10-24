"""Ensure labels exist in GitHub repositories.

Supports three modes:

1. **Single-label** — create/update one label in one repo.
2. **Sync** — provision every label from the canonical registry into a repo,
   and delete the deprecated and retired labels it still has.
3. **Project** — discover repos via a GitHub Project and sync each one.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.github import list_project_repos
from vergil_tooling.lib.intake_route import RETIRED_LABELS
from vergil_tooling.lib.labels import load_labels

# Generous ceiling for ``gh label list``: a managed repo carries a few dozen.
_LABEL_LIST_LIMIT = "1000"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Scope the App token to the owner named by the target argument — the
    # project owner in project mode, else the --repo owner — so labelling a
    # repo outside the cwd org selects the right installation (#2070).
    owner = args.owner if (args.owner and args.project) else args.repo.split("/", 1)[0]

    try:
        with github.target_org(owner):
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
    except github.NoInstallationError as exc:
        print(f"vrg-ensure-label: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1

    return 0


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


def _existing_labels(repo: str) -> set[str]:
    """Return the names of the labels *repo* currently has."""
    data = github.read_json(
        "label", "list", "--repo", repo, "--json", "name", "--limit", _LABEL_LIST_LIMIT
    )
    if not isinstance(data, list):
        msg = f"unexpected `gh label list` output for {repo}: {data!r}"
        raise TypeError(msg)
    return {entry["name"] for entry in cast("list[dict[str, str]]", data)}


def labels_to_delete(registry: dict[str, Any]) -> list[str]:
    """Return every label a sync de-provisions, in a stable order.

    That is the registry's ``delete`` list (GitHub default cruft and deprecated
    aliases) plus the retired intake kind labels (epic .github#176), whose one
    source of truth is :data:`intake_route.RETIRED_LABELS`.
    """
    names = [*registry.get("delete", []), *sorted(RETIRED_LABELS)]
    return list(dict.fromkeys(names))


def sync_repo(repo: str) -> None:
    """Provision all canonical labels and delete deprecated and retired ones.

    Deletion is idempotent: only labels the repo actually has are deleted, so
    an already-clean repo is a no-op, and a ``gh`` failure (a real API error,
    not an absent label) propagates rather than being swallowed. Deleting a
    label also strips it from every issue still carrying it, which is intended.
    """
    registry = load_labels()
    print(f"Syncing labels for {repo}:")
    for label in registry["labels"]:
        _ensure_single(repo, label["name"], label["color"], label["description"])
    present = _existing_labels(repo)
    for name in labels_to_delete(registry):
        if name in present:
            github.run("label", "delete", name, "--repo", repo, "--yes")
            print(f"  deleted {name}")


if __name__ == "__main__":
    sys.exit(main())
