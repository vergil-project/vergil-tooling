"""``vrg-claude-md-sync`` — single-repo ``CLAUDE.md`` template-region applicator.

Owns exactly the file change for **one** repository, and nothing else: it reads
``<repo>/CLAUDE.md`` and either checks (default) or writes the region between
the ``vergil:template:claude-md`` markers, which must carry the packaged
``claude_md_consumer.md`` template verbatim (#3868).

Only the marked region is managed. ``--write`` replaces the lines between the
two marker lines with the template, verbatim, and leaves every byte outside
them alone, the marker lines included. Drift is the config audit's own test
(:func:`vergil_tooling.lib.repo_config.claude_md_region_divergence`, built on
the audit's marker parser), so ``--check`` drifts exactly when ``--write``
would change the file, and after ``--write`` the audit's ``_check_claude_md``
passes. A region that matches apart from blank lines next to the markers is in
sync and is left untouched.

A ``CLAUDE.md`` that is missing, has no markers, or has malformed or duplicated
markers cannot be synced without guessing where the region belongs, so it is an
error (exit 2) that names the cause, in both modes, and nothing is written. It
has no git or PR knowledge — ``vrg-fleet-sync --target claude-md`` shells out
to it once per repo, and everything git-shaped lives there.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.repo_config import (
    ClaudeMdMarkerError,
    claude_md_marker_lines,
    claude_md_region_divergence,
    load_claude_md_template,
)

CLAUDE_MD_PATH = Path("CLAUDE.md")

_PROG = "vrg-claude-md-sync"


class _CannotSyncError(Exception):
    """A CLAUDE.md whose template region cannot be located."""


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    try:
        if args.write:
            return _run_write(repo_root)
        return _run_check(repo_root)
    except _CannotSyncError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 2


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Check or write the template region of a single repository's CLAUDE.md. "
            "Only the lines between the vergil:template:claude-md markers are "
            "managed: --write replaces them with the packaged template verbatim "
            "and leaves everything outside the markers untouched. This is the "
            "applicator: it owns only the file change and has no git or PR knowledge."
        ),
        epilog=(
            "Exit codes: 0 ok, 1 --check drift, 2 usage error or a CLAUDE.md "
            "that is missing or has no well-formed marker pair."
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
        help="Replace the marked template region with the packaged template.",
    )
    return parser.parse_args(argv)


@dataclass(frozen=True)
class _ClaudeMd:
    """A parsed CLAUDE.md: its lines with and without line ends, and its markers."""

    raw_lines: list[str]
    lines: list[str]
    begin: int
    end: int


def _read(repo_root: Path) -> _ClaudeMd:
    """Read and parse the repo's CLAUDE.md with the audit's marker parser.

    The file is decoded from bytes, never read in text mode, so its line ends
    survive unchanged; ``splitlines`` with and without ``keepends`` split at
    the same places, so a marker index is valid in both line lists. Raises
    :class:`_CannotSyncError` naming the cause when the file is missing or its
    markers are absent or malformed.
    """
    path = repo_root / CLAUDE_MD_PATH
    if not path.is_file():
        msg = f"cannot sync: missing {CLAUDE_MD_PATH.as_posix()}"
        raise _CannotSyncError(msg)
    text = path.read_bytes().decode("utf-8")
    lines = text.splitlines()
    try:
        begin, end = claude_md_marker_lines(lines)
    except ClaudeMdMarkerError as exc:
        msg = f"cannot sync: {CLAUDE_MD_PATH.as_posix()} has {exc}"
        raise _CannotSyncError(msg) from exc
    return _ClaudeMd(text.splitlines(keepends=True), lines, begin, end)


def _drift(doc: _ClaudeMd) -> str | None:
    """Return why the marked region is not the template, or ``None`` when it is."""
    divergence = claude_md_region_divergence(doc.lines, doc.begin, doc.end)
    if divergence is None:
        return None
    return (
        f"{CLAUDE_MD_PATH.as_posix()} template region differs from the template "
        f"(expected {divergence.expected}; found {divergence.actual})"
    )


def _run_check(repo_root: Path) -> int:
    """Report whether the marked region equals the template; 0 ok, 1 drift."""
    reason = _drift(_read(repo_root))
    if reason is None:
        return 0
    print(reason, file=sys.stderr)
    return 1


def _run_write(repo_root: Path) -> int:
    """Replace the marked region with the template. Exits 0 unless it cannot sync."""
    doc = _read(repo_root)
    raw_lines, begin, end = doc.raw_lines, doc.begin, doc.end
    if _drift(doc) is None:
        print("already in sync")
        return 0
    template = load_claude_md_template()
    if not template.endswith("\n"):
        template += "\n"
    new_text = "".join(raw_lines[: begin + 1]) + template + "".join(raw_lines[end:])
    _atomic_write(repo_root / CLAUDE_MD_PATH, new_text.encode("utf-8"))
    print(f"wrote the {CLAUDE_MD_PATH.as_posix()} template region")
    return 0


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file in the same directory and a rename.

    ``mkstemp`` creates the temp file ``0600``; it is given the replaced file's
    mode, so the write never narrows permissions.
    """
    mode = path.stat().st_mode & 0o777
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp.chmod(mode)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    sys.exit(main())
