"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, github, progress
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages
from vergil_tooling.lib.release.resume import find_resume_target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the full release workflow from develop to main.",
    )
    parser.add_argument(
        "version_override",
        nargs="?",
        choices=("minor", "major"),
        default=None,
        help="Bump to next minor or major before releasing (default: release current version).",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        default=False,
        help="Skip rolling-tag promotion after release.",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        default=None,
        metavar="X.Y.Z",
        help=(
            "Resume an interrupted release from its open tracking issue. "
            "Optionally name the version when more than one is in flight."
        ),
    )
    progress.add_progress_args(parser, build_stages())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = git.repo_root()

    resume_requested = args.resume is not None
    if resume_requested and args.version_override is not None:
        print(
            "vrg-release: --resume cannot be combined with a minor/major bump — "
            "the version is fixed by the in-flight release.",
            file=sys.stderr,
        )
        return 1

    resume_version: str | None = None
    resume_issue_number: int | None = None
    if resume_requested:
        try:
            resume_version, resume_issue_number = find_resume_target(
                github.current_repo(),
                [stage.name for stage in build_stages()],
                version=args.resume or None,
            )
        except ReleaseError as exc:
            print(f"vrg-release: {exc}", file=sys.stderr)
            return 1
        print(f"Resuming release {resume_version} (issue #{resume_issue_number}).")

    state = ReleaseState(
        version_override=args.version_override,
        repo_root=repo_root,
        promote=not args.no_promote,
        resume=resume_requested,
        resume_version=resume_version,
        resume_issue_number=resume_issue_number,
    )
    rc = progress.run_pipeline(
        state,
        build_stages(),
        command="vrg-release",
        label="vrg-release",
        args=args,
        repo_root=repo_root,
    )
    # The progress renderer collapses each finished stage to a one-line
    # summary, which erases the consumer-refresh commands — the one piece
    # of output the human must act on. Re-print them below the summary.
    if state.ctx is not None and state.ctx.consumer_refresh_message:
        print()
        print(state.ctx.consumer_refresh_message)
    return rc


if __name__ == "__main__":
    sys.exit(main())
