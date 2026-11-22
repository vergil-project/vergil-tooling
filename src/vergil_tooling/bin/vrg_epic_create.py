"""Create a top-level epic issue in its resolved home repo.

Epics are top-level (no parent), labelled ``epic`` plus the derived category
label (:func:`vergil_tooling.lib.epics.category_labels`): ``epic-development``
for a plain epic, ``epic-management`` for a queue, hoshin or operations epic, none beyond
``ad-hoc`` for an ad-hoc one. The *home* — the repo where
the epic issue physically lives — is the target's ``<org>/.github``
(:func:`vergil_tooling.lib.epics.resolve_epic_home`). The target defaults to
the current repo but is named
explicitly with ``--repo``. This is the sanctioned path for creating epics —
``vrg-gh`` denies raw ``gh issue create`` — used by the ``epic-create`` skill.

``--proxy-for <owner>/<repo>#<N> --repo <owner>/<repo>`` instead ensures the
**proxy epic** for another org's primary epic in the target's org home, and the
``Proxy:`` comment on the primary (:func:`vergil_tooling.lib.proxies.create_proxy`,
vergil-project/.github#1417). Its title and body are derived, so it takes no
``--title``, ``--body``, ``--label`` or ``--assignee``.

``--standing <kind> --org <org>`` instead ensures the org's standing epic of
that kind in ``<org>/.github`` (``intake``, ``planning``, ``deferred``,
``hoshin`` or ``operations``), creating it if absent and reusing it otherwise,
and prints its ref; it creates no task. It resolves the kind's ``--epic``
sentinel (:func:`vergil_tooling.lib.epics.resolve_epic_ref`), the path
``vrg-issue-create`` takes on first use, so its title, body and labels are
derived and it takes none of the flags above.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github, proxies
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.labels import ensure_labels_or_report


# The standing kinds --standing ensures: every one homed only in <org>/.github.
# The ad-hoc epic is per repo; vrg-adhoc-epic ensure provisions it.
STANDING_CHOICES: tuple[str, ...] = tuple(
    kind.sentinel for kind in epics.STANDING_KINDS if kind is not epics.ADHOC
)


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.proxy_for is not None and args.standing is not None:
        print("vrg-epic-create: --proxy-for and --standing are exclusive", file=sys.stderr)
        return 1
    if args.proxy_for is not None:
        return _proxy_for(args)
    if args.standing is not None:
        return _standing(args)
    if args.org is not None:
        print("vrg-epic-create: --org is only for --standing", file=sys.stderr)
        return 1
    if args.title is None:
        print(
            "vrg-epic-create: --title is required (unless --proxy-for or --standing)",
            file=sys.stderr,
        )
        return 1
    target = args.repo or github.current_repo()  # "owner/repo"; raises if undeterminable
    if "/" not in target:
        print(
            f"vrg-epic-create: --repo must be 'owner/repo' (got {target!r})",
            file=sys.stderr,
        )
        return 1
    given = [label for label in args.label if label in epics.DERIVED_CATEGORY_LABELS]
    if given:
        print(
            f"vrg-epic-create: --label {', '.join(given)} is a derived category label;"
            " it is applied from the epic's other labels, never given",
            file=sys.stderr,
        )
        return 1
    try:
        labels = epics.category_labels(["epic", *args.label], args.title)
    except epics.AmbiguousEpicCategoryError as exc:
        print(f"vrg-epic-create: {exc}", file=sys.stderr)
        return 1
    owner, bare = target.split("/", 1)
    home = epics.resolve_epic_home(owner, bare)
    # Scope every App-token call to the target's org, not the cwd org (mirrors
    # issue-create, #2070). Without this a cross-org epic mints the token for the
    # wrong org and either fails to write or misreports the epic's location (#2722).
    with github.target_org(owner):
        print(f"-> epic home: {home}")
        if not ensure_labels_or_report("vrg-epic-create", home, labels):
            return 1
        url = github.create_issue(
            repo=home,
            title=args.title,
            body=args.body,
            body_file=args.body_file,
            labels=labels,
            assignees=args.assignee,
        )
    print(f"Created {url} (epic).")
    return 0


def _given_derived_flags(args: argparse.Namespace) -> list[str]:
    """The content flags given that a derived epic (proxy or standing) refuses."""
    return [
        flag
        for flag, value in (
            ("--title", args.title),
            ("--body", args.body),
            ("--body-file", args.body_file),
            ("--label", args.label),
            ("--assignee", args.assignee),
        )
        if value
    ]


def _standing(args: argparse.Namespace) -> int:
    """``--standing``: ensure the org's standing epic of that kind and print its ref."""
    refused = _given_derived_flags(args)
    if args.repo is not None:
        refused.append("--repo")
    if refused:
        print(
            f"vrg-epic-create: --standing derives the epic's title, body, labels and"
            f" home; it does not take {', '.join(refused)}",
            file=sys.stderr,
        )
        return 1
    if not args.org or "/" in args.org:
        print(
            f"vrg-epic-create: --standing needs --org ORG, the org whose .github homes"
            f" the epic (got {args.org!r})",
            file=sys.stderr,
        )
        return 1
    # Scope the App token to the named org, not the cwd org (#2722).
    with github.target_org(args.org):
        epic = epics.resolve_epic_ref(args.standing, repo=f"{args.org}/.github")
    print(f"{args.standing.capitalize()} epic: {epic.slug}")
    return 0


