"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, github, identity_mode, progress
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.handoff import run_consumer_refresh
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages
from vergil_tooling.lib.release.resume import find_resume_target


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # The release workflow — branch, tag, merge, finalize — is a human
    # action, like vrg-submit-pr. Hard-abort under any agent identity before
    # any side effect, so an agent VM cannot drive a release (issue #1694).
    if identity_mode.is_agent():
        print(
            "vrg-release: releasing is a human maintainer action. Agents cannot run vrg-release.",
            file=sys.stderr,
        )
        return 1

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
        without_deps=args.without_deps,
        allow_pending_deps=args.allow_pending_deps,
        resume=resume_requested,
        resume_version=resume_version,
        resume_issue_number=resume_issue_number,
    )

    def success_label() -> str | None:
        # Name the cut tag in the completion line (issue #3753). The version
        # comes from the release context preflight built; before that there
        # is no version, so keep the plain label rather than guess.
        if state.ctx is None:
            return None
        return f"vrg-release v{state.ctx.version}"

    rc = progress.run_pipeline(
        state,
        build_stages(),
        command="vrg-release",
        label="vrg-release",
        success_label=success_label,
        args=args,
        repo_root=repo_root,
    )
    # The progress renderer collapses each finished stage to a one-line
    # summary, which erases the consumer-refresh commands — the one piece
    # of output the human must act on. Re-print them below the summary.
    if state.ctx is not None and state.ctx.consumer_refresh_message:
        print()
        print(state.ctx.consumer_refresh_message)

    # --install: run the consumer-refresh commands rather than leaving them
    # for the human to copy/paste (issue #1643). Only on a clean release —
    # a failed pipeline must not trigger an install — and only after the
    # message above has been printed, so the human sees what is about to run.
    if args.install and rc == 0:
        ctx = state.ctx
        commands = ctx.consumer_refresh_commands if ctx is not None else None
        if ctx is None or not commands:
            print(
                "vrg-release: --install was given but no [publish].consumer-refresh "
                "is configured; nothing to run.",
                file=sys.stderr,
            )
            return 1
        return run_consumer_refresh(commands, ctx.version)
    return rc


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
        "--without-deps",
        action="store_true",
        default=False,
        help="Skip the post-release dependency update (the update-deps stage).",
    )
    parser.add_argument(
        "--allow-pending-deps",
        action="store_true",
        default=False,
        help=(
            "Release even though a dependency-update PR is open (urgent fixes "
            "only); the bypass is logged on the tracking issue."
        ),
    )
    parser.add_argument(
        "--install",
        action="store_true",
        default=False,
        help=(
            "After a successful release, execute the [publish].consumer-refresh "
            "commands (the install/refresh step of the cascade) instead of only "
            "printing them."
        ),
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


if __name__ == "__main__":
    sys.exit(main())
