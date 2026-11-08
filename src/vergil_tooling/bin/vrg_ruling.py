"""The agent's side of an authenticated ruling on a task issue (epic .github#889).

One round on a task issue is a **request** (posted here), the human's
**reply** (posted by ``vrg-ruling-reply``, as the human) and a **response**
(posted here). The subcommands:

- ``request --issue --items`` posts a request from a request items file and
  prints its comment ID. It refuses while the issue has an open exchange,
  naming that request's ID so a resumed run can pick it up.
- ``draft --issue --request --items`` builds the human's reply from a reply
  items file, pushes it to the request's draft ref on the issue's repo
  (``refs/vergil/ruling-draft/<issue>/<request ID>``) and prints the human's
  ``vrg-ruling-reply`` command. It runs only in a clone whose ``origin`` is
  the issue's repo, refuses once the request has a reply, and posts nothing.
- ``read --issue --request`` runs every turn check and prints the human's
  reply on stdout (its comment ID goes to stderr). With ``--json`` a valid
  reply prints ``{"request": ID, "reply": ID, "body": text}`` instead, and
  every other outcome leaves stdout empty. Exit ``3`` means no reply yet;
  ``4`` is a fatal check, named with its comment.
- ``respond --issue --request --reply --items`` re-runs ``read``'s checks,
  then posts the response, which records the request and reply comment IDs.
  A re-run that finds its own response already landed reports that ID, posts
  nothing and succeeds. Either way it then deletes the request's draft ref,
  if one is left.
- ``draft-scope --issue --text-file`` pushes a scope change drafted for the
  human, the text alone, to the issue's scope draft ref
  (``refs/vergil/scope-draft/<issue>``) and prints the human's
  ``vrg-scope-change`` command. Like ``draft``, it runs only in a clone whose
  ``origin`` is the issue's repo and posts nothing.

Every subcommand runs only in the ``user`` identity mode, as the derived
agent login ``<the human login>-vergil-user``, which must be on the trusted
authors' ``agents`` list. Every GitHub call runs scoped to the issue's owner,
whatever the clone's ``origin`` is. Every comment is posted exactly once.

Exit codes: ``0`` success (or a valid reply), ``1`` any other refusal or
error, ``2`` a usage error, ``3`` no reply yet, ``4`` a fatal check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.rulings import RulingError, comments, draft_ref
from vergil_tooling.lib.rulings.exchange import (
    FatalCheck,
    check_respond,
    open_request,
    read_reply,
)
from vergil_tooling.lib.rulings.identity import agent_login, human_login
from vergil_tooling.lib.rulings.items import (
    load_reply_items,
    load_request_items,
    load_response_items,
    render_reply,
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
from vergil_tooling.lib.rulings.scope import render_scope_change
from vergil_tooling.lib.shell_ref import quoted_issue_ref
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
                return _read(ctx, args.request, as_json=args.json)
            if args.command == "draft":
                return _draft(ctx, args.request, Path(args.items))
            if args.command == "draft-scope":
                return _draft_scope(ctx, Path(args.text_file))
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
            f" vrg-ruling read --issue {quoted_issue_ref(ctx.repo, ctx.issue)}"
            f" --request {pending}"
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


def _read(ctx: _Context, request_id: int, *, as_json: bool) -> int:
    stream, all_ids = _stream(ctx)
    reply = read_reply(
        stream, request_id=request_id, agent=ctx.agent, human=ctx.human, all_ids=all_ids
    )
    if reply is None:
        print(f"{_PROG}: no reply yet to request {request_id} on {ctx.where}", file=sys.stderr)
        return EXIT_NO_REPLY
    print(f"{_PROG}: reply {reply.id} to request {request_id} on {ctx.where}", file=sys.stderr)
    if as_json:
        print(json.dumps({"request": request_id, "reply": reply.id, "body": reply.body}))
    else:
        print(reply.body)
    return EXIT_OK


def _draft(ctx: _Context, request_id: int, items_path: Path) -> int:
    """Push the reply drafted from ``items_path`` to the request's draft ref.

    Everything is screened before anything is published: the items, the
    rendered reply, the clone's ``origin`` (the ref goes there, so it must be
    the issue's repo) and the request, which must still be waiting for a reply.
    """
    body = render_reply(load_reply_items(items_path))
    check_plain_text(body, what="the rendered reply")
    draft_ref.require_remote_is(ctx.repo, remote="origin")
    stream, all_ids = _stream(ctx)
    reply = read_reply(
        stream, request_id=request_id, agent=ctx.agent, human=ctx.human, all_ids=all_ids
    )
    if reply is not None:
        msg = f"request {request_id} on {ctx.where} already has reply {reply.id}; nothing pushed"
        raise RulingError(msg)
    draft_ref.push_draft(ctx.issue, request_id, body, remote="origin")
    print(
        f"{_PROG}: pushed the draft reply to {draft_ref.draft_ref(ctx.issue, request_id)}"
        f" on {ctx.repo}; the human posts it with:",
        file=sys.stderr,
    )
    print(draft_ref.reply_command(ctx.repo, ctx.issue, request_id))
    return EXIT_OK


def _draft_scope(ctx: _Context, text_path: Path) -> int:
    """Push the scope change in ``text_path`` to the issue's scope draft ref.

    The text is screened as ``vrg-scope-change`` will screen it, the clone's
    ``origin`` must be the issue's repo, and the issue must exist, before
    anything is published. The turn is not checked here: ``vrg-scope-change``
    refuses while a ruling exchange is open, when the human runs it.
    """
    try:
        text = text_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read the scope change file {text_path}: {exc}"
        raise RulingError(msg) from exc
    render_scope_change(text)
    draft_ref.require_remote_is(ctx.repo, remote="origin", command=f"{_PROG} draft-scope")
    comments.fetch_comments(ctx.repo, ctx.issue)
    draft_ref.push_scope_draft(ctx.issue, text, remote="origin")
    print(
        f"{_PROG}: pushed the draft scope change to {draft_ref.scope_draft_ref(ctx.issue)}"
        f" on {ctx.repo}; the human posts it with:",
        file=sys.stderr,
    )
    print(draft_ref.scope_command(ctx.repo, ctx.issue))
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
    """Clean up after a response: delete the request's draft ref, if one is left.

    The ref is addressed by the issue's repo, whatever the clone's ``origin``
    is; an absent ref is not an error. A failure here comes after the response
    landed, so the message says so: a re-run finds the response, posts nothing
    and retries the cleanup.
    """
    try:
        draft_ref.delete_draft_api(repo, issue, request_id)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        msg = (
            f"the response is posted, but deleting the draft ref"
            f" {draft_ref.draft_ref(issue, request_id)} on {repo} failed: {exc}."
            " Re-run vrg-ruling respond with the same arguments to retry it; it posts nothing"
        )
        raise RulingError(msg) from exc


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
            "request, relay the reply drafted for the human, read the human's reply "
            "with every turn check, and post the response. Each comment is posted "
            "exactly once."
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
    read.add_argument(
        "--json",
        action="store_true",
        help=(
            'On a valid reply, print {"request": ID, "reply": ID, "body": text} '
            "instead of the body; on any other outcome stdout stays empty"
        ),
    )

    draft = sub.add_parser("draft", help="Push the reply drafted for the human to its draft ref")
    draft.add_argument("--issue", required=True, help="The task issue, owner/repo#N")
    draft.add_argument(
        "--request", required=True, type=_comment_id, help="The request's comment ID"
    )
    draft.add_argument("--items", required=True, help="The reply items file (JSON)")

    draft_scope = sub.add_parser(
        "draft-scope", help="Push a scope change drafted for the human to its draft ref"
    )
    draft_scope.add_argument("--issue", required=True, help="The task issue, owner/repo#N")
    draft_scope.add_argument(
        "--text-file",
        required=True,
        help="The scope change's text alone (printable ASCII and newline only)",
    )

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
