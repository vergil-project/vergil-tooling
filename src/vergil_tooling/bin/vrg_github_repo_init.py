"""Interactive wizard for bootstrapping VERGIL-managed repositories."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.config import _ENUMS
from vergil_tooling.lib.repo_init import (
    RepoInitContext,
    prompt_choice,
    prompt_free_text,
    prompt_yes_no,
    run_wizard,
)

# Flags that a --non-interactive new-repo run must supply: the prompts that have
# no sensible default (issue #2382). Everything else falls back to its
# documented default. (attr, flag) pairs; attr is the argparse dest.
_REQUIRED_NON_INTERACTIVE = (
    ("description", "--description"),
    ("repository_type", "--repository-type"),
    ("branching_model", "--branching-model"),
    ("versioning_scheme", "--versioning-scheme"),
    ("release_model", "--release-model"),
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
        help="Repository visibility, new repos only (default: public)",
    )

    # Wizard-prompt flags. Each overrides the matching interactive prompt exactly
    # as --visibility already does; supplying all of them runs the tool with zero
    # prompts (issue #2382).
    parser.add_argument(
        "--description",
        help="Project description (one paragraph)",
    )
    parser.add_argument(
        "--repository-type",
        choices=sorted(_ENUMS["repository-type"]),
        help="Repository type",
    )
    parser.add_argument(
        "--language",
        choices=sorted(_ENUMS["primary-language"]),
        help="Primary language (omit for a language-less repo)",
    )
    parser.add_argument(
        "--branching-model",
        choices=sorted(_ENUMS["branching-model"]),
        help="Branching model",
    )
    parser.add_argument(
        "--versioning-scheme",
        choices=sorted(_ENUMS["versioning-scheme"]),
        help="Versioning scheme",
    )
    parser.add_argument(
        "--release-model",
        choices=sorted(_ENUMS["release-model"]),
        help="Release model",
    )
    parser.add_argument(
        "--versions",
        help="CI versions, comma-separated (default: language-derived)",
    )
    parser.add_argument(
        "--integration-tests",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable integration tests (default: no)",
    )
    parser.add_argument(
        "--publish-release",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Publish releases (default: derived from --release-model)",
    )
    parser.add_argument(
        "--publish-docs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Publish docs (default: yes)",
    )
    parser.add_argument(
        "--vergil-version",
        help="Vergil dependency version (default: v2.1)",
    )
    parser.add_argument(
        "--license",
        choices=("MIT", "GPL-3.0", "Apache-2.0", "none"),
        help="License (default: MIT)",
    )
    parser.add_argument(
        "--initial-version",
        help="Initial version (default: 0.1.0)",
    )
    parser.add_argument(
        "--non-interactive",
        "--yes",
        action="store_true",
        dest="non_interactive",
        help="Run with no prompts; fail loud on any missing required flag",
    )

    args = parser.parse_args(argv)

    if args.adopt and args.repo:
        parser.error("--adopt cannot be used with a repo argument")

    if not args.adopt and not args.repo:
        parser.error("provide ORG/NAME or use --adopt from inside a clone")

    if args.repo and "/" not in args.repo:
        parser.error("repo must be in ORG/NAME format")

    # A --non-interactive new-repo run must supply every no-default prompt.
    # Adopt runs draw those from the existing vergil.toml, so they are validated
    # later once that config is loaded (repo_init.step_generate_config).
    if args.non_interactive and not args.adopt:
        missing = [flag for attr, flag in _REQUIRED_NON_INTERACTIVE if getattr(args, attr) is None]
        if missing:
            parser.error(
                "--non-interactive requires these flags for a new repo: " + ", ".join(missing)
            )

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
        # Under --non-interactive/--yes the destructive confirmation is
        # auto-accepted (issue #2382).
        if not args.non_interactive and not prompt_yes_no("Continue?", default=False):
            print("Aborted.")
            return 1

        ctx = RepoInitContext(org=org, name=name, adopt=True)
        if args.description is not None:
            ctx.description = args.description
    else:
        org, name = args.repo.split("/", 1)
        ctx = RepoInitContext(org=org, name=name)

        if args.visibility:
            ctx.visibility = args.visibility
        elif args.non_interactive:
            ctx.visibility = "public"
        else:
            ctx.visibility = prompt_choice(
                "Repository visibility",
                ["public", "private"],
                default="public",
            )

        # --description is a required flag under --non-interactive (validated in
        # parse_args), so it is always present there; the else is reached only by
        # an interactive run, which prompts.
        if args.description is not None:
            ctx.description = args.description
        else:
            ctx.description = prompt_free_text("Project description (one paragraph)")

    # Carry the remaining wizard-prompt flags into the context; the wizard
    # resolves each (flag override > non-interactive default > prompt).
    ctx.non_interactive = args.non_interactive
    ctx.opt_description = args.description
    ctx.opt_repository_type = args.repository_type
    ctx.opt_primary_language = args.language
    ctx.opt_branching_model = args.branching_model
    ctx.opt_versioning_scheme = args.versioning_scheme
    ctx.opt_release_model = args.release_model
    ctx.opt_ci_versions = args.versions
    ctx.opt_integration_tests = args.integration_tests
    ctx.opt_publish_release = args.publish_release
    ctx.opt_publish_docs = args.publish_docs
    ctx.opt_vergil_version = args.vergil_version
    ctx.opt_license_type = args.license
    ctx.opt_initial_version = args.initial_version

    run_wizard(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
