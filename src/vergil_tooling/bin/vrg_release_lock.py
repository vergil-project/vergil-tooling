"""Read the release lock of a repository (spec §5.6 of epic vergil-project/.github#1664).

``vrg-release-lock status [--json] [--repo <owner>/<repo>]`` prints the lock
``refs/vergil/release-lock`` holds on the repo's origin: its holder, version,
tracking issue, stage, health and heartbeat, whether it is stale and why,
and the PRs it holds unarmed (:mod:`vergil_tooling.lib.release_lock.status`).
Any identity may run it; it only reads.

The text form is ``no release lock held``, or one line per field ending in
``stale: yes|no (<reason>)`` and ``held PRs: ...``. ``--json`` prints one
object with the record's fields (``null`` with no readable record) plus
``repo``, ``held``, ``sha``, ``error``, ``stale``, ``live``, ``reason``,
``judged_tracking_issue``, ``heartbeat_age_secs`` and ``held_prs`` (a list of
``{"number", "url"}``). It is the machine-readable state a future
auto-releaser reads.

It exits 0 on a completed read, whatever the lock's state, and 1 when any
read fails (the lock ref, a tracking issue, the PR listing), with the error
on stderr and nothing on stdout. A failure is never shown as "no lock".
Plan Task 22 adds ``break``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock import status as lock_status
from vergil_tooling.lib.release_lock.model import ReleaseLockError

EXIT_OK = 0
EXIT_ERROR = 1

_REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo
    if repo is None:
        try:
            repo = lock_status.origin_repo()
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: could not read the origin remote: {error_text(exc)}", file=sys.stderr)
            return EXIT_ERROR
        if repo is None:
            print(
                "ERROR: origin is not a github.com remote; name the repository with --repo",
                file=sys.stderr,
            )
            return EXIT_ERROR
    try:
        result = lock_status.gather(repo)
    except ReleaseLockError as exc:
        print(f"ERROR: {repo}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps(result.as_json(), indent=2))
    else:
        print("\n".join(lock_status.text_lines(result)))
    return EXIT_OK


def _repo(value: str) -> str:
    if not _REPO_RE.fullmatch(value):
        msg = f"{value!r} is not <owner>/<repo>"
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read the release lock of a repository.")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status",
        help="Print the release lock, whether it is stale, and the PRs it holds unarmed.",
        description=(
            "Print the release lock of the repository: holder, version, tracking issue,"
            " stage, health, heartbeat, whether it is stale, and the PRs it holds unarmed."
            " Exits 0 on a read, 1 on a failed read."
        ),
    )
    status.add_argument("--json", action="store_true", help="Print one JSON object.")
    status.add_argument(
        "--repo",
        type=_repo,
        default=None,
        help="The repository, <owner>/<repo> (default: the one origin names).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
