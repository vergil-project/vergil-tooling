"""Show the state of the repository clone: its worktrees, grouped by kind,
the open PRs no worktree backs, and what is pending release.

Read-only observability for worktree hygiene. Everything with code on disk in
this repo is shown, in four worktree sections (issue #3386), then the open PRs
on ``origin`` that no linked worktree shows, then the Pending release section:

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
5. **Open PRs with no local worktree** — every open PR on ``origin`` whose
   head branch no linked worktree holds (a PR from a fork never counts as
   backed): automation, lifecycle and dependency-update PRs, and PRs
   prepared on another machine, with their type, author and CI state
   (issue #4581). Always shown; a failed lookup prints ``ERROR:``, never
   "no open PRs", and the command exits non-zero after the rest of the
   output.
6. **Pending release** — every change the integration branch holds that
   ``main`` does not, mapped to its PR by commit SHA, in merge order, with
   the latest release and rolling tags (epic vergil-project/.github#1049).
   Shown only when ``vergil.toml`` sets ``[publish].release``. Read from the
   remote without fetching; a failed lookup prints ``ERROR:`` and the
   command exits non-zero after the rest of the output.

Cleanup stays ``vrg-finalize-pr``'s job — this command only observes
(issue #1552). PR state is queried from GitHub (one call per worktree)
for an authoritative merged/closed verdict; a failed lookup is shown as
``unknown`` with the reason rather than silently downgraded.

A worktree's open PR is shown whatever its local state, a branch mid-rebase
included. A development worktree's open PR, the kind ``vrg-submit-pr`` opens
with auto-merge on, is read once more for its merge state (issue #4678). A PR that
cannot merge as it stands shows ``merge failed: <reason>`` in STATE, with the
reasons ``vrg-pr-await`` defines (``conflict``, ``check_failed``,
``check_cancelled``, ``auto_merge_off``, ``orphaned_check``), beside any local
state such as ``rebasing``, and a note naming the failure. A merge state that cannot be read
shows ``merge unknown`` with the reason.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, finalize_watch, pr_await, text_table, worktrees
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.open_prs import OpenPR, OpenPRKind, OpenPRsError, gather_open_prs
from vergil_tooling.lib.pending_release import (
    RELEASE_BRANCH,
    ChangeKind,
    PendingChange,
    PendingReleaseError,
    ReleaseTags,
    gather_pending_release,
)
from vergil_tooling.lib.process_errors import error_text, failure_message
from vergil_tooling.lib.worktrees import (
    MainCheckoutStatus,
    RawWorktree,
    WorktreeKind,
    WorktreeState,
    WorktreeStatus,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
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

_PENDING_COLUMNS = ("PR", "BRANCH", "TYPE", "MERGED", "TITLE")

_OPEN_COLUMNS = ("PR", "BRANCH", "BASE", "TYPE", "AUTHOR", "CI", "OPENED", "TITLE")

_OPEN_HEADING = "Open PRs on origin with no local worktree"

# A longer TITLE is cut to this width, its last character an ellipsis, to
# keep each pending-release row on one line.
_TITLE_WIDTH = text_table.TITLE_WIDTH

# A rolling tag's target is a release tag (vX.Y.Z) or a short SHA; only a
# release tag can lag the latest release.
_RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

_SHORT_SHA = 7


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = worktrees.discover_repo_worktrees()
    root = repo.main.path
    # One config read gives both the integration branch and the publish gate.
    integration_notes: list[str] = []
    try:
        cfg = config.read_config(root)
    except FileNotFoundError:
        integration = "develop"
        show_pending = False
        integration_notes = [
            f"no {config.CONFIG_FILE}; integration branch defaults to {integration}",
            f"no {config.CONFIG_FILE}; pending-release section skipped",
        ]
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    else:
        integration = config.INTEGRATION_BRANCH_BY_MODEL[cfg.project.branching_model]
        show_pending = cfg.publish.release
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
        # Only issue-branch PRs are opened by vrg-submit-pr with auto-merge on;
        # a release or deps PR is merged directly and never uses auto-merge,
        # so its merge state is not judged by vrg-pr-await's reasons.
        if kind is WorktreeKind.DEVELOPMENT:
            status = _with_merge_state(status)
        sections[kind].append(status)

    gone, gone_error = _lookup_gone(main_status, sections)
    now = datetime.datetime.now(tz=datetime.UTC).timestamp()

    print(f"Main checkout (integration branch: {integration})")
    print(text_table.render(_MAIN_COLUMNS, [_main_row(main_status, now, gone=gone)]))
    for note in integration_notes:
        print(f"  note: {note}")
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
        print(text_table.render(_COLUMNS, rows))
        _print_notes(statuses, labels)

    # A failed open-PR or pending-release lookup still lets the rest print,
    # then the command exits non-zero: it could not say what is open or
    # pending (spec §5.4 of epic vergil-project/.github#1049, issue #4581).
    open_ok = _print_open_prs(sections, now)
    pending_ok = _print_pending(integration, now) if show_pending else True

    print()
    print(_summary(main_status, sections))
    print(_development_summary(sections[WorktreeKind.DEVELOPMENT]))
    if gone_error is not None:
        print(f"  note: branch-gone check failed: {gone_error}")
    _print_vanished(vanished)
    return 0 if open_ok and pending_ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Show the state of the repository clone: its worktrees, grouped by kind, "
            "the open PRs on origin that no local worktree backs, and what is "
            "pending release."
        ),
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
        return set(), (exc.stderr or failure_message(exc)).strip()


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


def _state_cell(status: WorktreeStatus) -> str:
    """The STATE cell: the worktree's state and, apart, its open PR's merge state.

    A PR that cannot merge shows ``merge failed: <reason>``; one whose merge
    state could not be read shows ``merge unknown``. For a clean worktree with
    an open PR that replaces ``open-pr``; a local state (``rebasing``,
    ``detached``) is kept in front of it, so both are shown (#4678).
    """
    if status.merge_reason is not None:
        merge = f"merge failed: {status.merge_reason}"
    elif status.merge_error is not None:
        merge = "merge unknown"
    else:
        return status.state.value
    if status.state is WorktreeState.OPEN_PR:
        return merge
    return f"{status.state.value}, {merge}"


def _row(
    status: WorktreeStatus, now: float, *, label: str | None = None, gone: bool = False
) -> tuple[str, ...]:
    pr = f"#{status.pr_number}" if status.pr_number is not None else "-"
    return (
        label if label is not None else status.worktree.path.name,
        _branch_cell(status.worktree.branch, gone=gone),
        pr,
        _state_cell(status),
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


def _print_notes(statuses: list[WorktreeStatus], labels: list[str]) -> None:
    # Surface any captured detail so neither a failed lookup (UNKNOWN) nor a
    # reused-branch-name mismatch (issue #1719) is silently hidden. An
    # unreadable pr-workflow file (the WORKFLOW 'unknown' cell) gets its reason
    # surfaced too, never a silent failure.
    for status, label in zip(statuses, labels, strict=True):
        branch = status.worktree.branch
        key = f"{label} {branch}" if branch == _DETACHED else branch
        for note in _status_notes(status):
            print(f"  note: {key}: {note}")
        if status.workflow_error:
            print(f"  note: {key}: pr-workflow unreadable: {status.workflow_error}")


def _status_notes(status: WorktreeStatus) -> list[str]:
    """The notes for one worktree row.

    An open PR that cannot merge is named with its reason, and a merge state
    that could not be read with why. A rebase on a branch with an open PR is
    the PR's own (pr-watch updating it), so the how-to-finish-a-rebase detail
    is shown only for a worktree with no open PR (#4678).
    """
    pr = status.pr_number
    rebasing = status.state is WorktreeState.REBASING
    notes: list[str] = []
    if status.merge_reason is not None:
        note = f"PR #{pr} cannot merge ({status.merge_reason}): {status.merge_detail}"
        if rebasing:
            note += "; pr-watch is resolving it (rebase in progress)"
        notes.append(note)
    elif status.merge_error is not None:
        notes.append(f"PR #{pr}: merge state unreadable: {status.merge_error}")
    if rebasing and pr is not None:
        if status.merge_reason is None:
            notes.append(f"PR #{pr} is open; its branch is mid-rebase (pr-watch updating it)")
    elif status.detail:
        notes.append(status.detail)
    return notes


def _summary(
    main_status: MainCheckoutStatus, sections: dict[WorktreeKind, list[WorktreeStatus]]
) -> str:
    """One line counting every section, empty ones included."""
    total = 1 + sum(len(statuses) for statuses in sections.values())
    counts = ", ".join(
        f"{len(sections[kind])} {title.lower()}" for kind, title in _SECTION_TITLES.items()
    )
    return f"{_count(total, 'worktree')}: main checkout ({_main_state(main_status)}), {counts}."


# Row states whose branch already has a PR, so nothing is left to submit.
_HAS_PR = frozenset({WorktreeState.OPEN_PR, WorktreeState.MERGED, WorktreeState.CLOSED})


def _development_summary(statuses: list[WorktreeStatus]) -> str:
    total = len(statuses)
    cruft = sum(1 for s in statuses if s.removable)
    attention = sum(1 for s in statuses if s.needs_attention)
    # A reused-branch worktree lands as NO_PR *and* needs_attention; count it
    # only under attention so the buckets stay disjoint and sum to total.
    stalled = sum(1 for s in statuses if s.state is WorktreeState.NO_PR and not s.needs_attention)
    # A ready state file on a branch whose PR is already open, merged or
    # closed (one opened by hand never stamps the state) is not a prepared
    # PR: count it by its row's state, not the file (issue #4993).
    prepared = sum(1 for s in statuses if s.pr_prepared and s.state not in _HAS_PR)
    # Finished-but-stuck worktrees (needs_attention) are pulled out of active:
    # never let a merged-but-dirty worktree pose as live work behind "0 cruft"
    # (issue #2347).
    active = total - cruft - stalled - attention
    line = (
        f"Development: {active} active, "
        f"{attention} needs-attention, "
        f"{stalled} stalled (no-pr), {cruft} cruft (removable). "
        f"{_count(prepared, 'PR')} prepared."
    )
    if attention:
        line += " Some worktrees need attention (see notes above)."
    blocked = sum(1 for s in statuses if s.merge_reason is not None)
    if blocked:
        line += f" {_count(blocked, 'PR')} cannot merge (see notes above)."
    if cruft:
        line += " Run vrg-finalize-pr to clean cruft."
    return line


def _count(n: int, noun: str) -> str:
    """``'1 change'`` / ``'2 changes'``: *n* with the noun agreeing with it."""
    return f"{n} {noun if n == 1 else noun + 's'}"


def _release_line(tags: ReleaseTags, main_moved_ts: float, now: float) -> str:
    """The latest release, how long ago main moved, and the rolling tag.

    It is printed after the section heading, on the heading's line.
    """
    latest = tags.latest if tags.latest is not None else "no release yet"
    line = f"latest release {latest}, main moved {_format_age(main_moved_ts, now)}; "
    if tags.rolling is None or tags.rolling_target is None:
        return line + "no rolling tag"
    line += f"rolling tag {tags.rolling} → {tags.rolling_target}"
    target = tags.rolling_target
    # A lag is information, not a problem: the rolling tag can be held back
    # deliberately. A short-SHA target is no release, so it cannot lag one.
    if _RELEASE_TAG_RE.match(target) and tags.latest is not None and target != tags.latest:
        line += f" (lags {tags.latest})"
    return line


def _pending_rows(changes: Sequence[PendingChange], now: float) -> list[tuple[str, ...]]:
    return [
        (
            f"#{c.pr_number}" if c.pr_number is not None else "PROBLEM",
            c.branch if c.branch is not None else "-",
            c.type_label,
            _format_age(c.merged_ts, now),
            text_table.truncate(c.title),
        )
        for c in changes
    ]


def _pending_summary(changes: Sequence[PendingChange]) -> str:
    """The section's count of changes, every bucket listed; commits with no
    PR, and PRs the trust layer does not accept, are named only when there
    are some."""
    kinds = [c.kind for c in changes]
    line = (
        f"{_count(len(changes), 'change')} pending release: "
        f"{_count(kinds.count(ChangeKind.DEPS), 'dependency update')}, "
        f"{_count(kinds.count(ChangeKind.RELEASE), 'release back-merge')}, "
        f"{_count(kinds.count(ChangeKind.AUTOMATION), 'automation PR')}, "
        f"{_count(kinds.count(ChangeKind.OTHER), 'other PR')}"
    )
    problems = kinds.count(ChangeKind.NO_PR)
    if problems:
        line += f", {problems} PROBLEM: commits with no PR"
    untrusted = kinds.count(ChangeKind.UNTRUSTED)
    if untrusted:
        line += f", {untrusted} PROBLEM: not a same-repo PR with a trusted author"
    return line + "."


def _print_pending(integration: str, now: float) -> bool:
    """Print the Pending release section; return False only on an error.

    The heading and the release line share one line. A failed or incomplete
    lookup prints the bare heading and the ``ERROR:`` line alone, never a
    partial table (spec §5.4).
    """
    print()
    heading = f"Pending release ({integration} → {RELEASE_BRANCH})"
    try:
        pending = gather_pending_release(integration=integration)
    except PendingReleaseError as exc:
        print(heading)
        print(f"ERROR: pending release: {exc}")
        return False
    print(f"{heading}: {_release_line(pending.tags, pending.main_moved_ts, now)}")
    if not pending.changes:
        print(f"Nothing pending release: {integration} and {RELEASE_BRANCH} are level.")
        return True
    print(text_table.render(_PENDING_COLUMNS, _pending_rows(pending.changes, now)))
    for change in pending.changes:
        if change.kind is ChangeKind.NO_PR:
            sha7 = change.shas[0][:_SHORT_SHA]
            print(f"  PROBLEM: {sha7} reached {integration} without a PR")
        elif change.kind is ChangeKind.UNTRUSTED:
            print(
                f"  PROBLEM: #{change.pr_number} is not a same-repo PR with a trusted "
                "author; its title, branch and labels are not read"
            )
    print(_pending_summary(pending.changes))
    return True


def _backed(pr: OpenPR, branches: set[str], numbers: set[int]) -> bool:
    """Whether a linked worktree already shows *pr*: its PR number, or its
    head branch for a PR from this repository (a fork's branch name can
    collide with a local one, so it never counts)."""
    if pr.number in numbers:
        return True
    return not pr.cross_repository and pr.branch in branches


def _open_branch_cell(pr: OpenPR) -> str:
    return f"{pr.head_owner}:{pr.branch}" if pr.head_owner is not None else pr.branch


def _open_rows(prs: Sequence[OpenPR], now: float) -> list[tuple[str, ...]]:
    return [
        (
            f"#{pr.number}",
            _open_branch_cell(pr),
            pr.base,
            pr.type_label,
            pr.author,
            pr.ci,
            _format_age(pr.created_ts, now),
            text_table.truncate(f"[draft] {pr.title}" if pr.draft else pr.title),
        )
        for pr in prs
    ]


def _open_summary(prs: Sequence[OpenPR]) -> str:
    """The section's count of open PRs, every bucket listed."""
    kinds = [pr.kind for pr in prs]
    return (
        f"{_count(len(prs), 'open PR')} with no local worktree: "
        f"{_count(kinds.count(OpenPRKind.AUTOMATION), 'automation PR')}, "
        f"{_count(kinds.count(OpenPRKind.LIFECYCLE), 'lifecycle PR')}, "
        f"{_count(kinds.count(OpenPRKind.DEPS), 'dependency update')}, "
        f"{_count(kinds.count(OpenPRKind.RELEASE), 'release PR')}, "
        f"{_count(kinds.count(OpenPRKind.OTHER), 'other PR')}."
    )


def _print_open_prs(sections: dict[WorktreeKind, list[WorktreeStatus]], now: float) -> bool:
    """Print the open PRs no linked worktree shows; return False only on an error.

    A failed lookup prints the heading and the ``ERROR:`` line alone, never a
    partial table and never a count of zero.
    """
    statuses = [s for kind_statuses in sections.values() for s in kind_statuses]
    branches = {s.worktree.branch for s in statuses}
    numbers = {s.pr_number for s in statuses if s.pr_number is not None}
    print()
    print(_OPEN_HEADING)
    try:
        prs = gather_open_prs()
    except OpenPRsError as exc:
        print(f"ERROR: open PRs: {exc}")
        return False
    unbacked = [pr for pr in prs if not _backed(pr, branches, numbers)]
    if not unbacked:
        if prs:
            print(f"None: {_count(len(prs), 'open PR')} on origin, each with a local worktree.")
        else:
            print("None: origin has no open PRs.")
        return True
    print(text_table.render(_OPEN_COLUMNS, _open_rows(unbacked, now)))
    print(_open_summary(unbacked))
    return True


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


# The states whose PR is open, so its merge state is read: a clean worktree
# with an open PR, or one whose branch is mid-rebase or detached.
_MERGE_STATE_STATES = frozenset(
    {WorktreeState.OPEN_PR, WorktreeState.REBASING, WorktreeState.DETACHED}
)


def _with_merge_state(status: WorktreeStatus) -> WorktreeStatus:
    """Attach why *status*'s open PR cannot merge, if it cannot (#4678).

    The reasons are ``vrg-pr-await``'s (:data:`pr_await.Reason`), from one
    read of the PR. A failed read is attached as ``merge_error`` and shown,
    never read as a PR that can merge.
    """
    if status.pr_number is None or status.state not in _MERGE_STATE_STATES:
        return status
    try:
        block = pr_await.standing_block(str(status.pr_number))
    except (
        subprocess.CalledProcessError,
        ValueError,
        finalize_watch.FinalizeWatchError,
        pr_await.PrAwaitError,
    ) as exc:
        return replace(status, merge_error=error_text(exc))
    if block is None:
        return status
    return replace(status, merge_reason=block.reason, merge_detail=block.detail)


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
