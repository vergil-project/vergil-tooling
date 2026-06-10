"""Mechanized dependency update — human-invoked, deterministic.

Runs on a clean, synced develop: upgrades dependencies in a managed worktree,
validates once, and drives the PR through merge and finalize. A no-op run
creates no PR.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, identity_mode, progress
from vergil_tooling.lib.update_deps.orchestrator import UpdateDepsState, build_stages


def _csv(value: str) -> list[str]:
    return [token.strip() for token in value.split(",") if token.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the mechanized dependency-update workflow on develop.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--only",
        type=_csv,
        metavar="NAME[,NAME]",
        help="Run only these updaters (comma-separated names).",
    )
    selection.add_argument(
        "--skip",
        type=_csv,
        metavar="NAME[,NAME]",
        help="Run all applicable updaters except these (comma-separated names).",
    )
    progress.add_progress_args(parser, build_stages())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if not identity_mode.is_human():
        print(
            "vrg-update-deps is a human-only command (PR submission, merge, and "
            "finalization are human actions). Refusing to run as an agent.",
            file=sys.stderr,
        )
        return 1
    args = parse_args(argv)
    repo_root = git.repo_root()
    state = UpdateDepsState(repo_root=repo_root, only=args.only, skip=args.skip)
    return progress.run_pipeline(
        state,
        build_stages(),
        command="vrg-update-deps",
        label="vrg-update-deps",
        args=args,
        repo_root=repo_root,
    )


if __name__ == "__main__":
    sys.exit(main())
