"""``vrg-hook-guard-shim-sync`` — single-repo managed ``.claude/hooks/guard.sh`` applicator.

Owns exactly the file change for **one** repository, and nothing else: it reads
``<repo>/.claude/hooks/guard.sh`` and either checks (default) or writes the
packaged hook guard shim as returned by
:func:`vergil_tooling.lib.repo_config.render_hook_guard_shim`, the same shim
``vrg-github-repo-init`` writes (#4109).

The file is **fully managed**: the config audit (``_check_hook_guard_shim``)
requires it to match the packaged shim byte for byte, so a stale copy or a
local edit is drift and ``--write`` replaces the whole file. The shim must also
be executable, or Claude Code cannot run it as a hook, so ``--write`` leaves it
mode ``0755`` and a non-executable copy is drift too. A file that already
matches and is executable is in sync and is left untouched, so ``--check``
drifts exactly when ``--write`` would change the file. A repo with no shim gets
one. A shim that cannot be read is an error (exit 2), never a pass. It has no
git or PR knowledge — ``vrg-fleet-sync --target hook-guard-shim`` shells out to
it once per repo, and everything git-shaped lives there.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.repo_config import HOOK_GUARD_SHIM_PATH, render_hook_guard_shim

SHIM_PATH = Path(HOOK_GUARD_SHIM_PATH)

#: The mode ``--write`` leaves the shim at.
SHIM_MODE = 0o755

#: Every execute bit; a shim missing any of them is drift.
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    try:
        if args.write:
            return _run_write(repo_root)
        return _run_check(repo_root)
    except ShimReadError as exc:
        # Exit 2, not 1: under --check, 1 means drift, and an unreadable
        # shim must be an error for the sweep, never a "would sync".
        print(f"vrg-hook-guard-shim-sync: {exc}", file=sys.stderr)
        return 2


class ShimReadError(Exception):
    """The repo's shim exists but cannot be read."""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-hook-guard-shim-sync",
        description=(
            "Check or write a single repository's managed .claude/hooks/guard.sh. "
            "The file is fully managed: it must match the packaged hook guard shim "
            "byte for byte and be executable, and --write replaces the whole file "
            "with the packaged shim, mode 0755. This is the applicator: it owns only "
            "the file change and has no git or PR knowledge."
        ),
        epilog="Exit codes: 0 ok, 1 --check drift, 2 usage error or an unreadable shim.",
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
        help="Write the packaged hook guard shim to .claude/hooks/guard.sh, mode 0755.",
    )
    return parser.parse_args(argv)


def _drift(repo_root: Path) -> str | None:
    """Return why the repo's shim is not the managed one, or ``None`` when it is.

    Raises :class:`ShimReadError` when the shim exists but cannot be read.
    """
    path = repo_root / SHIM_PATH
    if not path.is_file():
        return f"missing {SHIM_PATH.as_posix()}"
    try:
        current = path.read_bytes()
    except OSError as exc:
        msg = f"cannot read {SHIM_PATH.as_posix()}: {exc}"
        raise ShimReadError(msg) from exc
    if current != render_hook_guard_shim().encode("utf-8"):
        return f"{SHIM_PATH.as_posix()} differs from the packaged shim"
    if path.stat().st_mode & _EXEC_BITS != _EXEC_BITS:
        return f"{SHIM_PATH.as_posix()} is not executable"
    return None


def _run_check(repo_root: Path) -> int:
    """Report whether the repo's shim is the managed one; 0 ok, 1 drift."""
    reason = _drift(repo_root)
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 1


def _run_write(repo_root: Path) -> int:
    """Replace the shim with the packaged one, mode 0755. Exits 0."""
    if _drift(repo_root) is None:
        print("already in sync")
        return 0
    path = repo_root / SHIM_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, render_hook_guard_shim().encode("utf-8"))
    print(f"wrote {SHIM_PATH.as_posix()}")
    return 0


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file in the same directory and a rename.

    The bytes are written as they are, with no newline translation, and the
    temp file is given mode ``0755`` before the rename, so the shim is never
    in place without its execute bits.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp.chmod(SHIM_MODE)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
