"""Show every worktree in the repo, grouped by kind, with its lifecycle state.

Read-only observability for worktree hygiene. Everything with code on disk in
this repo is shown, in four sections (issue #3386):

1. **Main checkout** — always shown; flagged when it is not on the repo's
   integration branch, is dirty, or is mid-rebase.
2. **Development** — issue-branch worktrees under ``.worktrees/``
   (``feature/``, ``bugfix/``, ``hotfix/`` or ``chore/`` + ``<N>-<slug>``),
   showing which are removable cruft (merged/closed PRs whose worktree was
   never cleaned up) versus in-flight work.
3. **Release and dependency machinery** — ``release/*`` and ``deps/update-*``
   worktrees, which are meant to be single-run, so a leftover stands out.
4. **Other / unknown** — any other branch, a detached HEAD, or a worktree
   outside ``.worktrees/``.

Cleanup stays ``vrg-finalize-pr``'s job — this command only observes
(issue #1552). PR state is queried from GitHub (one call per worktree)
for an authoritative merged/closed verdict; a failed lookup is shown as
``unknown`` with the reason rather than silently downgraded.
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, worktrees
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.worktrees import (
    MainCheckoutStatus,
    RawWorktree,
    WorktreeKind,
    WorktreeState,
    WorktreeStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

# Live work first, cruft last, so the removable rows group at the bottom.
# In-flight/mid-operation worktrees (rebasing, detached) sort near the top —
# they are active work needing a human, never removable cruft (issue #2634).
_SORT_RANK = {
    WorktreeState.OPEN_PR: 0,
    WorktreeState.REBASING: 1,
    WorktreeState.DETACHED: 2,
    WorktreeState.NO_PR: 3,
    WorktreeState.DRAFT: 4,
    WorktreeState.UNKNOWN: 5,
    WorktreeState.MERGED: 6,
    WorktreeState.CLOSED: 7,
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

_MAIN_COLUMNS = ("CHECKOUT", "BRANCH", "DIRTY", "LAST COMMIT", "STATUS")

# Section titles for the linked worktrees, in display order.
_SECTION_TITLES = {
    WorktreeKind.DEVELOPMENT: "Development",
    WorktreeKind.MACHINERY: "Release and dependency machinery",
    WorktreeKind.OTHER: "Other / unknown",
}

# The branch gather_detached_status reports when it cannot recover one.
_DETACHED = "(detached)"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = worktrees.discover_repo_worktrees()
    root = repo.main.path
    try:
        integration = config.integration_branch(root)
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    integration_note = None
    if integration is None:
        integration = "develop"
        integration_note = f"no {config.CONFIG_FILE}; integration branch defaults to {integration}"
    main_status = worktrees.gather_main_checkout_status(repo.main, integration=integration)

    sections: dict[WorktreeKind, list[WorktreeStatus]] = {kind: [] for kind in WorktreeKind}
    vanished: list[RawWorktree] = []
    for raw in repo.linked:
        try:
            status = _gather(raw, target=args.target_branch)
        except subprocess.CalledProcessError:
            # Discovery is a snapshot: a concurrent operation (e.g. a parallel
            # vrg-finalize-pr) can remove the worktree and delete its branch
            # before we reach it (issue #3187). Only that race is tolerated —
            # a failure on a worktree that still exists propagates unchanged.
            if worktrees.worktree_still_present(raw, root, any_location=True):
                raise
            vanished.append(raw)
            continue
        # A detached worktree is classified by the branch recovered for it
        # (a paused rebase's head-name, or its issue-<N>-<slug> directory), so
        # a mid-rebase feature worktree stays in Development (issue #2634).
        kind = worktrees.worktree_kind(
            status.worktree.branch,
            canonical=worktrees.is_canonical_worktree(raw.path, root),
        )
        sections[kind].append(status)

    gone, gone_error = _lookup_gone(main_status, sections)
    now = datetime.datetime.now(tz=datetime.UTC).timestamp()

    print(f"Main checkout (integration branch: {integration})")
    print(_render_table(_MAIN_COLUMNS, [_main_row(main_status, now, gone=gone)]))
    if integration_note is not None:
        print(f"  note: {integration_note}")
    for note in main_status.notes:
        print(f"  note: main checkout: {note}")

    for kind, title in _SECTION_TITLES.items():
        statuses = sorted(sections[kind], key=lambda s: (_SORT_RANK[s.state], s.worktree.branch))
        if not statuses:
            continue
        print()
        print(title)
        labels = [_label(s.worktree.path, root) for s in statuses]
        rows = [
            _row(s, now, label=label, gone=s.worktree.branch in gone)
            for s, label in zip(statuses, labels, strict=True)
        ]
        print(_render_table(_COLUMNS, rows))
        _print_notes(statuses, labels)

    print()
    print(_summary(main_status, sections))
    print(_development_summary(sections[WorktreeKind.DEVELOPMENT]))
    if gone_error is not None:
        print(f"  note: branch-gone check failed: {gone_error}")
    _print_vanished(vanished)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show every worktree in the repo, grouped by kind, with its state.",
    )
    parser.add_argument(
        "--target-branch",
        default="develop",
        help="Branch to measure commits-ahead against (default: develop).",
    )
    return parser.parse_args(argv)


def _lookup_gone(
    main_status: MainCheckoutStatus, sections: dict[WorktreeKind, list[WorktreeStatus]]
) -> tuple[set[str], str | None]:
    """Find which shown branches were deleted on their remote.

    Returns ``(gone, error)``. A failed remote lookup marks nothing gone and
    returns the reason, which the caller prints — never a silent miss.
    """
    branches = {s.worktree.branch for statuses in sections.values() for s in statuses}
    if main_status.branch is not None:
        branches.add(main_status.branch)
    try:
        return worktrees.gone_branches(sorted(branches)), None
    except subprocess.CalledProcessError as exc:
        return set(), (exc.stderr or str(exc)).strip()


def _label(path: Path, root: Path) -> str:
    """Name a worktree: its directory under ``.worktrees/``, else its full path."""
    return path.name if worktrees.is_canonical_worktree(path, root) else str(path)


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


def _branch_cell(branch: str, *, gone: bool) -> str:
    return f"{branch} (branch gone)" if gone else branch


def _row(
    status: WorktreeStatus, now: float, *, label: str | None = None, gone: bool = False
) -> tuple[str, ...]:
    pr = f"#{status.pr_number}" if status.pr_number is not None else "-"
    return (
        label if label is not None else status.worktree.path.name,
        _branch_cell(status.worktree.branch, gone=gone),
        pr,
        status.state.value,
        _workflow_cell(status),
        str(status.ahead),
        "yes" if status.dirty else "-",
        _format_age(status.last_commit_ts, now),
        _format_age(status.last_modified_ts, now),
    )


def _main_state(status: MainCheckoutStatus) -> str:
    problems = status.problems
    return f"PROBLEM: {', '.join(problems)}" if problems else "ok"


def _main_row(status: MainCheckoutStatus, now: float, *, gone: set[str]) -> tuple[str, ...]:
    branch = status.branch if status.branch is not None else _DETACHED
    return (
        status.path.name,
        _branch_cell(branch, gone=branch in gone),
        "yes" if status.dirty else "-",
        _format_age(status.last_commit_ts, now),
        _main_state(status),
    )


def _render_table(columns: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    cells = [columns, *rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    return "\n".join(
        "  ".join(row[i].ljust(widths[i]) for i in range(len(columns))).rstrip() for row in cells
    )


def _print_notes(statuses: list[WorktreeStatus], labels: list[str]) -> None:
    # Surface any captured detail so neither a failed lookup (UNKNOWN) nor a
    # reused-branch-name mismatch (issue #1719) is silently hidden. An
    # unreadable pr-workflow file (the WORKFLOW 'unknown' cell) gets its reason
    # surfaced too, never a silent failure.
    for status, label in zip(statuses, labels, strict=True):
        branch = status.worktree.branch
        key = f"{label} {branch}" if branch == _DETACHED else branch
        if status.detail:
            print(f"  note: {key}: {status.detail}")
        if status.workflow_error:
            print(f"  note: {key}: pr-workflow unreadable: {status.workflow_error}")


def _summary(
    main_status: MainCheckoutStatus, sections: dict[WorktreeKind, list[WorktreeStatus]]
) -> str:
    """One line counting every section, empty ones included."""
    total = 1 + sum(len(statuses) for statuses in sections.values())
    counts = ", ".join(
        f"{len(sections[kind])} {title.lower()}" for kind, title in _SECTION_TITLES.items()
    )
    return f"{total} worktrees: main checkout ({_main_state(main_status)}), {counts}."


def _development_summary(statuses: list[WorktreeStatus]) -> str:
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
        f"Development: {active} active, "
        f"{attention} needs-attention, "
        f"{stalled} stalled (no-pr), {cruft} cruft (removable). "
        f"{prepared} PR prepared."
    )
    if attention:
        line += " Some worktrees need attention (see notes above)."
    if cruft:
        line += " Run vrg-finalize-pr to clean cruft."
    return line


def _gather(raw: RawWorktree, *, target: str) -> WorktreeStatus:
    # A detached-HEAD worktree (the normal state during a paused rebase) has
    # no branch to classify a lifecycle from — it gets its own REBASING /
    # DETACHED status instead of being dropped, so an in-flight worktree is
    # never hidden behind "no worktrees found" (issue #2634).
    if raw.branch is None:
        return worktrees.gather_detached_status(raw, target=target, with_freshness=True)
    return worktrees.gather_worktree_status(
        worktrees.Worktree(path=raw.path, branch=raw.branch),
        target=target,
        with_freshness=True,
    )


def _print_vanished(vanished: list[RawWorktree]) -> None:
    """Report worktrees removed mid-scan, so the drop is never silent."""
    for raw in vanished:
        label = raw.branch or _DETACHED
        print(
            f"  note: {raw.path.name} ({label}): removed by a concurrent operation "
            "during the scan — not listed"
        )


if __name__ == "__main__":
    sys.exit(main())
