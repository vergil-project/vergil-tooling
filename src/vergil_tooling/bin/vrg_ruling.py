"""The agent's side of an authenticated ruling on a task issue (epic .github#889).

One round on a task issue is a **request** (posted here), the human's
**reply** (posted by ``vrg-ruling-reply``, as the human) and a **response**
(posted here). The subcommands:

- ``request --issue --items`` posts a request from a request items file and
  prints its comment ID. It refuses while the issue has an open exchange,
  naming that request's ID so a resumed run can pick it up.
- ``read --issue --request`` runs every turn check and prints the human's
  reply on stdout (its comment ID goes to stderr). Exit ``3`` means no reply
  yet; ``4`` is a fatal check, named with its comment.
- ``respond --issue --request --reply --items`` re-runs ``read``'s checks,
  then posts the response, which records the request and reply comment IDs.
  A re-run that finds its own response already landed reports that ID, posts
  nothing and succeeds.

Every subcommand runs only in the ``user`` identity mode, as the derived
agent login ``<the human login>-vergil-user``, which must be on the trusted
authors' ``agents`` list. Every GitHub call runs scoped to the issue's owner,
whatever the clone's ``origin`` is. Every comment is posted exactly once.

Exit codes: ``0`` success (or a valid reply), ``1`` any other refusal or
error, ``2`` a usage error, ``3`` no reply yet, ``4`` a fatal check.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.rulings import RulingError, comments
from vergil_tooling.lib.rulings.exchange import (
    FatalCheck,
    check_respond,
    open_request,
    read_reply,
)
from vergil_tooling.lib.rulings.identity import agent_login, human_login
from vergil_tooling.lib.rulings.items import (
    load_request_items,
    load_response_items,
    render_request,
    render_response,
)
from vergil_tooling.lib.rulings.marker import Marker, parse_marker
from vergil_tooling.lib.rulings.plain_text import check_plain_text
from vergil_tooling.lib.rulings.post import (
    PostNotLanded,
    PostOutcomeUnknown,
    post_comment_once,
)
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_PROG = "vrg-ruling"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_REPLY = 3
EXIT_FATAL = 4


class _Context:
    """What every subcommand resolves before touching GitHub."""

    def __init__(self, authors: TrustedAuthors, agent: str, repo: str, issue: int) -> None:
        self.authors = authors
        self.agent = agent
        self.human = human_login(authors)
        self.repo = repo
        self.issue = issue
        self.where = f"{repo}#{issue}"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        authors = load_trusted_authors()
        agent = agent_login(authors, identity_mode.current_mode())
        repo, issue = comments.parse_issue_ref(args.issue)
        ctx = _Context(authors, agent, repo, issue)
        with github.target_org(repo.split("/", 1)[0]):
            if args.command == "request":
                return _request(ctx, Path(args.items))
            if args.command == "read":
                return _read(ctx, args.request)
            return _respond(ctx, args.request, args.reply, Path(args.items))
    except FatalCheck as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return EXIT_FATAL
    except (RulingError, TrustedAuthorsNotConfiguredError) as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except github.MissingGitHubTokenError:
        raise
    except (github.GitHubAPIError, RuntimeError, ValueError) as exc:
        # ValueError covers an invalid trusted-authors configuration and output
        # that is not JSON; RuntimeError covers GraphQL errors.
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _stream(ctx: _Context) -> tuple[list[comments.IssueComment], frozenset[int]]:
    """The issue's trusted comment stream and every comment ID on it."""
    every = comments.fetch_comments(ctx.repo, ctx.issue)
    return comments.trusted(every, ctx.authors), frozenset(c.id for c in every)


def _check_body(body: str, marker: Marker, ctx: _Context, kind: str) -> None:
    """Refuse a rendered body that does not read back as the intended comment.

    Parsed as the agent's own comment, it must give back exactly ``marker``,
    and everything before the marker line must be plain text with no marker
    prefix. Nothing is posted when this refuses.
    """
    probe = comments.IssueComment(1, ctx.agent, body, edited=False)
    if parse_marker(probe, ctx.agent) != marker:
        msg = f"the rendered {kind} does not end with its marker alone; nothing was posted"
        raise RulingError(msg)
    head, _, _marker_line = body.rpartition("\n")
    check_plain_text(head, what=f"the rendered {kind}")


