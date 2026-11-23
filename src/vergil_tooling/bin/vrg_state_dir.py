"""Create and check a task's private state directory (doctrine §1.5).

``vrg-state-dir --kind <kind> --issue <owner/name#N> --ensure`` checks
``build/.vergil-state/<kind>/<N>/`` in the main clone level by level and
prints its absolute path. ``<kind>`` is ``operational``, ``issue-implement``
or ``upstream-report``. ``build`` must be a real directory (it is created
with a plain mkdir when absent); each level from ``.vergil-state`` down to
``<N>`` must be a real directory, not a symlink, with mode exactly 700, and
an absent level is created mode 700. A level that fails stops the command
before anything is created below it. The caller runs it before every write
it makes there and writes only to the path it prints.

Before it creates or checks anything it refuses unless the current clone's
``origin`` is the ``--issue`` repository, as ``vrg-merge-review-request``
does: the directory is resolved from the current directory's main clone, so
a run from another repository's clone would put it where no later run looks.

It makes the checks ``vrg-merge-review-request --ensure-dir`` makes for
``merge-review/``, through the same code (``lib/merge_review/state.py``).

Exit codes: ``0`` the directory is private and its path printed, ``1`` a
refusal (nothing on stdout, the reason on stderr), ``2`` a usage error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from vergil_tooling.lib import git
from vergil_tooling.lib.build_dir import BuildDirError
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.merge_review import request as req
from vergil_tooling.lib.merge_review.state import TASK_KINDS, StateDirError, ensure_task_dir

EXIT_OK = 0
EXIT_ERROR = 1

_COMMAND = "vrg-state-dir"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        issue = req.parse_task_issue(args.issue)
        req.check_clone_is_issue_repo(issue, command=_COMMAND)
        path = ensure_task_dir(git.main_worktree_root(), args.kind, issue.number)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_COMMAND,
        description="Create and check a task's private state directory, and print its path.",
    )
    parser.add_argument("--kind", required=True, choices=TASK_KINDS, help="the directory's kind")
    parser.add_argument("--issue", required=True, help="the task issue, owner/name#N")
    parser.add_argument(
        "--ensure",
        action="store_true",
        required=True,
        help="create absent levels mode 700, check every level, and print the path",
    )
    return parser
