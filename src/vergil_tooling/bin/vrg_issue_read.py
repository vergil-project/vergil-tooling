"""Print an issue's or PR's trusted content, or the trusted results of a search or list.

``vrg-issue-read`` is the read command skills use instead of raw ``vrg-gh``
body, comment, review, search and list reads (epic vergil-project/.github#1467,
spec §6.4). Every trust decision is the trust layer's
(:mod:`vergil_tooling.lib.trust`); this command only prints its answers.

**One item:** ``vrg-issue-read OWNER/REPO#N [--pr] [--json]
[--allow-untrusted-body] [--roles human,agents,automation]``.

- Text: the trusted body, then each trusted comment (and, with ``--pr``, each
  trusted review and inline review comment), each under a header naming its
  ID, its author's role and its time.
- ``--json``: ``{"repo", "number", "issue": <IssueContent>, "reviews":
  <PRReviewContent or null>}``.
- The body must be trusted: an untrusted body (an outsider-opened item, or a
  body an untrusted identity edited) prints the stop error on stderr and exits
  3, before any content is printed. ``--allow-untrusted-body`` is only for
  listing or display reads: the body is then left out and reported.

**A search or a list:** ``vrg-issue-read (--repo OWNER/REPO | --owner ORG)
[--search QUERY] [--pr] [--state open|closed|merged|all] [--json] [--roles
...]``. It prints only the results whose body and title the layer trusts (the
body's author and editors, and everyone who renamed the item). Without
``--search`` a ``--repo`` read lists the repository's issues (or, with
``--pr``, its PRs) newest first; with ``--search``, or with ``--owner``, it runs
GitHub's search, scoped to the repo or org by the flag. ``--state`` defaults to
``open``.

- Text: one line per result, ``<repo>#<n> [<state>] <title> (<labels>)
  <url>``, and a closing count of trusted and skipped results on stderr.
- ``--json``: an array of ``{"repo", "number", "kind", "title", "url",
  "state", "labels", "author_role"}``, and for a PR ``"linked_issues"``: the
  issues its trusted body links by a ``Ref`` or ``Closes`` line.

Every untrusted item is reported on stderr, metadata only (repo, number, kind,
login, time and field), never its text. A read failure (an API error, a
missing field, a page not read in full, a search matching more results than
GitHub returns) prints ``ERROR:`` on stderr and exits 1, with nothing on
stdout. Bad arguments exit 2.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github, trust
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.linkage import LINKAGE_RE
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

EXIT_READ_FAILURE = 1
EXIT_UNTRUSTED_BODY = 3

_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_NAME = r"[A-Za-z0-9._-]+"
_OWNER_RE = re.compile(rf"^{_OWNER}$")
_REPO_RE = re.compile(rf"^(?P<owner>{_OWNER})/(?P<name>{_NAME})$")
_TARGET_RE = re.compile(rf"^(?P<repo>{_OWNER}/{_NAME})#(?P<number>[1-9][0-9]*)$")

# The flags set a search's scope; a query that names its own would widen it
# (GitHub ORs repeated scope qualifiers).
_SCOPE_QUALIFIER_RE = re.compile(r"(?:^|[\s(])-?(?:repo|org|user|owner):", re.IGNORECASE)

_STATES = ("open", "closed", "merged", "all")
# GitHub search qualifiers for each --state.
_SEARCH_STATES = {"open": "is:open", "closed": "is:closed", "merged": "is:merged", "all": ""}


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        authors = load_trusted_authors()
        if args.target is not None:
            return _read_one(args, authors)
        return _read_many(args, authors)
    except github.MissingGitHubTokenError:
        raise  # cli_main prints the auth message
    except trust.TrustReadError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_READ_FAILURE
    except (TrustedAuthorsNotConfiguredError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_READ_FAILURE


# --- one item -------------------------------------------------------------------


def _read_one(args: argparse.Namespace, authors: TrustedAuthors) -> int:
    repo, number = cast("tuple[str, int]", args.target)
    try:
        with github.target_org(repo.partition("/")[0]):
            content = trust.read_issue(
                repo,
                number,
                roles=args.roles,
                require_body=not args.allow_untrusted_body,
                authors=authors,
            )
            reviews = (
                trust.read_reviews(repo, number, roles=args.roles, authors=authors)
                if args.pr
                else None
            )
    except trust.UntrustedContentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_UNTRUSTED_BODY
    skipped = list(content.skipped) + ([] if reviews is None else list(reviews.skipped))
    if args.json:
        print(
            json.dumps(
                {
                    "repo": repo,
                    "number": number,
                    "issue": asdict(content),
                    "reviews": None if reviews is None else asdict(reviews),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_item(repo, number, content, reviews))
    trust.report_skipped(skipped, stream=sys.stderr)
    return 0


def _block(header: str, body: str) -> str:
    return f"{header}\n{body}" if body else f"{header}\n(empty)"


def format_item(
    repo: str,
    number: int,
    content: trust.IssueContent,
    reviews: trust.PRReviewContent | None,
) -> str:
    """The text form of one item: the trusted body, then each trusted comment and review."""
    blocks = []
    if content.body is None:
        blocks.append(f"== {repo}#{number} body: left out, untrusted (reported on stderr)")
    else:
        blocks.append(_block(f"== {repo}#{number} body by {content.body_role}", content.body))
    for comment in content.comments:
        blocks.append(
            _block(f"== comment {comment.id} by {comment.role} at {comment.at}", comment.body)
        )
    if reviews is not None:
        for review in reviews.reviews:
            blocks.append(
                _block(f"== review {review.id} by {review.role} at {review.at}", review.body)
            )
        for comment in reviews.review_comments:
            blocks.append(
                _block(
                    f"== review comment {comment.id} by {comment.role} at {comment.at}",
                    comment.body,
                )
            )
    return "\n\n".join(blocks)


# --- searches and lists ------------------------------------------------------------


def search_query(args: argparse.Namespace) -> str:
    """The GitHub search query for *args*: its scope, its state, then ``--search``."""
    scope = f"repo:{args.repo}" if args.repo else f"org:{args.owner}"
    parts = [scope, _SEARCH_STATES[args.state], args.search or ""]
    return " ".join(part for part in parts if part)


def _read_many(args: argparse.Namespace, authors: TrustedAuthors) -> int:
    kind: trust.ItemKind = "pr" if args.pr else "issue"
    org = args.repo.partition("/")[0] if args.repo else args.owner
    with github.target_org(org):
        if args.repo and args.search is None:
            hits, skipped = trust.list_items(
                args.repo, kind=kind, state=args.state, roles=args.roles, authors=authors
            )
        else:
            hits, skipped = trust.search_items(
                search_query(args), kind=kind, roles=args.roles, authors=authors
            )
    if args.json:
        print(json.dumps([hit_json(hit) for hit in hits], ensure_ascii=False, indent=2))
    else:
        for hit in hits:
            print(format_hit(hit))
    trust.report_skipped(skipped, stream=sys.stderr)
    print(f"{len(hits)} trusted result(s), {len(skipped)} skipped", file=sys.stderr)
    return 0


def linked_issues(hit: trust.TrustedHit) -> list[dict[str, Any]]:
    """The issues *hit*'s trusted body links by a ``Ref`` or ``Closes`` line."""
    links = []
    for match in LINKAGE_RE.finditer(hit.body):
        keyword = match["keyword"].lower()
        links.append(
            {
                "repo": match["repo"] or hit.repo,
                "number": int(match["number"]),
                "keyword": "Ref" if keyword == "ref" else "Closes",
            }
        )
    return links