def _request(ctx: _Context, items_path: Path) -> int:
    body = render_request(load_request_items(items_path))
    _check_body(body, Marker("request", None, None), ctx, "request")
    stream, _ids = _stream(ctx)
    pending = open_request(stream, ctx.agent)
    if pending is not None:
        msg = (
            f"{ctx.where} has open request {pending}; read it with"
            f" vrg-ruling read --issue {ctx.where} --request {pending}"
        )
        raise RulingError(msg)
    try:
        comment_id = post_comment_once(ctx.repo, ctx.issue, body, author=ctx.agent)
    except PostNotLanded as exc:
        msg = f"{exc}. Re-running the request is safe"
        raise RulingError(msg) from exc
    except PostOutcomeUnknown as exc:
        msg = (
            f"{exc}. Re-run vrg-ruling request to settle it: if the request landed,"
            " the re-run is refused and names it as the open request"
        )
        raise RulingError(msg) from exc
    print(comment_id)
    return EXIT_OK


def _read(ctx: _Context, request_id: int) -> int:
    stream, all_ids = _stream(ctx)
    reply = read_reply(
        stream, request_id=request_id, agent=ctx.agent, human=ctx.human, all_ids=all_ids
    )
    if reply is None:
        print(f"{_PROG}: no reply yet to request {request_id} on {ctx.where}", file=sys.stderr)
        return EXIT_NO_REPLY
    print(f"{_PROG}: reply {reply.id} to request {request_id} on {ctx.where}", file=sys.stderr)
    print(reply.body)
    return EXIT_OK


def _respond(ctx: _Context, request_id: int, reply_id: int, items_path: Path) -> int:
    body = render_response(
        load_response_items(items_path), request_id=request_id, reply_id=reply_id
    )
    _check_body(body, Marker("response", request_id, reply_id), ctx, "response")
    stream, all_ids = _stream(ctx)
    landed = check_respond(
        stream,
        request_id=request_id,
        reply_id=reply_id,
        agent=ctx.agent,
        human=ctx.human,
        all_ids=all_ids,
    )
    if landed is not None:
        print(
            f"{_PROG}: the response to request {request_id} and reply {reply_id}"
            f" already landed; nothing was posted",
            file=sys.stderr,
        )
        print(landed)
        _after_respond(ctx.repo, ctx.issue, request_id)
        return EXIT_OK
    try:
        comment_id = post_comment_once(ctx.repo, ctx.issue, body, author=ctx.agent)
    except PostNotLanded as exc:
        msg = f"{exc}. Re-running the response is safe"
        raise RulingError(msg) from exc
    except PostOutcomeUnknown as exc:
        msg = (
            f"{exc}. Re-run vrg-ruling respond to settle it: if the response landed,"
            " the re-run reports it and succeeds without posting"
        )
        raise RulingError(msg) from exc
    print(comment_id)
    _after_respond(ctx.repo, ctx.issue, request_id)
    return EXIT_OK


def _after_respond(repo: str, issue: int, request_id: int) -> None:
    """Clean up after a response: deleting the request's draft ref.

    The draft relay arrives in Task 3 of epic vergil-project/.github#889; until
    then there is no draft ref to delete, so this does nothing.
    """
    del repo, issue, request_id


# --- arguments --------------------------------------------------------------


def _comment_id(text: str) -> int:
    if not text.isascii() or not text.isdigit() or text.startswith("0"):
        msg = f"must be a positive comment ID, got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return int(text)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "The agent's side of an authenticated ruling on a task issue: post a "
            "request, read the human's reply with every turn check, and post the "
            "response. Each comment is posted exactly once."
        ),
        epilog=(
            "Exit codes: 0 success (or a valid reply), 1 any other refusal or error, "
            "2 usage error, 3 no reply yet, 4 a fatal check."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    request = sub.add_parser("request", help="Post a request from an items file")
    request.add_argument("--issue", required=True, help="The task issue, owner/repo#N")
    request.add_argument("--items", required=True, help="The request items file (JSON)")

    read = sub.add_parser("read", help="Read and check the human's reply to a request")
    read.add_argument("--issue", required=True, help="The task issue, owner/repo#N")
    read.add_argument("--request", required=True, type=_comment_id, help="The request's comment ID")

    respond = sub.add_parser("respond", help="Post the response to a request and its reply")
    respond.add_argument("--issue", required=True, help="The task issue, owner/repo#N")
    respond.add_argument(
        "--request", required=True, type=_comment_id, help="The request's comment ID"
    )
    respond.add_argument("--reply", required=True, type=_comment_id, help="The reply's comment ID")
    respond.add_argument("--items", required=True, help="The response items file (JSON)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
