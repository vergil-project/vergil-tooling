"""Print the epic/task drift audit (read-only safety net).

See :mod:`vergil_tooling.lib.epic_audit`. Surfaces work that slipped through
auto-close so a human can close it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from vergil_tooling.lib import epic_audit, epics, github, identity_mode
from vergil_tooling.lib.cli import cli_main

_DEFAULT_WINDOW_DAYS = 30

# Set by the scheduled reconciliation workflow (ops-epic-sweep) to authorize the
# automated ``--close``. The sweep only closes provably-complete drift (epics
# whose children are all closed, tasks whose PR merged), so it is a trusted,
# deterministic automation — allowed to close even though it is not a human. An
# interactive agent session never sets this, so the human/agent gate stands.
_SWEEP_ENV = "VRG_EPIC_SWEEP"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Epic-scoped enumeration is a distinct, read-only mode: resolve the epic ref
    # and emit its children (with states) as machine-readable JSON, bypassing the
    # drift-audit path entirely (issue #2538). Qualify + validate directly rather
    # than via resolve_epic_ref so the read-only 'adhoc' sentinel (which *creates*
    # an epic) is not reachable from an enumeration. With no task to anchor on, a
    # bare '#N' reads against the current repo's epic home (#3415), and the
    # visibility probe behind it runs under the current repo owner's App token.
    if args.epic is not None:
        repo = github.current_repo()
        try:
            with github.target_org(repo.split("/", 1)[0]):
                epic = epics.qualify_epic_ref(args.epic, repo=repo)
        except ValueError as exc:
            print(f"vrg-epic-audit: {exc}", file=sys.stderr)
            return 1
        if not epics.is_epic(epic):
            print(
                f"vrg-epic-audit: {epic.slug} is not an epic (missing the 'epic' label)",
                file=sys.stderr,
            )
            return 1
        print(epic_audit.render_epic_children(epic, epics.child_states(epic)))
        return 0
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
    if args.repo:
        if "/" not in args.repo:
            print(
                f"vrg-epic-audit: --repo must be 'owner/repo' (got {args.repo!r})",
                file=sys.stderr,
            )
            return 1
        owner, bare = args.repo.split("/", 1)
        org, home = owner, epics.resolve_epic_home(owner, bare)
    else:
        org = github.detect_org()
        if org is None:
            print(
                "vrg-epic-audit: could not determine the GitHub org from this "
                "repo's 'origin' remote; run it from inside a repo in the org you "
                "want to audit.",
                file=sys.stderr,
            )
            return 1
        home = f"{org}/.github"
    since = (datetime.now(UTC) - timedelta(days=args.window_days)).date().isoformat()
    tasks = epic_audit.task_drift(since, org=org)
    epics_drift = epic_audit.epic_drift(org, home=home)
    closed_epic_open_children = epic_audit.closed_epic_open_child(org, home=home)
    if args.close:
        closed = epic_audit.close_drift(tasks, epics_drift, org=org, home=home)
        # Remediate the closed-epic-with-open-child invariant (issue #2259): reopen
        # each epic that was wrongly closed while a child was still open.
        reopened = epic_audit.reopen_epics_with_open_children(closed_epic_open_children)
        # Backstop ad-hoc drain (epic #238): drain closed children of every live
        # ad-hoc epic into per-quarter archives and close past archives. main() has
        # no other org scope, so scope the App token to the org before this
        # mutation (spec §7), matching vrg-adhoc-epic and vrg-epic-move.
        # The intake and planning queue epics drain the same way (epic
        # vergil-project/.github#176), under the same org scope.
        now = datetime.now(UTC)
        with github.target_org(org):
            drained = epics.drain_adhoc_org(org, apply=True, now=now)
            queue_drained = epics.drain_standing_org(org, apply=True, now=now)
            # One org-wide enumeration feeds every orphan-pass check.
            issues = epic_audit.open_issues(org)
        moved = sum(len(p.moves) for p in drained)
        closed_archives = sum(len(p.close) for p in drained)
        print(
            f"Ad-hoc drain: {moved} child(ren) archived, {closed_archives} past archive(s) closed."
        )
        qmoved = sum(len(p.moves) for p in queue_drained)
        qclosed = sum(len(p.close) for p in queue_drained)
        print(
            f"Intake/planning drain: {qmoved} child(ren) archived, "
            f"{qclosed} past archive(s) closed."
        )
        print(
            epic_audit.render_closed(
                closed,
                org=org,
                window_days=args.window_days,
                home=home,
                reopened=reopened,
                private_under_public=epic_audit.private_under_public_epic(org, issues=issues),
            )
        )
        return _apply_orphan_pass(org, issues)
    epics_outside = epic_audit.epic_outside_dotgithub(org)
    stray = epic_audit.stray_dotgithub_issue(org, home=home)
    pending_operational = epic_audit.operational_pending(org, home=home)
    closed_operational_no_success = epic_audit.closed_operational_without_success(org)
    # Read-only preview of the ad-hoc drain the --close sweep would perform (epic
    # #238): apply=False mutates nothing, so no org scope is needed.
    now = datetime.now(UTC)
    preview = epics.drain_adhoc_org(org, apply=False, now=now)
    pmoved = sum(len(p.moves) for p in preview)
    print(f"Ad-hoc drain (preview): {pmoved} child(ren) would be archived.")
    queue_preview = epics.drain_standing_org(org, apply=False, now=now)
    qpmoved = sum(len(p.moves) for p in queue_preview)
    print(f"Intake/planning drain (preview): {qpmoved} child(ren) would be archived.")
    # Read-only preview of the orphan pass: one shared enumeration, no writes
    # (an absent intake epic is named, not created).
    issues = epic_audit.open_issues(org)
    print(
        epic_audit.render(
            tasks,
            epics_drift,
            org=org,
            window_days=args.window_days,
            home=home,
            epics_outside=epics_outside,
            stray=stray,
            pending_operational=pending_operational,
            closed_operational_no_success=closed_operational_no_success,
            closed_epic_open_children=closed_epic_open_children,
            orphans=epic_audit.orphan_drift(org, issues=issues),
            retired_labels=epic_audit.retired_label_drift(org, issues=issues),
            body_only=epic_audit.body_only_parents(org, issues=issues),
            public_queues=epic_audit.public_self_homed_queues(org, issues=issues),
            private_under_public=epic_audit.private_under_public_epic(org, issues=issues),
        )
    )
    return 0


def _apply_orphan_pass(org: str, issues: list[epic_audit.OpenIssue]) -> int:
    """Apply the orphan pass (epic .github#176) after the existing ``--close`` passes.

    Order: nativize body-only ``Parent:`` links -> strip retired kind labels ->
    move now-public repos' queue items -> attach orphans to intake. Every write is
    scoped to the org's token. A full queue epic stops the pass with the
    unlinked items on stderr and exit 1 — never a silent skip.
    """
    with github.target_org(org):
        nativized = epic_audit.nativize_parents(epic_audit.body_only_parents(org, issues=issues))
        print(f"Body-only Parent: links nativized: {nativized}.")
        stripped = epic_audit.strip_retired_labels(
            epic_audit.retired_label_drift(org, issues=issues)
        )
        print(f"Retired kind labels stripped: {stripped} issue(s).")
        try:
            moved = epic_audit.move_public_queues(
                epic_audit.public_self_homed_queues(org, issues=issues), org=org
            )
            print(f"Public-repo queue items moved to {org}/.github: {moved}.")
            attached = epic_audit.attach_orphans(epic_audit.orphan_drift(org, issues=issues))
        except epic_audit.StandingEpicFull as exc:
            print(f"vrg-epic-audit: {exc}", file=sys.stderr)
            return 1
        print(f"Orphans attached to intake: {attached}.")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-epic-audit",
        description=(
            "Report epic/task drift and invariant violations for the current "
            "repo's GitHub org: merged PRs whose Ref'd task is still open, open "
            "non-perpetual epics whose children are all closed, and issues in "
            "the wrong place (epics outside .github; stray .github issues; "
            "orphans with no epic parent; private-repo work under public .github "
            "epics). "
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
        "--epic",
        metavar="REF",
        help=(
            "Enumerate a single epic's child tasks as machine-readable JSON "
            "instead of running the drift audit. REF is an epic ref "
            "('owner/repo#N', used as written, or a bare '#N' resolved against "
            "the current repo's epic home: '<org>/.github' for a public repo, "
            "the repo itself for a private repo that homes its own epics). "
            'Emits {"epic": <slug>, "children": [{repo, number, title, '
            "state}, ...]} — a stable array of objects (native sub-issues, with "
            "the portable Parent: reflink fallback), so "
            "'.children[] | select(.state==\"OPEN\")' lists the open children "
            "without brittle jq against GitHub's raw subIssues shape. Read-only; "
            "ignores the drift-audit flags."
        ),
    )
    parser.add_argument(
        "--repo",
        help=(
            "Audit a single repo's resolved epic home ('owner/repo') instead of "
            "the org's .github — a private repo self-homes its epics. The "
            "home-scoped checks (epic drift, stray issues, operational-pending, "
            "and --close) then target that repo."
        ),
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help=(
            "Close the drifted task issues and rolled-up epics, and reopen any "
            "epic wrongly closed while a child was still open (with an "
            "explanatory comment on each), instead of only reporting them. Then "
            "run the org-wide orphan pass: link body-only Parent: lines natively, "
            "remove the retired triage/idea/research labels, move now-public "
            "repos' queue items under .github, and attach every orphan to its "
            "intake epic (exit 1, listing what was not attached, if an intake "
            "epic is full). A "
            "human action (or the scheduled reconciliation sweep) — refused in "
            "interactive agent sessions. Default: read-only."
        ),
    )
    return parser.parse_args(argv)


def _automated_sweep() -> bool:
    """True when running as the trusted scheduled reconciliation sweep."""
    return os.environ.get(_SWEEP_ENV) == "1"


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        msg = "must be a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return value


if __name__ == "__main__":
    sys.exit(main())
