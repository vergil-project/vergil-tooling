"""Interactive wizard for bootstrapping VERGIL-managed repositories."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    prompt_choice,
    prompt_free_text,
    prompt_yes_no,
    run_wizard,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Bootstrap a VERGIL-managed repository.",
    )

    parser.add_argument(
        "repo",
        nargs="?",
        help="Repository to create (ORG/NAME format)",
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help="Adopt an existing repo (run from inside its clone)",
    )
    parser.add_argument(
        "--visibility",
        choices=("public", "private"),
        help="Repository visibility (new repos only)",
    )

    args = parser.parse_args(argv)

    if args.adopt and args.repo:
        parser.error("--adopt cannot be used with a repo argument")

    if not args.adopt and not args.repo:
        parser.error("provide ORG/NAME or use --adopt from inside a clone")

    if args.repo and "/" not in args.repo:
        parser.error("repo must be in ORG/NAME format")

    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.adopt:
        from vergil_tooling.lib import github

        repo_slug = github.current_repo()
        org, name = repo_slug.split("/", 1)

        print(f"Adopting {repo_slug}...")
        print(
            "WARNING: This will overwrite all Vergil-managed files to canonical state.\n"
            "Files affected: vergil.toml, CLAUDE.md, .claude/settings.json,\n"
            ".claude/hooks/guard.sh, LICENSE, README.md, .gitignore, CI/CD workflows,\n"
            "docs site config, GitHub settings, rulesets, and labels."
        )
        if not prompt_yes_no("Continue?", default=False):
            print("Aborted.")
            return 1

        ctx = RepoInitContext(org=org, name=name, adopt=True)
    else:
        org, name = args.repo.split("/", 1)
        ctx = RepoInitContext(org=org, name=name)

        if args.visibility:
            ctx.visibility = args.visibility
        else:
            ctx.visibility = prompt_choice(
                "Repository visibility",
                ["public", "private"],
                default="public",
            )

        ctx.description = prompt_free_text("Project description (one paragraph)")

    run_wizard(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
