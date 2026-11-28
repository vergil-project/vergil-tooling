"""Create and check a skill's private state directory (doctrine §1.5).

``vrg-state-dir --kind <kind> ... --ensure`` checks a state directory in the
main clone level by level and prints its absolute path. The kinds and the
options each takes:

- ``issue-implement``, ``operational`` and ``upstream-report`` take ``--issue
  owner/name#N`` and check ``build/.vergil-state/<kind>/<N>/``.
- ``design-review`` takes ``--issue owner/name#M``, the planning issue, and
  ``--round ID``, a ``S``, ``P`` or ``A`` then digits, and checks
  ``build/.vergil-state/design-review/<M>/<ID>/``. An existing round directory
  is accepted; whether a round may pre-exist is the caller's decision.
- ``hoshin`` takes ``--repo owner/name``, ``--date YYYY-MM-DD`` (a real
  calendar date) and ``--mode {start,end}``, and checks
  ``build/.vergil-state/hoshin/<date>-<mode>/``. There is no issue, so
  ``--issue`` does not apply to it.

An option that does not apply to the kind, or a required one left out, is a
usage error. ``build`` must be a real directory (it is created with a plain
mkdir when absent); each level from ``.vergil-state`` down to the kind's last
level must be a real directory, not a symlink, with mode exactly 700, and an
absent level is created mode 700. A level that fails stops the command before
anything is created below it. The caller runs it before every write it makes
there and writes only to the path it prints.

Before it creates or checks anything it refuses unless the current clone's
``origin`` is the repository the kind is about: the ``--issue`` repository, or
the ``--repo`` repository for ``hoshin``. The directory is resolved from the
current directory's main clone, so a run from another repository's clone would
put it where no later run looks.

It makes the checks ``vrg-merge-review-request --ensure-dir`` makes for
``merge-review/``, through the same code (``lib/merge_review/state.py``).

Exit codes: ``0`` the directory is private and its path printed, ``1`` a
refusal (nothing on stdout, the reason on stderr), ``2`` a usage error,
including a malformed ``--round``, ``--date``, ``--repo`` or ``--mode``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.build_dir import BuildDirError
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.merge_review import request as req
from vergil_tooling.lib.merge_review.state import (
    HOSHIN_MODES,
    TASK_KINDS,
    StateDirError,
    check_hoshin_date,
    check_round_id,
    ensure_design_review_dir,
    ensure_hoshin_dir,
    ensure_task_dir,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1

_COMMAND = "vrg-state-dir"

#: Every kind: the three issue-numbered kinds, then the two shaped otherwise.
_KINDS = (*TASK_KINDS, "design-review", "hoshin")

#: The options each kind takes, by the name of their destination.
_TAKES: dict[str, frozenset[str]] = {
    "issue-implement": frozenset({"issue"}),
    "operational": frozenset({"issue"}),
    "upstream-report": frozenset({"issue"}),
    "design-review": frozenset({"issue", "round"}),
    "hoshin": frozenset({"repo", "date", "mode"}),
}

#: The options that take a value, as ``(destination, flag)``, in report order.
_VALUED = (
    ("issue", "--issue"),
    ("round", "--round"),
    ("repo", "--repo"),
    ("date", "--date"),
    ("mode", "--mode"),
)

_REPO_PATTERN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _check_options(parser, args)
    try:
        path = _ensure(args)
    except req.RequestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: git failed (exit {exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        return EXIT_ERROR
    except (BuildDirError, StateDirError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(path)
    return EXIT_OK


def _ensure(args: argparse.Namespace) -> Path:
    """Check the clone, then create and check the directory of *args*' kind."""
    if args.kind == "hoshin":
        owner, repo = args.repo.split("/", 1)
        req.check_clone_is_repo(owner, repo, command=_COMMAND, what="the repo named by --repo")
        return ensure_hoshin_dir(git.main_worktree_root(), args.date, args.mode)
    issue = req.parse_task_issue(args.issue)
    req.check_clone_is_issue_repo(issue, command=_COMMAND)
    if args.kind == "design-review":
        return ensure_design_review_dir(git.main_worktree_root(), issue.number, args.round)
    return ensure_task_dir(git.main_worktree_root(), args.kind, issue.number)


def _check_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse, as a usage error, an option the kind does not take or one it lacks."""
    takes = _TAKES[args.kind]
    for dest, flag in _VALUED:
        given = getattr(args, dest) is not None
        if dest in takes and not given:
            parser.error(f"--kind {args.kind} requires {flag}")
        if dest not in takes and given:
            parser.error(f"{flag} does not apply to --kind {args.kind}")


def _valid(value: str, check: Callable[[str], None]) -> str:
    """Return *value* when the library's *check* accepts it, else a usage error."""
    try:
        check(value)
    except StateDirError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _round_arg(value: str) -> str:
    return _valid(value, check_round_id)


def _date_arg(value: str) -> str:
    return _valid(value, check_hoshin_date)


def _repo_arg(value: str) -> str:
    if _REPO_PATTERN.fullmatch(value) is None:
        msg = f"must be owner/name, not {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_COMMAND,
        description="Create and check a skill's private state directory, and print its path.",
    )
    parser.add_argument("--kind", required=True, choices=_KINDS, help="the directory's kind")
    parser.add_argument(
        "--issue",
        help="the task issue, owner/name#N (design-review: the planning issue, owner/name#M)",
    )
    parser.add_argument("--round", type=_round_arg, help="the design-review round, e.g. S1")
    parser.add_argument("--repo", type=_repo_arg, help="the repository, owner/name (hoshin)")
    parser.add_argument("--date", type=_date_arg, help="the hoshin date, YYYY-MM-DD")
    parser.add_argument("--mode", choices=HOSHIN_MODES, help="the hoshin mode, start or end")
    parser.add_argument(
        "--ensure",
        action="store_true",
        required=True,
        help="create absent levels mode 700, check every level, and print the path",
    )
    return parser


if __name__ == "__main__":
    sys.exit(main())
