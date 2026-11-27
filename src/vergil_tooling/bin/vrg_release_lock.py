"""Read the release lock of a repository, or break it (spec §5.6, epic vergil-project/.github#1664).

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

``vrg-release-lock break --reason "<why>" [--json] [--repo <owner>/<repo>]``
is the human's escape hatch (:func:`~vergil_tooling.lib.release_lock.reclaim.break_lock`):
it records the reason and the old lock in the lock history and on the
tracking issue, deletes the lock, live or unparseable, and runs the re-arm
sweep. It is human only: refused, before anything is read, under every agent
identity and agent signal, as ``vrg-release`` is. ``--reason`` is required
and must not be blank. It exits 0 once the lock is broken and the sweep has
run, printing what it did (``--json``: the result as one object, with
``repo``, ``action``, ``old_sha``, ``tracking_issue``, ``reason`` and
``rearm``: ``armed``, ``merged``, ``held`` and ``refused`` as a list of
``{"url", "reason"}``), and 1 on any failure, with the error on stderr and
nothing on stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys

from vergil_tooling.lib import identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock import reclaim
from vergil_tooling.lib.release_lock import status as lock_status
from vergil_tooling.lib.release_lock.model import ReleaseLockError

EXIT_OK = 0
EXIT_ERROR = 1

_REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "break" and (identity_mode.is_agent() or identity_mode.any_agent_signal()):
        # Fail closed, as vrg-release does: any agent signal refuses, whatever else is set.
        print(
            "vrg-release-lock break: breaking the release lock is a human maintainer action."
            " Agents cannot run it.",
            file=sys.stderr,
        )
        return EXIT_ERROR
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
    if args.command == "break":
        return _break(repo, reason=args.reason, as_json=args.json)
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


def _break(repo: str, *, reason: str, as_json: bool) -> int:
    actor = f"{identity_mode.current_mode().value} on {socket.gethostname()}"
    try:
        result = reclaim.break_lock(repo, actor=actor, reason=reason)
    except ReleaseLockError as exc:
        print(f"ERROR: {repo}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if as_json:
        print(json.dumps(result.as_json(), indent=2))
        return EXIT_OK
    rearm = result.rearm
    lines = [
        f"broke the release lock on {repo} (was at {result.old_sha}): {result.reason}",
        f"tracking issue: {result.tracking_issue or '(none)'}",
        f"re-armed: {', '.join(rearm.armed) or '(none)'}",
        f"merged: {', '.join(rearm.merged) or '(none)'}",
        f"held: {', '.join(rearm.held) or '(none)'}",
        "refused: " + (", ".join(f"{url} ({why})" for url, why in rearm.refused) or "(none)"),
    ]
    print("\n".join(lines))
    return EXIT_OK


def _repo(value: str) -> str:
    if not _REPO_RE.fullmatch(value):
        msg = f"{value!r} is not <owner>/<repo>"
        raise argparse.ArgumentTypeError(msg)
    return value


def _reason(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        msg = "the reason must not be empty"
        raise argparse.ArgumentTypeError(msg)
    return stripped


def _add_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        type=_repo,
        default=None,
        help="The repository, <owner>/<repo> (default: the one origin names).",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read the release lock of a repository, or break it."
    )
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
    _add_repo(status)
    breaker = commands.add_parser(
        "break",
        help="Break the release lock (human only): record why, delete it, re-arm the PRs.",
        description=(
            "Break the release lock of the repository, live or unreadable: record the reason"
            " and the old lock in the lock history and on its tracking issue, delete the lock,"
            " then re-arm every PR it held. Human only: refused under every agent identity"
            " and agent signal. Exits 0 once broken and re-armed, 1 on any failure."
        ),
    )
    breaker.add_argument(
        "--reason", type=_reason, required=True, help="Why the lock is broken (not empty)."
    )
    breaker.add_argument("--json", action="store_true", help="Print the result as one JSON object.")
    _add_repo(breaker)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
