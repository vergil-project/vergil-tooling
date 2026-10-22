"""Sweep finished branches (merged or closed PR) left on a GitHub remote.

The safety net behind the delete-on-merge repo setting and the delete-on-close
rule (issues #3332, #3333): lists every branch of the target repo and reports,
or with ``--apply`` deletes, the ones whose PR work is finished. The rules live
in :mod:`vergil_tooling.lib.branch_sweep`.

Dry-run by default. Prints one line per branch and a closing summary; any
per-branch error makes the run exit 1 after every branch has been processed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from vergil_tooling.lib import branch_sweep, github
from vergil_tooling.lib.branch_sweep import Action
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.process_errors import error_text

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo or github.current_repo()
    owner = repo.split("/", 1)[0]
    mode = "apply" if args.apply else "dry run"
    print(f"Sweeping finished branches in {repo} ({mode})")
    try:
        with github.target_org(owner):
            outcomes = branch_sweep.sweep(repo, apply=args.apply)
    except github.MissingGitHubTokenError:
        raise
    except (subprocess.CalledProcessError, ValueError) as exc:
        detail = " ".join(error_text(exc).split())
        print(f"vrg-branch-sweep: cannot list branches of {repo}: {detail}", file=sys.stderr)
        return 1
    for line in branch_sweep.format_outcomes(outcomes):
        print(line)
    print()
    print(branch_sweep.summarize(repo, outcomes, apply=args.apply))
    errors = [o for o in outcomes if o.action is Action.ERROR]
    if errors:
        names = ", ".join(o.branch for o in errors)
        print(f"vrg-branch-sweep: {len(errors)} branch(es) failed: {names}", file=sys.stderr)
        return 1
    return 0


def _repo_arg(value: str) -> str:
    if not _REPO_RE.match(value):
        msg = f"expected OWNER/NAME, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-branch-sweep",
        description=(
            "Find branches on a GitHub repo whose PR work is finished (newest PR "
            "merged or closed, none open, tip unchanged since that PR) and delete "
            "them with --apply. Never deletes the default branch, develop, main, "
            "gh-pages, a protected branch, or a branch with no PR. Dry-run by default."
        ),
    )
    parser.add_argument(
        "--repo",
        type=_repo_arg,
        help="Target repository as OWNER/NAME (default: the current directory's repo)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the finished branches (default: report what would be deleted)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
