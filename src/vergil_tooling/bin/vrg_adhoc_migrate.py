"""Relocate per-repo standing epics into the org ``.github`` (one-shot, epic #85).

Dry-run by default: prints the planned relocations, changing nothing. ``--apply``
executes them (reparent each standing epic's open children under the new
``.github`` ad-hoc epic, then close the standing epic) — a human action, refused
in agent sessions, mirroring ``vrg-epic-audit --close``.

See :mod:`vergil_tooling.lib.adhoc_migrate`.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import adhoc_migrate, github, identity_mode


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-adhoc-migrate",
        description=(
            "Relocate per-repo standing epics into the org .github (the ad-hoc "
            "model, epic #85): re-link open children under the new .github ad-hoc "
            "epic and close the old standing epic. Dry-run by default; pass "
            "--apply (as a human) to execute. The org is auto-detected from this "
            "repo's 'origin' remote."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Execute the relocations (a human action — refused in agent "
            "sessions). Default: read-only dry-run preview."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Gate the write path before any network work, mirroring vrg-epic-audit
    # --close: relocating and closing epics is a human action.
    if args.apply and not identity_mode.is_human():
        print(
            "vrg-adhoc-migrate: --apply is a human action and was refused in an "
            "agent session; run without --apply to preview the migration, or run "
            "as a human to apply it.",
            file=sys.stderr,
        )
        return 1
    org = github.detect_org()
    if org is None:
        print(
            "vrg-adhoc-migrate: could not determine the GitHub org from this "
            "repo's 'origin' remote; run it from inside a repo in the org to "
            "migrate.",
            file=sys.stderr,
        )
        return 1
    relocations = adhoc_migrate.plan(org)
    if not args.apply:
        print(adhoc_migrate.render_plan(relocations, org=org))
        return 0
    summaries = [adhoc_migrate.apply_one(reloc) for reloc in relocations]
    print(adhoc_migrate.render_applied(summaries, org=org))
    return 0


if __name__ == "__main__":
    sys.exit(main())
