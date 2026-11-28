"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import re
import sys

from vergil_tooling.lib import git, github, identity_mode, progress
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.release import gate, reload_reminder
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages
from vergil_tooling.lib.release.resume import find_resume_target

# Flags that were removed, in the order they are checked: promote and install
# are part of every release (spec §4.8 of epic vergil-project/.github#1440;
# --install since #1184). Each is still parsed, hidden from --help, so that
# passing it is a clear fatal error rather than argparse's generic
# "unrecognized arguments".
REMOVED_FLAGS = ("--no-promote", "--no-install", "--install")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


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

    # A caller still passing a removed flag is told so loudly, before any
    # side effect, rather than having the flag silently accepted.
    for flag in REMOVED_FLAGS:
        if getattr(args, _dest(flag)):
            print(removed_message(flag), file=sys.stderr)
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
    if args.supersede is not None:
        print(f"Superseding release {args.supersede} with a fresh release.")

    state = ReleaseState(
        version_override=args.version_override,
        repo_root=repo_root,
        without_deps=args.without_deps,
        allow_pending_deps=args.allow_pending_deps,
        resume=resume_requested,
        resume_version=resume_version,
        resume_issue_number=resume_issue_number,
        supersede=args.supersede,
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
    # summary. The install stage runs the commands itself and, after a run
    # that succeeded, leaves nothing to read; what it does leave (a failed
    # run's full output) is re-printed below the summary.
    if state.ctx is not None and state.ctx.install_message:
        print()
        print(state.ctx.install_message)
    # A plugin repo's finished release ends with the reload reminder, printed
    # last so it is what the human reads. Never on failure.
    if (
        rc == 0
        and state.ctx is not None
        and reload_reminder.reload_plugins_enabled(state.ctx.repo_root)
    ):
        print()
        print(reload_reminder.REMINDER)
    return rc


def removed_message(flag: str) -> str:
    """The error *flag*, a removed flag, exits with."""
    return f"vrg-release: {flag} was removed; promote and install are part of every release"


def _dest(flag: str) -> str:
    """The argparse attribute *flag* is stored under."""
    return flag.removeprefix("--").replace("-", "_")


def _version(value: str) -> str:
    """An ``X.Y.Z`` release version argument."""
    if not _VERSION_RE.match(value):
        msg = f"{value!r} is not a release version X.Y.Z"
        raise argparse.ArgumentTypeError(msg)
    return value


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
    for flag in REMOVED_FLAGS:
        parser.add_argument(flag, action="store_true", default=False, help=argparse.SUPPRESS)
    # A supersede is a fresh release and a resume finishes an existing one,
    # so the two are a usage error together (spec §4.7 of epic
    # vergil-project/.github#1440).
    in_flight = parser.add_mutually_exclusive_group()
    in_flight.add_argument(
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
    in_flight.add_argument(
        "--supersede",
        type=_version,
        default=None,
        metavar="X.Y.Z",
        help=(
            "Start a fresh release that supersedes open release X.Y.Z, which "
            "--resume cannot finish: one that is tagged with only cd-publish, "
            "package-index, update-deps or install left, or one halted at "
            "confirm-main with no tag after its CD release job failed. One whose "
            "tracking issue fails the identity rule is rebuilt from GitHub and git "
            "facts instead of its state block. It absorbs "
            "X.Y.Z's issues and closes its tracking issue. Combines with minor/major."
        ),
    )
    progress.add_progress_args(parser, build_stages())
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
