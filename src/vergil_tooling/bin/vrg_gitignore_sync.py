"""``vrg-gitignore-sync`` — single-repo ``.gitignore`` managed-block applicator.

Owns exactly the file change for **one** repository, and nothing else: it reads
``<repo>/.gitignore``, resolves the repo's fence language from
``[project].primary-language``, and either checks (default) or writes the
vergil-managed block via :mod:`vergil_tooling.lib.gitignore`.

It has no git or PR knowledge — the fleet driver (epic vergil-project/.github#325,
Task 7) shells out to it once per repo, and everything git-shaped lives there.

The fence language is resolved with the *same* rule the ``.gitignore`` audit
uses (:func:`vergil_tooling.lib.repo_config._gitignore_language`): the asserted
``[project].primary-language``, normalized to ``None`` (base-only) for any
language without a managed fragment or when the config is absent/unreadable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib import gitignore
from vergil_tooling.lib.repo_config import _gitignore_language


def _read_gitignore(repo_root: Path) -> str:
    """Return ``<repo>/.gitignore`` text, or ``""`` when the file is absent."""
    path = repo_root / ".gitignore"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _lang_label(lang: str | None) -> str:
    """Human-readable language name for summaries (``base-only`` for ``None``)."""
    return lang if lang else "base-only"


def _run_check(repo_root: Path, lang: str | None) -> int:
    """Report whether the repo's ``.gitignore`` is compliant; 0 ok, 1 drift."""
    compliance = gitignore.check(_read_gitignore(repo_root), lang)
    if compliance.compliant:
        return 0
    for reason in compliance.reasons:
        print(reason, file=sys.stderr)
    return 1


def _run_write(repo_root: Path, lang: str | None) -> int:
    """Sync the managed block into the repo's ``.gitignore``. Always exits 0."""
    result = gitignore.sync(_read_gitignore(repo_root), lang)
    if not result.changed:
        print("already in sync")
        return 0
    (repo_root / ".gitignore").write_text(result.text, encoding="utf-8")
    if result.removed:
        print(
            f"dropped {len(result.removed)} line(s) matching other-language "
            f"fragments; this repo is {_lang_label(lang)}"
        )
    print(f"synced managed block in {repo_root / '.gitignore'}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-gitignore-sync",
        description=(
            "Check or write the vergil-managed block in a single repository's "
            ".gitignore. The fence language is the repo's asserted "
            "[project].primary-language, normalized to base-only for any "
            "language without a managed fragment. This is the applicator: it "
            "owns only the file change and has no git or PR knowledge."
        ),
        epilog="Exit codes: 0 ok, 1 --check drift, 2 usage error.",
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
        help="Write the managed block into .gitignore.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    lang = _gitignore_language(repo_root)
    if args.write:
        return _run_write(repo_root, lang)
    return _run_check(repo_root, lang)


if __name__ == "__main__":
    sys.exit(main())
