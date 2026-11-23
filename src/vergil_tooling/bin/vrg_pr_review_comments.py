"""List a pull request's inline (line-level) review comments, read-only.

``vrg-pr-review-comments --repo <owner>/<repo> --pr <N> [--json]`` reads
``GET repos/{owner}/{repo}/pulls/{n}/comments``, every page of it, and prints
each inline review comment: its ID, author, path, line, review ID, reply-to
ID, created time, body and URL. ``vrg-gh pr view --json reviews,comments``
returns only review bodies and conversation comments, and the user identity
may not run ``gh api``, so this is the user agent's one way to read them
(vergil-tooling#4105). The ``epic-retrospective`` sweep of the plugin
consumes it.

Only trusted comments are printed (GHSA-q6hx-8c85-9f56): the PR's reviews
and inline comments are also read through the trust layer
(:func:`vergil_tooling.lib.trust.read_reviews`), and a comment is kept only
when it is one the layer trusts (its author and every editor hold a role),
with the body the layer read. Every other comment is left out and reported on
stderr, metadata only, never its text.

The command is strictly read-only: it makes one paginated ``GET`` through
:func:`vergil_tooling.lib.github.read_json_paginated` and the trust layer's
GraphQL reads, and nothing else. It widens no ``vrg-gh`` allowlist.

- Without ``--json`` it prints one block per comment, oldest first as the API
  returns them, or a single line saying the PR has none.
- ``--json`` prints a JSON array of objects with the keys ``id``, ``author``,
  ``path``, ``line``, ``review_id``, ``in_reply_to_id``, ``created_at``,
  ``body`` and ``url``. ``line`` is ``null`` for a comment that is not on a
  current line of the diff: a file-level comment, or an outdated one on a line
  the PR no longer has. ``author`` is ``null`` for a deleted
  account, ``review_id`` is ``null`` when GitHub gives none, and
  ``in_reply_to_id`` is ``null`` for a comment that starts a thread.

A GitHub API failure, a response that is not the expected list of
comments, or a trust-layer read that cannot complete (or a missing
trusted-authors configuration) prints ``ERROR:`` on stderr and exits 1; it never prints a partial
or empty list in its place. Bad arguments exit 2.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from typing import cast

from vergil_tooling.lib import github, trust
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

# GitHub owner (user or org) names are alphanumeric with single inner hyphens;
# repo names add ``.`` and ``_``. The repo may not be ``.`` or ``..``, so the
# endpoint path can never be steered off ``repos/<owner>/<repo>/``.
_REPO_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/(?P<name>[A-Za-z0-9._-]+)$"
)


@dataclass(frozen=True)
class ReviewComment:
    """One inline review comment, in the shape this command prints."""

    id: int
    author: str | None
    path: str
    line: int | None
    review_id: int | None
    in_reply_to_id: int | None
    created_at: str
    body: str
    url: str


class MalformedCommentError(ValueError):
    """The API returned something that is not a well-formed review comment."""


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    owner = args.repo.split("/", 1)[0]
    try:
        with github.target_org(owner):
            comments = fetch_review_comments(args.repo, args.pr)
    except github.MissingGitHubTokenError:
        raise  # cli_main prints the auth message
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: failed to read the review comments of {args.repo}#{args.pr}:"
            f" {error_text(exc)}",
            file=sys.stderr,
        )
        return 1
    except (trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
        print(
            f"ERROR: cannot judge the review comments of {args.repo}#{args.pr}: {exc}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(
            f"ERROR: malformed review comments response for {args.repo}#{args.pr}: {exc}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps([asdict(comment) for comment in comments], indent=2))
    else:
        print(format_text(args.repo, args.pr, comments))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List a pull request's inline (line-level) review comments, read-only"
            " (GET repos/{owner}/{repo}/pulls/{n}/comments, every page)."
        ),
    )
    parser.add_argument(
        "--repo",
        required=True,
        type=_repo_arg,
        help="The PR's repository, as <owner>/<repo>.",
    )
    parser.add_argument(
        "--pr",
        required=True,
        type=_pr_arg,
        help="The pull request number.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a JSON array of comment objects instead of text.",
    )
    return parser.parse_args(argv)


def _repo_arg(value: str) -> str:
    match = _REPO_RE.match(value)
    if match is None or match.group("name") in {".", ".."}:
        msg = f"expected <owner>/<repo>, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _pr_arg(value: str) -> int:
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        msg = f"expected a positive PR number, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return int(value)


def fetch_review_comments(repo: str, pr: int) -> list[ReviewComment]:
    """Every trusted inline review comment on *repo*'s PR *pr*, in API order.

    The REST listing gives each comment's place (path, line, review, thread
    and URL); the trust layer decides which comments count and gives their
    bodies (:func:`trust.read_reviews`). A comment it does not trust is left
    out, and the untrusted ones are reported on stderr, metadata only.
    """
    items = github.read_json_paginated(f"repos/{repo}/pulls/{pr}/comments")
    listed = [parse_comment(item, index) for index, item in enumerate(items)]
    content = trust.read_reviews(repo, pr)
    trust.report_skipped(content.skipped, stream=sys.stderr)
    bodies = {comment.id: comment.body for comment in content.review_comments}
    return [
        replace(comment, body=bodies[str(comment.id)])
        for comment in listed
        if str(comment.id) in bodies
    ]


def parse_comment(item: object, index: int) -> ReviewComment:
    """Project one API comment object onto :class:`ReviewComment`.

    Raises :class:`MalformedCommentError` naming the field when the object
    lacks a required field or holds one of the wrong type.
    """
    if not isinstance(item, dict):
        msg = f"item {index} is not an object: {type(item).__name__}"
        raise MalformedCommentError(msg)
    data = cast("dict[str, object]", item)
    user = data.get("user")
    if user is None:
        author = None
    elif isinstance(user, dict):
        author = _required(cast("dict[str, object]", user), "login", str, f"item {index} user")
    else:
        msg = f"item {index} field 'user' is not an object: {type(user).__name__}"
        raise MalformedCommentError(msg)
    where = f"item {index}"
    return ReviewComment(
        id=_required(data, "id", int, where),
        author=author,
        path=_required(data, "path", str, where),
        line=_optional_int(data, "line", where),
        review_id=_optional_int(data, "pull_request_review_id", where),
        in_reply_to_id=_optional_int(data, "in_reply_to_id", where),
        created_at=_required(data, "created_at", str, where),
        body=_required(data, "body", str, where),
        url=_required(data, "html_url", str, where),
    )


def _required[T](data: dict[str, object], key: str, kind: type[T], where: str) -> T:
    value = data.get(key)
    # bool is an int subclass; a boolean is never a valid ID or line.
    if not isinstance(value, kind) or isinstance(value, bool):
        shown = "missing" if key not in data else f"a {type(value).__name__}"
        msg = f"{where} field {key!r} is {shown}, expected {kind.__name__}"
        raise MalformedCommentError(msg)
    return value


def _optional_int(data: dict[str, object], key: str, where: str) -> int | None:
    if data.get(key) is None:
        return None
    return _required(data, key, int, where)


def format_text(repo: str, pr: int, comments: list[ReviewComment]) -> str:
    """The human-readable listing: one block per comment, blank-line separated."""
    if not comments:
        return f"{repo}#{pr} has no inline review comments."
    blocks = []
    for comment in comments:
        # A null line is a file-level comment or an outdated one; the API's
        # line field alone cannot tell which, so name neither.
        where = comment.path if comment.line is None else f"{comment.path}:{comment.line}"
        refs = [f"comment {comment.id}"]
        if comment.review_id is not None:
            refs.append(f"review {comment.review_id}")
        if comment.in_reply_to_id is not None:
            refs.append(f"reply to {comment.in_reply_to_id}")
        author = comment.author if comment.author is not None else "(deleted user)"
        header = f"{where} by {author} at {comment.created_at} ({', '.join(refs)})"
        body = "\n".join(f"    {text}" for text in comment.body.splitlines()) or "    (empty)"
        blocks.append(f"{header}\n{comment.url}\n{body}")
    return "\n\n".join(blocks)


if __name__ == "__main__":
    sys.exit(main())