def _proxy_for(args: argparse.Namespace) -> int:
    """``--proxy-for``: ensure the proxy and its comment, and report what was done."""
    derived = _given_derived_flags(args)
    if args.org is not None:
        derived.append("--org")
    if derived:
        print(
            f"vrg-epic-create: --proxy-for derives the proxy's title, body and labels;"
            f" it does not take {', '.join(derived)}",
            file=sys.stderr,
        )
        return 1
    if args.repo is None:
        print(
            "vrg-epic-create: --proxy-for needs --repo OWNER/REPO, the repo the proxy's"
            " tasks land in",
            file=sys.stderr,
        )
        return 1
    try:
        # No default repo, so a bare '#N' is refused: the primary is in another org.
        primary = epics.parse_issue_ref(args.proxy_for, default_repo="")
    except ValueError:
        print(
            f"vrg-epic-create: --proxy-for must be a full 'owner/repo#N' ref"
            f" (got {args.proxy_for!r})",
            file=sys.stderr,
        )
        return 1
    try:
        result = proxies.create_proxy(primary, args.repo)
    except (proxies.ProxyRefusedError, proxies.ProxyLinkError) as exc:
        print(f"vrg-epic-create: {exc}", file=sys.stderr)
        return 1
    proxy = result.proxy.slug
    if result.created:
        print(f"Created proxy {proxy} for {primary.slug}.")
    elif result.reopened:
        print(f"Reopened proxy {proxy} for {primary.slug} (it was closed).")
    else:
        print(f"Proxy {proxy} for {primary.slug} already exists.")
    if result.retitled:
        print(f"Retitled proxy {proxy} to the derived title.")
    if result.commented:
        print(f"Posted '{proxies.proxy_line(result.proxy)}' on {primary.slug}.")
    else:
        print(f"{primary.slug} already records '{proxies.proxy_line(result.proxy)}'.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-epic-create",
        description=(
            "Create a top-level epic labelled 'epic' with no parent. The epic "
            "home is the target repo's <org>/.github."
        ),
        epilog=(
            "Extra --label values are added alongside 'epic' (e.g. --label "
            "ad-hoc for an ad-hoc epic). The epic's category label is derived and "
            "applied too: 'epic-management' for an intake, planning, deferred, "
            "hoshin or operations epic, 'epic-development' for a plain epic; neither "
            "may be given "
            "with --label, and labels marking two categories are refused. With "
            "--proxy-for OWNER/REPO#N --repo OWNER/REPO it instead ensures the "
            "proxy epic for that primary epic in the target's org .github, and "
            "the 'Proxy:' comment on the primary; the two repos must be "
            "in different orgs. With --standing KIND --org ORG it instead ensures "
            "ORG's standing epic of that kind in ORG/.github, creating it if absent, "
            "and prints its ref; it creates no task."
        ),
    )
    parser.add_argument("--title", help="Epic title (required unless --proxy-for or --standing)")
    parser.add_argument("--body", default="", help="Epic body text")
    parser.add_argument("--body-file", help="Read the epic body from a file")
    parser.add_argument(
        "--repo",
        help=(
            "Target repo 'owner/repo' the epic is for (default: current repo). "
            "The epic home is the target's <org>/.github."
        ),
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Extra label (repeatable); 'epic' is always added",
    )
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    parser.add_argument(
        "--proxy-for",
        metavar="OWNER/REPO#N",
        help=(
            "Ensure the proxy epic for this primary epic (in another org) in the "
            "--repo target's org .github, and its 'Proxy:' comment on the primary"
        ),
    )
    parser.add_argument(
        "--standing",
        choices=STANDING_CHOICES,
        help=(
            "Ensure the --org org's standing epic of this kind in <org>/.github "
            "(create-if-missing, idempotent) and print its ref; creates no task"
        ),
    )
    parser.add_argument(
        "--org", help="The org whose standing epic --standing ensures (--standing only)"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
