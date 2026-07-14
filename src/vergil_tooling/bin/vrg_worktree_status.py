"""List canonical ``.worktrees/`` worktrees with their lifecycle state.

Read-only observability for worktree hygiene: shows which worktrees are
removable cruft (merged/closed PRs whose worktree was never cleaned up)
versus legitimate in-flight work, so the cruft is obvious at a glance.

Cleanup stays ``vrg-finalize-pr``'s job — this command only observes
(issue #1552). PR state is queried from GitHub (one call per worktree)
for an authoritative merged/closed verdict; a failed lookup is shown as
``unknown`` with the reason rather than silently downgraded.
"""

from __future__ import annotations

import argparse
import datetime
import sys

from vergil_tooling.lib import git, worktrees
from vergil_tooling.lib.worktrees import WorktreeState, WorktreeStatus

# Live work first, cruft last, so the removable rows group at the bottom.
_SORT_RANK = {
    WorktreeState.OPEN_PR: 0,
    WorktreeState.NO_PR: 1,
    WorktreeState.DRAFT: 2,
    WorktreeState.UNKNOWN: 3,
    WorktreeState.MERGED: 4,
    WorktreeState.CLOSED: 5,
}

_COLUMNS = (
    "WORKTREE",
    "BRANCH",
    "PR",
    "STATE",
    "WORKFLOW",
    "AHEAD",
    "DIRTY",
    "LAST COMMIT",
    "LAST MODIFIED",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List canonical worktrees with their lifecycle state.",
    )
    parser.add_argument(
        "--target-branch",
        default="develop",
        help="Branch to measure commits-ahead against (default: develop).",
    )
    return parser.parse_args(argv)


def _workflow_cell(status: WorktreeStatus) -> str:
    """Render the pr-workflow prep signal: 'unknown' on a read error, the raw
    status verbatim when the file loaded, '-' when there is no file yet."""
    if status.workflow_error is not None:
        return "unknown"
    return status.workflow_status if status.workflow_status is not None else "-"


def _format_age(ts: float | None, now: float) -> str:
    """Render *ts* (epoch seconds) as a relative age: '2h ago' / '3d ago'.

    ``None`` renders '-'. A future timestamp (clock skew, or a commit dated
    just ahead of *now*) clamps to '0h ago' rather than a negative age.
    """
    if ts is None:
        return "-"
    elapsed = max(0.0, now - ts)
    days = elapsed / 86400.0
    if days < 1:
        return f"{int(elapsed // 3600)}h ago"
    return f"{int(days)}d ago"


def _row(status: WorktreeStatus, now: float) -> tuple[str, ...]:
    pr = f"#{status.pr_number}" if status.pr_number is not None else "-"
    return (
        status.worktree.path.name,
        status.worktree.branch,
        pr,
        status.state.value,
        _workflow_cell(status),
        str(status.ahead),
        "yes" if status.dirty else "-",
        _format_age(status.last_commit_ts, now),
        _format_age(status.last_modified_ts, now),
    )


def _render_table(rows: list[tuple[str, ...]]) -> str:
    cells = [_COLUMNS, *rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(row[i].ljust(widths[i]) for i in range(len(_COLUMNS))).rstrip() for row in cells
    )


def _summary(statuses: list[WorktreeStatus]) -> str:
    total = len(statuses)
    cruft = sum(1 for s in statuses if s.removable)
    attention = sum(1 for s in statuses if s.needs_attention)
    # A reused-branch worktree lands as NO_PR *and* needs_attention; count it
    # only under attention so the buckets stay disjoint and sum to total.
    stalled = sum(1 for s in statuses if s.state is WorktreeState.NO_PR and not s.needs_attention)
    prepared = sum(1 for s in statuses if s.pr_prepared)
    # Finished-but-stuck worktrees (needs_attention) are pulled out of active:
    # never let a merged-but-dirty worktree pose as live work behind "0 cruft"
    # (issue #2347).
    active = total - cruft - stalled - attention
    line = (
        f"{total} worktrees — {active} active, "
        f"{attention} needs-attention, "
        f"{stalled} stalled (no-pr), {cruft} cruft (removable). "
        f"{prepared} PR prepared."
    )
    if attention:
        line += " Some worktrees need attention (see notes below)."
    if cruft:
        line += " Run vrg-finalize-pr to clean cruft."
    return line


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = git.repo_root()
    statuses = [
        worktrees.gather_worktree_status(wt, target=args.target_branch, with_freshness=True)
        for wt in worktrees.list_worktrees(root)
    ]
    if not statuses:
        print("No canonical .worktrees/ worktrees found.")
        return 0
    statuses.sort(key=lambda s: (_SORT_RANK[s.state], s.worktree.branch))
    now = datetime.datetime.now(tz=datetime.UTC).timestamp()
    print(_render_table([_row(s, now) for s in statuses]))
    print()
    print(_summary(statuses))
    # Surface any captured detail so neither a failed lookup (UNKNOWN) nor a
    # reused-branch-name mismatch (issue #1719) is silently hidden. An
    # unreadable pr-workflow file (the WORKFLOW 'unknown' cell) gets its reason
    # surfaced too, never a silent failure.
    for status in statuses:
        if status.detail:
            print(f"  note: {status.worktree.branch}: {status.detail}")
        if status.workflow_error:
            print(
                f"  note: {status.worktree.branch}: pr-workflow unreadable: {status.workflow_error}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
