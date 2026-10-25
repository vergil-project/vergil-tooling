"""Run the repo's ``[upstream-refresh].hook`` and open, refresh or close one PR.

Language-neutral (epic vergil-project/.github#472, spec §5.6): the repo's hook
refreshes its upstream inputs by editing tracked files, and this command turns
the result into exactly one ``upstream-refresh`` PR, or none. It never merges,
and never runs dependency updaters, validation or a release. It runs from the
scheduled ``ops-upstream-refresh`` workflow, or by hand as a human; agent
identity modes are refused because it opens PRs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import git, github, identity_mode
from vergil_tooling.lib.branch_sweep import default_branch
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError, read_config
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.upstream_refresh import (
    REPORT_ENV,
    UpstreamRefreshError,
    changed_paths,
    ensure_clean_on_base,
    render_body,
    run_hook,
    sync,
)

_PROG = "vrg-upstream-refresh"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if identity_mode.is_agent():
        print(f"{_PROG} opens PRs and is not run by agents", file=sys.stderr)
        return 1
    try:
        return _refresh(args)
    except (UpstreamRefreshError, ConfigError) as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"{_PROG}: command failed: {error_text(exc)}", file=sys.stderr)
        return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Run the repo's [upstream-refresh].hook on a clean checkout of the base "
            "branch, then open, refresh or close the single upstream-refresh PR. "
            "Never merges."
        ),
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="Target repository (default: $GITHUB_REPOSITORY, then the origin remote).",
    )
    parser.add_argument(
        "--base",
        metavar="BRANCH",
        help="Base branch for the refresh PR (default: the repository's default branch).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the hook and print the PR body; push nothing and change no PR.",
    )
    return parser.parse_args(argv)


def _resolve_repo(arg: str | None) -> str:
    return arg or os.environ.get("GITHUB_REPOSITORY") or github.current_repo()


def _refresh(args: argparse.Namespace) -> int:
    repo_root = git.repo_root()
    cfg = read_config(repo_root)
    if cfg.upstream_refresh is None:
        print(f"{_PROG}: vergil.toml has no [upstream-refresh] section", file=sys.stderr)
        return 1
    repo = _resolve_repo(args.repo)
    base = args.base or default_branch(repo)
    ensure_clean_on_base(repo_root, base)
    with tempfile.TemporaryDirectory(prefix="vrg-upstream-refresh-") as tmp:
        report = Path(tmp) / "report.txt"
        print(f"Running [upstream-refresh].hook ({REPORT_ENV}={report}).")
        notes = run_hook(repo_root, cfg.upstream_refresh.hook, report)
    paths = changed_paths(repo_root)
    if args.dry_run:
        if paths:
            print(f"Dry run: would open or refresh the refresh PR on {repo} ({base}):\n")
            print(render_body(notes, paths))
        else:
            print(f"Dry run: nothing to refresh on {base}; would close any open refresh PR.")
        return 0
    outcome = sync(repo, base, repo_root, notes, paths)
    print(f"{_PROG}: {outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
