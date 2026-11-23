"""Print the epic/task drift audit (read-only safety net).

See :mod:`vergil_tooling.lib.epic_audit`. Surfaces work that slipped through
auto-close so a human can close it.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from vergil_tooling.lib import (
    epic_audit,
    epics,
    github,
    identity_mode,
    outsider_report,
    proxies,
    security_records,
    trust,
)
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.trusted_authors import (
    GITHUB_ACTIONS_ENV,
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
    org_config,
    vergil_orgs,
)

if TYPE_CHECKING:
    from vergil_tooling.lib import epic_docs, roadmap
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

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
    # The outsider report (epic vergil-project/.github#1467, spec §6.5) is a
    # separate read-only mode across the listed orgs, not the cwd's org.
    if args.untrusted:
        return _outsider_report(args)
    # Epic-scoped enumeration is a distinct, read-only mode: resolve the epic ref
    # and emit its children (with states) as machine-readable JSON, bypassing the
    # drift-audit path entirely (issue #2538). Qualify + validate directly rather
    # than via resolve_epic_ref so the read-only 'adhoc' sentinel (which *creates*
    # an epic) is not reachable from an enumeration. With no task to anchor on, a
    # bare '#N' reads against the current repo's epic home (#3415), and the
    # existence check behind it runs under the current repo owner's App token.
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
        return _epic_listing(epic)
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
    # Every read and write runs under the audited org's token, never the cwd
    # repo's: --close writes to the audited org's issues, which only that org's
    # installation can do (#3513).
    with github.target_org(org):
        return _audit(args, org)


def _outsider_report(args: argparse.Namespace) -> int:
    """List the untrusted activity of ``--org``, or of every listed org, since ``--since``.

    Metadata and links only; under GitHub Actions, counts by kind only. A
    read failure (a search GitHub would cut short included) prints nothing on
    stdout and exits 1.
    """
    try:
        authors = load_trusted_authors()
        listed = vergil_orgs(authors)
        if args.org is None:
            orgs = list(listed)
        else:
            match = next((o for o in listed if o.casefold() == args.org.casefold()), None)
            if match is None:
                print(
                    f"vrg-epic-audit: {args.org} is not a Vergil org: the configuration"
                    f" has no [orgs.{args.org}] stanza",
                    file=sys.stderr,
                )
                return 1
            orgs = [match]
        items: list[outsider_report.OutsiderItem] = []
        for org in orgs:
            with github.target_org(org):
                items += outsider_report.outsider_activity(org, since=args.since, authors=authors)
    except github.MissingGitHubTokenError:
        raise  # cli_main prints the auth message
    except (trust.TrustReadError, TrustedAuthorsNotConfiguredError, ValueError) as exc:
        print(f"vrg-epic-audit: {exc}", file=sys.stderr)
        return 1
    under_actions = os.environ.get(GITHUB_ACTIONS_ENV) == "true"
    render = outsider_report.render_counts if under_actions else outsider_report.render
    print(render(items, orgs=orgs, since=args.since))
    return 0


def _epic_listing(epic: epics.IssueRef) -> int:
    """Print *epic*'s children, its proxies' included, as JSON; exit 1 on link drift.

    A ``Primary:``/``Proxy:`` disagreement fails the listing, never only reports
    it, so the retrospective gate stops on it (epic vergil-project/.github#1417):
    the problems go to stderr and no JSON is printed. On a proxy the JSON names
    its primary. A proxy body the trust layer does not trust, or cannot read in
    full, gives no link (spec §5.2 of epic vergil-project/.github#1467): the
    stop goes to stderr, metadata only, and no JSON is printed.
    """
    try:
        problems = epic_audit.proxy_link_problems(epic)
        if problems:
            for problem in problems:
                print(f"vrg-epic-audit: proxy link drift: {problem}", file=sys.stderr)
            return 1
        children = epic_audit.epic_children(epic)
        with github.target_org(epic.owner):
            primary = proxies.primary_of(epic) if proxies.is_proxy_ref(epic) else None
    except proxies.ProxyLinkError as exc:
        print(f"vrg-epic-audit: proxy link drift: {exc}", file=sys.stderr)
        return 1
    except (trust.UntrustedContentError, trust.TrustReadError) as exc:
        print(f"vrg-epic-audit: {exc}", file=sys.stderr)
        return 1
    print(epic_audit.render_epic_children(epic, children, primary=primary))
    return 0


def _audit(args: argparse.Namespace, org: str) -> int:
    """The drift audit (and, with ``--close``, its passes) of *org*, under its token.

    Every pass acts only on trusted content (epic vergil-project/.github#1467,
    spec §5.2 and §5.3), with or without ``--close``: the trusted authors are
    loaded once, and an org with no ``[orgs.<org>]`` stanza is not a Vergil
    org, so it is reported and never swept. A batch run stops the item, not
    the run: each stop is added to one :class:`trust.BatchStops`, named on
    stderr at the end (counted under GitHub Actions), and exits 1.
    """
    try:
        authors = load_trusted_authors()
    except (TrustedAuthorsNotConfiguredError, ValueError) as exc:
        print(f"vrg-epic-audit: {exc}", file=sys.stderr)
        return 1
    if org_config(authors, org) is None:
        print(
            f"vrg-epic-audit: {org} is not a Vergil org (the configuration has no"
            f" [orgs.{org}] stanza); it is not swept",
            file=sys.stderr,
        )
        return 0
    # An owner with no .github has no home to audit (#3613).
    home = epics.require_org_home(org)
    stops = trust.BatchStops()
    since = (datetime.now(UTC) - timedelta(days=args.window_days)).date().isoformat()
    tasks = epic_audit.task_drift(since, org=org, stops=stops, authors=authors)
    epics_drift = epic_audit.epic_drift(org, stops=stops, authors=authors)
    # One listing of the home's epics feeds both per-epic checks (epic .github#819).
    # The home is always in the audited org, so the org's token reads it.
    home_epics = epic_audit.list_home_epics(home)
    closed_epic_open_children = epic_audit.closed_epic_open_child(
        org, stops=stops, authors=authors, home=home, listing=home_epics
    )
    # The proxy close pass (epic .github#1417): an open proxy whose primary has
    # closed. A proxy whose link fails is collected, never raised mid-run, so
    # every other pass still runs; the exit status reflects it at the end.
    proxy_plan, proxy_errors = epic_audit.proxy_closures(
        home, stops=stops, authors=authors, listing=home_epics
    )
    for error in proxy_errors:
        print(f"vrg-epic-audit: proxy link error: {error}", file=sys.stderr)
    if args.close:
        return _close(
            args,
            org,
            home,
            home_epics,
            stops,
            authors,
            tasks=tasks,
            epics_drift=epics_drift,
            closed_epic_open_children=closed_epic_open_children,
            proxy_plan=proxy_plan,
            proxy_errors=proxy_errors,
        )
    epics_outside = epic_audit.epic_outside_dotgithub(org)
    stray = epic_audit.stray_dotgithub_issue(org)
    # An operational child whose body is untrusted is counted in the report,
    # named on stderr, and exits 1 (#4971).
    pending_operational = epic_audit.operational_pending(org, stops=stops)
    closed_operational_no_success = epic_audit.closed_operational_without_success(org)
    # Read-only preview of the ad-hoc drain the --close sweep would perform (epic
    # #238): apply=False mutates nothing. It reads under the org's token, as
    # every read here does (#3513).
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
    parents = epic_audit.body_only_parents(org, stops=stops, authors=authors, issues=issues)
    # Read-only preview of the security-records pass (the reads of
    # vrg-security-record sync, no state writes). --close never runs it. An open
    # issue with a Security advisory: line but no security label is reported
    # with its broken pairs, by ref alone (spec 6.3 of .github#1331; #4996).
    unlabelled = epic_audit.unlabelled_advisory_lines(
        org, stops=stops, authors=authors, issues=issues
    )
    security_rc = security_records.records_pass(
        org, apply=False, prog="vrg-epic-audit", unlabelled=unlabelled
    )
    print(
        epic_audit.render(
            tasks,
            epics_drift,
            org=org,
            window_days=args.window_days,
            epics_outside=epics_outside,
            stray=stray,
            pending_operational=pending_operational,
            closed_operational_no_success=closed_operational_no_success,
            closed_epic_open_children=closed_epic_open_children,
            orphans=epic_audit.orphan_drift(org, parents=parents, issues=issues),
            retired_labels=epic_audit.retired_label_drift(org, issues=issues),
            body_only=list(parents.pairs),
            misplaced=epic_audit.misplaced_archives(org, issues=issues),
            epic_docs_findings=epic_audit.epic_documents(org, home=home, listing=home_epics),
            proxy_link_drift=epic_audit.proxy_link_drift(home, stops=stops, listing=home_epics),
            proxy_closures=proxy_plan,
            proxy_errors=proxy_errors,
            misplaced_adhoc=epic_audit.misplaced_adhoc(org, issues=issues),
        )
    )
    stops.print_summary(sys.stderr)
    # A preview with a broken proxy link or a stopped item never exits clean.
    return 1 if proxy_errors or stops.stopped else security_rc


def _close(  # noqa: PLR0913 - the passes' shared reads, made once by _audit
    args: argparse.Namespace,
    org: str,
    home: str,
    home_epics: list[epic_docs.EpicRecord],
    stops: trust.BatchStops,
    authors: TrustedAuthors,
    *,
    tasks: list[epic_audit.TaskDrift],
    epics_drift: list[roadmap.EpicSummary],
    closed_epic_open_children: list[epic_audit.EpicOpenChildViolation],
    proxy_plan: list[epic_audit.ProxyClosure],
    proxy_errors: list[str],
) -> int:
    """Apply the ``--close`` passes of *org*, from the reads :func:`_audit` made."""
    closed = epic_audit.close_drift(tasks, epics_drift, org=org)
    proxy_drift = epic_audit.close_proxies(proxy_plan)
    proxies_closed = [c.proxy.slug for c in proxy_plan if not c.open_tasks]
    # Remediate the closed-epic-with-open-child invariant (issue #2259): reopen
    # each epic that was wrongly closed while a child was still open.
    reopened = epic_audit.reopen_epics_with_open_children(closed_epic_open_children)
    # Backstop ad-hoc drain (epic #238): drain closed children of every live
    # ad-hoc epic into per-quarter archives and close past archives, under the
    # org's token that main() scopes the whole audit to (spec §7), matching
    # vrg-adhoc-epic and vrg-epic-move. The intake, planning and deferred
    # queue epics (epic vergil-project/.github#176) and the hoshin epic
    # (epic vergil-project/.github#1417) drain the same way.
    now = datetime.now(UTC)
    drained = epics.drain_adhoc_org(org, apply=True, now=now)
    queue_drained = epics.drain_standing_org(org, apply=True, now=now)
    # One org-wide enumeration feeds every orphan-pass check.
    issues = epic_audit.open_issues(org)
    moved = sum(len(p.moves) for p in drained)
    closed_archives = sum(len(p.close) for p in drained)
    print(f"Ad-hoc drain: {moved} child(ren) archived, {closed_archives} past archive(s) closed.")
    qmoved = sum(len(p.moves) for p in queue_drained)
    qclosed = sum(len(p.close) for p in queue_drained)
    print(f"Intake/planning drain: {qmoved} child(ren) archived, {qclosed} past archive(s) closed.")
    # The trusted-comment checks run here too: the sweep reads the delivered
    # configuration (spec §5.1a of epic vergil-project/.github#1467).
    print(
        epic_audit.render_closed(
            closed,
            org=org,
            window_days=args.window_days,
            reopened=reopened,
            misplaced=epic_audit.misplaced_archives(org, issues=issues),
            proxies_closed=proxies_closed,
            proxy_drift=proxy_drift,
            proxy_errors=proxy_errors,
            proxy_link_drift=epic_audit.proxy_link_drift(home, stops=stops, listing=home_epics),
            misplaced_adhoc=epic_audit.misplaced_adhoc(org, issues=issues),
        )
    )
    # No security-records pass here: it lists and writes security advisories,
    # which the sweep's scoped token cannot do (vergil-tooling#4313). The
    # human or the user agent runs it with vrg-security-record sync.
    orphan_rc = _apply_orphan_pass(org, issues, stops=stops, authors=authors)
    stops.print_summary(sys.stderr)
    return 1 if proxy_errors or stops.stopped else orphan_rc


def _apply_orphan_pass(
    org: str,
    issues: list[epic_audit.OpenIssue],
    *,
    stops: trust.BatchStops,
    authors: TrustedAuthors,
) -> int:
    """Apply the orphan pass (epic .github#176) after the existing ``--close`` passes.

    Order: nativize body-only ``Parent:`` links -> strip retired kind labels ->
    attach orphans to intake. Every write runs under the org's token that
    main() scopes the whole audit to. A full queue epic stops the pass with the
    unlinked items on stderr and exit 1 — never a silent skip. An issue the
    trust layer cannot read is a stop on that issue alone, added to *stops*.
    """
    parents = epic_audit.body_only_parents(org, stops=stops, authors=authors, issues=issues)
    nativized = epic_audit.nativize_parents(list(parents.pairs))
    print(f"Body-only Parent: links nativized: {nativized}.")
    stripped = epic_audit.strip_retired_labels(epic_audit.retired_label_drift(org, issues=issues))
    print(f"Retired kind labels stripped: {stripped} issue(s).")
    orphans = epic_audit.orphan_drift(org, parents=parents, issues=issues)
    try:
        attached = epic_audit.attach_orphans(orphans, stops=stops, authors=authors)
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
            "orphans with no epic parent), and each epic's Documents section "
            "(present and well formed; on a closed epic, nothing pending and "
            "every link naming a file; checked only without --close). "
            "Read-only by default; pass --close (as a human) to close the drift "
            "it finds (invariant violations are report-only)."
        ),
        epilog=(
            "Scope: the org is auto-detected from this repo's 'origin' remote, "
            "so run it from inside a repo in the org you want to audit (--org "
            "applies to --untrusted only). Output is Markdown on stdout. Every "
            "read and write runs under the audited org's token."
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
            "the current repo's epic home, '<org>/.github'). "
            'Emits {"epic": <slug>, "children": [{repo, number, title, '
            "state}, ...]} — a stable array of objects (native sub-issues, with "
            "the portable Parent: reflink fallback), so "
            "'.children[] | select(.state==\"OPEN\")' lists the open children "
            "without brittle jq against GitHub's raw subIssues shape. The children "
            "of every proxy epic the epic's trusted 'Proxy:' comments name are "
            "listed beside its own, and on a proxy the JSON gains a "
            '"primary": <slug> key. A Primary:/Proxy: link that is broken or '
            "disagrees exits 1 with the problems on stderr and no JSON. Read-only; "
            "ignores the drift-audit flags."
        ),
    )
    parser.add_argument(
        "--close",
        action="store_true",
        help=(
            "Close the drifted task issues and rolled-up epics, and reopen any "
            "epic wrongly closed while a child was still open (with an "
            "explanatory comment on each), instead of only reporting them. Close "
            "each proxy epic whose primary has closed and which has no open task "
            "(one with an open task is reported and left open). Then "
            "run the org-wide orphan pass: link body-only Parent: lines natively, "
            "remove the retired triage/idea/research labels, and attach every "
            "orphan to its intake epic (exit 1, listing what was not attached, if an intake "
            "epic is full). --close makes no security-advisory call: it does "
            "not sync advisory states or check advisory/issue pairs; run "
            "'vrg-security-record sync' for that. A "
            "human action (or the scheduled reconciliation sweep) — refused in "
            "interactive agent sessions. Default: read-only."
        ),
    )
    parser.add_argument(
        "--untrusted",
        action="store_true",
        help=(
            "Print the outsider report instead of the drift audit: across the "
            "repos of every org listed in the Vergil configuration (or of --org), "
            "the issues and PRs opened by untrusted accounts, untrusted comments, "
            "reviews and inline review comments, untrusted edits and renames of "
            "trusted content, and untrusted closes and reopens, done since "
            "--since. Possible tampering (edits, renames, closes) is listed "
            "first, then the rest, each newest first. Metadata and links only, "
            "never the untrusted text; under GitHub Actions, counts only. "
            "Read-only. Needs --since; ignores the drift-audit flags."
        ),
    )
    parser.add_argument(
        "--since",
        type=_since,
        metavar="ISO",
        help=(
            "With --untrusted: list what was done at or after this ISO 8601 "
            "date (midnight UTC) or time with a zone (e.g. 2026-11-21T08:00:00Z)."
        ),
    )
    parser.add_argument(
        "--org",
        metavar="ORG",
        help="With --untrusted: report on this listed org only.",
    )
    args = parser.parse_args(argv)
    if args.untrusted:
        if args.since is None:
            parser.error("--untrusted needs --since")
        if args.epic is not None or args.close:
            parser.error("--untrusted cannot be combined with --epic or --close")
    elif args.since is not None or args.org is not None:
        parser.error("--since and --org need --untrusted")
    return args


def _since(raw: str) -> str:
    try:
        outsider_report.parse_since(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return raw


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
