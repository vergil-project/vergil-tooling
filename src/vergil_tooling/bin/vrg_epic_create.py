"""Create a top-level epic issue in its resolved home repo.

Epics are top-level (no parent), labelled ``epic`` plus the derived category
label (:func:`vergil_tooling.lib.epics.category_labels`): ``epic-development``
for a plain epic, ``epic-management`` for a queue or hoshin epic, none beyond
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
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github, proxies
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.labels import ensure_labels_or_report


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.proxy_for is not None:
        return _proxy_for(args)
    if args.title is None:
        print("vrg-epic-create: --title is required (unless --proxy-for)", file=sys.stderr)
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


def _proxy_for(args: argparse.Namespace) -> int:
    """``--proxy-for``: ensure the proxy and its comment, and report what was done."""
    derived = [
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
            "applied too: 'epic-management' for an intake, planning, deferred or "
            "hoshin epic, 'epic-development' for a plain epic; neither may be given "
            "with --label, and labels marking two categories are refused. With "
            "--proxy-for OWNER/REPO#N --repo OWNER/REPO it instead ensures the "
            "proxy epic for that primary epic in the target's org .github, and "
            "the 'Proxy:' comment on the primary; both repos must be public and "
            "in different orgs."
        ),
    )
    parser.add_argument("--title", help="Epic title (required unless --proxy-for)")
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
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
