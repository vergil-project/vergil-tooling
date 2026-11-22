"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, github, identity_mode, progress
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.release import gate
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages
from vergil_tooling.lib.release.resume import find_resume_target

INSTALL_REMOVED = (
    "vrg-release: --install was removed; installing is now the default "
    "(use --no-install to skip it)"
)


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # The release workflow — branch, tag, merge, finalize — is a human
    # action. Hard-abort under any agent identity before any side effect, so
    # an agent VM cannot drive a release (issue #1694). Fail closed: any agent
    # signal refuses, whatever else is set (stricter than the is_agent()-only
    # guard of other human-only commands such as vrg-submit-pr).
    if identity_mode.is_agent() or identity_mode.any_agent_signal():
        print(
            "vrg-release: releasing is a human maintainer action. Agents cannot run vrg-release.",
            file=sys.stderr,
        )
        return 1

    # --install was removed when installing became the default (#1184): a
    # caller still passing it is told so loudly, before any side effect,
    # rather than having the flag silently accepted.
    if args.install:
        print(INSTALL_REMOVED, file=sys.stderr)
        return 1

    repo_root = git.repo_root()

    # Follow the repo's declared identity (issue #4175): a repo that does not
    # set [publish].release = true is refused before the resume lookup or any
    # stage, so nothing is branched, opened or merged.
    refusal = gate.unreleased_refusal(repo_root)
    if refusal is not None:
        print(f"vrg-release: {refusal}", file=sys.stderr)
        return 1

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
        install=not args.no_install,
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
    # summary. The consumer-refresh stage runs the commands itself and, after
    # a run that succeeded, leaves nothing to read; what it does leave (the
    # hold warning for a deferred publish, the commands under --no-install,
    # the notice that none is configured, or a failed run's full output) is
    # re-printed below the summary.
    if state.ctx is not None and state.ctx.consumer_refresh_message:
        print()
        print(state.ctx.consumer_refresh_message)
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
        help=(
            "Skip the post-release dependency update (the update-deps stage). "
            "Recorded on the tracking issue, so every later --resume skips it "
            "too; with --resume of a release whose tracking issue is still "
            "open, it skips an update that failed."
        ),
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
        "--no-install",
        action="store_true",
        default=False,
        help=(
            "Skip the install step. By default the consumer-refresh stage runs "
            "the [publish].consumer-refresh commands as the release's last "
            "stage; with this flag it only prints them."
        ),
    )
    # Removed (#1184): still parsed, and hidden from --help, only so that
    # passing it is the clear fatal error main() prints rather than
    # argparse's generic "unrecognized arguments".
    parser.add_argument("--install", action="store_true", default=False, help=argparse.SUPPRESS)
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
