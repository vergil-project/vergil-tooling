"""Print the epic/task drift audit (read-only safety net).

See :mod:`vergil_tooling.lib.epic_audit`. Surfaces work that slipped through
auto-close so a human can close it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from vergil_tooling.lib import epic_audit, github, identity_mode

_DEFAULT_WINDOW_DAYS = 30

# Set by the scheduled reconciliation workflow (ops-epic-sweep) to authorize the
# automated ``--close``. The sweep only closes provably-complete drift (epics
# whose children are all closed, tasks whose PR merged), so it is a trusted,
# deterministic automation — allowed to close even though it is not a human. An
# interactive agent session never sets this, so the human/agent gate stands.
_SWEEP_ENV = "VRG_EPIC_SWEEP"


def _automated_sweep() -> bool:
    """True when running as the trusted scheduled reconciliation sweep."""
    return os.environ.get(_SWEEP_ENV) == "1"


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        msg = "must be a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-epic-audit",
        description=(
            "Report epic/task drift and invariant violations for the current "
            "repo's GitHub org: merged PRs whose Ref'd task is still open, open "
            "non-perpetual epics whose children are all closed, and issues in "
            "the wrong place (epics outside .github; stray .github issues). "
            "Read-only by default; pass --close (as a human) to close the drift "
            "it finds (invariant violations are report-only)."
        ),
        epilog=(
            "Scope: the org is auto-detected from this repo's 'origin' remote, "
            "so run it from inside a repo in the org you want to audit (there is "
            "no --org flag). Output is Markdown on stdout."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=_positive_int,
        default=_DEFAULT_WINDOW_DAYS,
        metavar="N",
        help=(f"How many days back to scan for merged PRs (default: {_DEFAULT_WINDOW_DAYS})."),
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help=(
            "Close the drifted task issues and rolled-up epics (with an "
            "explanatory comment on each) instead of only reporting them. A "
            "human action (or the scheduled reconciliation sweep) — refused in "
            "interactive agent sessions. Default: read-only."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Gate the write path before any network work so a rejected agent run is
    # cheap and unambiguous. A human may close interactively; the scheduled
    # reconciliation sweep (ops-epic-sweep) closes via the _SWEEP_ENV signal.
    if args.close and not (identity_mode.is_human() or _automated_sweep()):
        print(
            "vrg-epic-audit: --close is a human action and was refused in an "
            "interactive agent session; run without --close to preview the "
            "drift, or run as a human to apply the closes. (The scheduled sweep "
            f"authorizes automated closes via ${_SWEEP_ENV}.)",
            file=sys.stderr,
        )
        return 1
    org = github.detect_org()
    if org is None:
        print(
            "vrg-epic-audit: could not determine the GitHub org from this "
            "repo's 'origin' remote; run it from inside a repo in the org you "
            "want to audit.",
            file=sys.stderr,
        )
        return 1
    since = (datetime.now(UTC) - timedelta(days=args.window_days)).date().isoformat()
    tasks = epic_audit.task_drift(since, org=org)
    epics = epic_audit.epic_drift()
    if args.close:
        closed = epic_audit.close_drift(tasks, epics, org=org)
        print(epic_audit.render_closed(closed, org=org, window_days=args.window_days))
        return 0
    epics_outside = epic_audit.epic_outside_dotgithub(org)
    stray = epic_audit.stray_dotgithub_issue(org)
    pending_operational = epic_audit.operational_pending(org)
    closed_operational_no_success = epic_audit.closed_operational_without_success(org)
    print(
        epic_audit.render(
            tasks,
            epics,
            org=org,
            window_days=args.window_days,
            epics_outside=epics_outside,
            stray=stray,
            pending_operational=pending_operational,
            closed_operational_no_success=closed_operational_no_success,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
