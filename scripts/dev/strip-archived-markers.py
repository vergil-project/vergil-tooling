#!/usr/bin/env python3
"""One-time cosmetic strip of legacy ``archived@`` session-name markers.

Task 7 (Stage D) of epic vergil-project/.github#230. The old ``vrg-vm`` archive
machinery renamed a stale session to ``archived@<timestamp>@<original-name>``;
that behavior is gone (Task 4), but any such *name* left in a real transcript is
now just an opaque string the seam still lists and resolves. It renders unevenly
next to the clean ``label:workspace`` names.

This script restores every such session's original name so the list renders
uniformly. It is:

- **idempotent** — a clean name never matches, so re-running is a no-op;
- **dry-run by default** — it only prints what it *would* do; pass ``--apply``
  to actually rename;
- **rename-only** — it goes through the supported ``SessionStore.rename`` seam
  and **never deletes** a session or a transcript.

It touches nothing but ``archived@``-prefixed names; every other session is left
exactly as it is.

Usage::

    # inside the dev container (see CLAUDE.md), or on a host/VM with real sessions
    python scripts/dev/strip-archived-markers.py            # dry run (default)
    python scripts/dev/strip-archived-markers.py --apply    # perform the renames
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from vergil_tooling.lib.session_store import ScrapeStore

if TYPE_CHECKING:
    from vergil_tooling.lib.session_store import SessionInfo, SessionStore

# The legacy archive label was ``archived@<timestamp>@<original-name>`` (see the
# retired ``make_archived_name`` in ``lib/session.py`` history). ``parse_archived``
# was deleted with the archive machinery in Task 4, so the minimal recovery of the
# original name is reproduced here — this script is the only remaining reader of the
# legacy form.
_ARCHIVED_PREFIX = "archived@"


def strip_archived_name(name: str) -> str | None:
    """Recover the original name from a legacy ``archived@<ts>@<orig>`` label.

    Splits on the **first two** ``@`` so an original name that itself contains
    ``@`` (a workspace path can) is recovered intact. Returns ``None`` for any
    name that is not a well-formed archive label — a clean name, a prefix with no
    original segment — which is what makes the strip idempotent: the recovered
    name no longer carries the prefix, so a second pass leaves it untouched.
    """
    if not name.startswith(_ARCHIVED_PREFIX):
        return None
    parts = name.split("@", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    return parts[2]


@dataclass(frozen=True)
class Strip:
    """One planned rename: restore ``session_id`` from ``old_name`` to ``new_name``."""

    session_id: str
    old_name: str
    new_name: str


def plan_strips(rows: list[SessionInfo]) -> list[Strip]:
    """Every archived-marked session row and the original name to restore it to.

    Rows with no name, or with a name that is not an ``archived@`` label, are
    skipped — only legacy markers are ever touched.
    """
    strips: list[Strip] = []
    for row in rows:
        if row.name is None:
            continue
        original = strip_archived_name(row.name)
        if original is not None:
            strips.append(Strip(row.session_id, row.name, original))
    return strips


def run(store: SessionStore, apply: bool, out: TextIO) -> int:
    """List (and, when ``apply``, perform) every ``archived@`` marker strip.

    Prints each planned/performed rename to ``out``. In dry-run mode nothing is
    renamed; with ``apply`` set each rename goes through ``store.rename`` (the
    supported seam). Returns ``0`` always — an empty plan is a clean success, not
    an error.
    """
    strips = plan_strips(store.list_sessions())
    if not strips:
        print("No legacy archived@ session names found; nothing to strip.", file=out)
        return 0

    verb = "Renaming" if apply else "Would rename"
    for item in strips:
        print(f"{verb}: {item.old_name}  ->  {item.new_name}  ({item.session_id})", file=out)
        if apply:
            store.rename(item.session_id, item.new_name)

    if apply:
        print(f"\nRenamed {len(strips)} session(s).", file=out)
    else:
        print(
            f"\nDry run: {len(strips)} session(s) would be renamed. "
            f"Re-run with --apply to execute.",
            file=out,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cosmetically strip legacy archived@ markers from session names "
        "(dry-run by default; never deletes).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the renames (default: dry run, print only).",
    )
    parser.add_argument(
        "--claude-dir",
        type=Path,
        default=Path.home() / ".claude",
        help="the ~/.claude directory to read/rewrite (default: ~/.claude).",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="restrict to one project slug (default: every slug).",
    )
    args = parser.parse_args(argv)

    store = ScrapeStore(args.claude_dir, args.slug)
    return run(store, apply=args.apply, out=sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
