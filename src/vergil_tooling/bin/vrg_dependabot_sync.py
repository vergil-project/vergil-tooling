"""``vrg-dependabot-sync`` — single-repo report-only ``dependabot.yml`` applicator.

Owns exactly the file change for **one** repository, and nothing else: it reads
``<repo>/.github/dependabot.yml``, resolves the repo's primary language from
``[project].primary-language``, and either checks (default) or writes the
canonical report-only render from :mod:`vergil_tooling.lib.dependabot`
(epic vergil-project/.github#420). A ``vergil.toml`` that fails to load is a
config error (exit 2), never an actions-only render (#3451).

The file is **exact match**: ``--write`` replaces any local additions with the
canonical render. It has no git or PR knowledge — ``vrg-fleet-sync --target
dependabot`` shells out to it once per repo, and everything git-shaped lives
there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib import dependabot
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    try:
        lang = dependabot.resolve_language(repo_root)
    except ConfigError as exc:
        # A config that will not load is not drift: report it and exit 2 so no
        # language-less file is written in its place (#3451).
        print(f"vrg-dependabot-sync: {exc}", file=sys.stderr)
        return 2
    if args.write:
        return _run_write(repo_root, lang)
    return _run_check(repo_root, lang)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-dependabot-sync",
        description=(
            "Check or write a single repository's canonical report-only "
            ".github/dependabot.yml (every entry open-pull-requests-limit: 0). "
            "The ecosystems follow the repo's [project].primary-language. The "
            "file is exact match: --write replaces local additions. This is "
            "the applicator: it owns only the file change and has no git or "
            "PR knowledge."
        ),
        epilog=(
            "Exit codes: 0 ok, 1 --check drift, 2 usage error or a vergil.toml that fails to load."
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Repository root (default: current working directory).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Report compliance without modifying the file (default).",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="Write the canonical .github/dependabot.yml.",
    )
    return parser.parse_args(argv)


def _read(repo_root: Path) -> str | None:
    """Return the repo's ``dependabot.yml`` text, or ``None`` when absent."""
    path = repo_root / dependabot.DEPENDABOT_PATH
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _run_check(repo_root: Path, lang: str | None) -> int:
    """Report whether the repo's ``dependabot.yml`` is canonical; 0 ok, 1 drift."""
    reasons = dependabot.check(_read(repo_root), lang)
    for reason in reasons:
        print(reason, file=sys.stderr)
    return 1 if reasons else 0


def _run_write(repo_root: Path, lang: str | None) -> int:
    """Write the canonical ``dependabot.yml``. Always exits 0."""
    if not dependabot.check(_read(repo_root), lang):
        print("already in sync")
        return 0
    path = repo_root / dependabot.DEPENDABOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dependabot.render(lang), encoding="utf-8")
    print(f"wrote {dependabot.DEPENDABOT_PATH.as_posix()} ({lang or 'none'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
