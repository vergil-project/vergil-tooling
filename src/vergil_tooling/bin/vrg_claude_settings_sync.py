"""``vrg-claude-settings-sync`` — single-repo managed ``.claude/settings.json`` applicator.

Owns exactly the file change for **one** repository, and nothing else: it reads
``<repo>/.claude/settings.json`` and either checks (default) or writes the
packaged template as rendered by
:func:`vergil_tooling.lib.repo_init.render_claude_settings`, the same render
``vrg-github-repo-init`` writes (#4056).

The file is **fully managed**: the config audit (``_check_claude_settings``)
requires it to equal the template after JSON parsing, so ``--check`` uses that
same comparison and ``--write`` replaces the whole file (it never merges keys).
A file that already equals the template after parsing is in sync and is left
untouched, so ``--check`` drifts exactly when ``--write`` would change the file.
A repo with no ``.claude/settings.json`` gets one. It has no git or PR
knowledge — ``vrg-fleet-sync --target claude-settings`` shells out to it once
per repo, and everything git-shaped lives there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.repo_init import render_claude_settings

SETTINGS_PATH = Path(".claude") / "settings.json"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    if args.write:
        return _run_write(repo_root)
    return _run_check(repo_root)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-claude-settings-sync",
        description=(
            "Check or write a single repository's managed .claude/settings.json. "
            "The file is fully managed: it must equal the packaged template after "
            "JSON parsing, and --write replaces the whole file with the template. "
            "This is the applicator: it owns only the file change and has no git "
            "or PR knowledge."
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
        help="Write the managed .claude/settings.json from the template.",
    )
    return parser.parse_args(argv)


def _drift(repo_root: Path) -> str | None:
    """Return why the repo's settings file is not the template, or ``None`` when it is.

    The comparison is the audit's: equal after JSON parsing, so key order and
    whitespace never count as drift.
    """
    path = repo_root / SETTINGS_PATH
    if not path.is_file():
        return f"missing {SETTINGS_PATH.as_posix()}"
    text = path.read_text(encoding="utf-8")
    try:
        current = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"{SETTINGS_PATH.as_posix()} is not valid JSON ({exc})"
    if current != json.loads(render_claude_settings()):
        return f"{SETTINGS_PATH.as_posix()} differs from the template"
    return None


def _run_check(repo_root: Path) -> int:
    """Report whether the repo's settings file equals the template; 0 ok, 1 drift."""
    reason = _drift(repo_root)
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 1


def _run_write(repo_root: Path) -> int:
    """Replace the settings file with the template render. Always exits 0."""
    if _drift(repo_root) is None:
        print("already in sync")
        return 0
    path = repo_root / SETTINGS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, render_claude_settings())
    print(f"wrote {SETTINGS_PATH.as_posix()}")
    return 0


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same directory and ``os.replace``.

    ``mkstemp`` creates the temp file ``0600``; it is given the replaced file's
    mode, or ``0644`` for a new file, so the write never narrows permissions.
    """
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