def hit_json(hit: trust.TrustedHit) -> dict[str, Any]:
    """One result's JSON object; a PR's carries its ``linked_issues``."""
    obj: dict[str, Any] = {
        "repo": hit.repo,
        "number": hit.number,
        "kind": hit.kind,
        "title": hit.title,
        "url": hit.url,
        "state": hit.state,
        "labels": list(hit.labels),
        "author_role": str(hit.body_role),
    }
    if hit.kind == "pr":
        obj["linked_issues"] = linked_issues(hit)
    return obj


def format_hit(hit: trust.TrustedHit) -> str:
    """One result's text line."""
    labels = f" ({', '.join(hit.labels)})" if hit.labels else ""
    return f"{hit.repo}#{hit.number} [{hit.state}] {hit.title}{labels} {hit.url}"


# --- arguments ----------------------------------------------------------------------


def _target_arg(value: str) -> tuple[str, int]:
    match = _TARGET_RE.match(value)
    if match is None or match["repo"].split("/")[1] in {".", ".."}:
        msg = f"expected OWNER/REPO#N, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return match["repo"], int(match["number"])


def _repo_arg(value: str) -> str:
    match = _REPO_RE.match(value)
    if match is None or match["name"] in {".", ".."}:
        msg = f"expected OWNER/REPO, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _owner_arg(value: str) -> str:
    if _OWNER_RE.match(value) is None:
        msg = f"expected an org or user name, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _roles_arg(value: str) -> frozenset[trust.Role]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    known = {role.value for role in trust.Role}
    unknown = [name for name in names if name not in known]
    if not names or unknown:
        msg = f"expected a comma-separated list of {', '.join(sorted(known))}, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return frozenset(trust.Role(name) for name in names)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print an issue's or PR's trusted body, comments and reviews, or the trusted"
            " results of a search or list. Untrusted items are reported on stderr,"
            " metadata only. Exit 0 success, 1 read failure, 2 bad arguments, 3 untrusted"
            " body."
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        type=_target_arg,
        help="The issue or PR to read, as OWNER/REPO#N. Leave it out for a search or list.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--repo",
        type=_repo_arg,
        help="Search or list this repository's issues (or PRs, with --pr).",
    )
    scope.add_argument(
        "--owner",
        type=_owner_arg,
        help="Search every repository of this org (or user).",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help=(
            "GitHub search syntax (for example 'Fixture run in:title'), scoped by --repo or"
            " --owner; it may not name its own repo:, org:, user: or owner: qualifier."
        ),
    )
    parser.add_argument(
        "--state",
        choices=_STATES,
        help="The results' state, for a search or list (default: open; merged is PRs only).",
    )
    parser.add_argument(
        "--pr",
        action="store_true",
        help=(
            "One item: also read its reviews and inline review comments."
            " A search or list: pull requests instead of issues."
        ),
    )
    parser.add_argument(
        "--allow-untrusted-body",
        action="store_true",
        help=(
            "One item, for a listing or display read only: leave an untrusted body out and"
            " report it, instead of stopping with exit 3."
        ),
    )
    parser.add_argument(
        "--roles",
        type=_roles_arg,
        default=trust.ALL_ROLES,
        help="The roles to trust, comma-separated (default: human,agents,automation).",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args(argv)
    _check_mode(parser, args)
    return args


def _check_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    many = args.repo is not None or args.owner is not None
    if args.target is not None and many:
        parser.error("give OWNER/REPO#N, or --repo or --owner for a search or list, not both")
    if args.target is None and not many:
        parser.error("give OWNER/REPO#N, or --repo or --owner for a search or list")
    if args.target is not None:
        for flag, value in (("--search", args.search), ("--state", args.state)):
            if value is not None:
                parser.error(f"{flag} is for a search or list, not one item")
        return
    if args.allow_untrusted_body:
        parser.error("--allow-untrusted-body is for one item, not a search or list")
    if args.search is not None:
        if not args.search.strip():
            parser.error("--search needs a query")
        if _SCOPE_QUALIFIER_RE.search(args.search):
            parser.error(
                "--search may not name a repo:, org:, user: or owner: qualifier:"
                " --repo or --owner sets the scope"
            )
    if args.state is None:
        args.state = "open"
    if args.state == "merged" and not args.pr:
        parser.error("--state merged is for pull requests: add --pr")


if __name__ == "__main__":
    sys.exit(main())
