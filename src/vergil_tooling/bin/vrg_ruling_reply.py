"""The human's side of an authenticated ruling on a task issue (epic .github#889).

``vrg-ruling-reply --issue OWNER/REPO#N --request ID [--response-text TEXT]``
posts the human's reply to the agent's request, **as the human**. It is a
human-only command, run in the human's own shell on the host, logged in as
themselves, the way ``vrg-submit-pr`` is run; never through ``!`` in an agent
session, whose shell carries the agent's identity.

In order, it:

1. refuses in an agent identity mode (before parsing, so ``--help`` also
   redirects);
2. refuses unless stdin and stdout are interactive terminals;
3. refuses unless the login its credentials post as (``gh api user``) is the
   one human login of the trusted-authors configuration;
4. refuses unless the request is the issue's open request and has no reply
   yet (a fatal turn check exits 4);
5. reads the reply **once**: ``--response-text``, else the agent's draft from
   the request's draft ref (no draft is a refusal);
6. refuses a reply that is not plain text (printable ASCII plus newline, no
   marker prefix) or is blank;
7. prints the reply exactly as it will be posted, with its byte length and
   SHA-256, and asks for confirmation;
8. on yes, reads the issue again and refuses, posting nothing, if its trusted
   comments changed while the prompt waited or the checks no longer hold;
9. posts exactly the bytes it showed, once, then deletes the request's draft
   ref. On no, it exits 1 and keeps the draft, so the command can be run
   again.

A draft pushed again while the prompt waits is never what is posted: the
reply is read once, in step 5, and only those bytes are posted.

Every GitHub call runs scoped to the issue's owner. Exit codes: ``0`` the
reply is posted, ``1`` any refusal or error (a declined prompt included),
``2`` a usage error, ``4`` a fatal turn check.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import confirm, github, identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.rulings import RulingError, comments, draft_ref, human_post
from vergil_tooling.lib.rulings.exchange import FatalCheck, open_request, read_reply
from vergil_tooling.lib.rulings.identity import human_login
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

_PROG = "vrg-ruling-reply"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_FATAL = 4

_AGENT_HELP = """\
vrg-ruling-reply is a human-only command: it posts the human's reply to a
ruling request as the human, and agent identities cannot run it.

The human runs it in their own shell on the host, never through '!' in an
agent session. The agent drafts the reply with 'vrg-ruling draft', which
prints the command for the human to run, and reads the posted reply with
'vrg-ruling read'.
"""

_AGENT_REFUSAL = (
    f"{_PROG}: this is a human-only command and cannot run in an agent identity;"
    " the human runs it in their own shell. Draft the reply with 'vrg-ruling draft'."
)


@cli_main
def main(argv: list[str] | None = None) -> int:
    # The identity gate runs before parsing so an agent's --help reaches it,
    # as vrg-submit-pr's does.
    if identity_mode.is_agent():
        raw_args = sys.argv[1:] if argv is None else argv
        if {"-h", "--help"} & set(raw_args):
            print(_AGENT_HELP)
            return EXIT_OK
        print(_AGENT_REFUSAL, file=sys.stderr)
        return EXIT_ERROR

    args = parse_args(argv)
    try:
        if not _is_interactive():
            msg = (
                "refusing to run without an interactive terminal: the reply is shown"
                " and confirmed at a prompt before it is posted"
            )
            raise RulingError(msg)
        authors = load_trusted_authors()
        human = human_login(authors)
        repo, issue = comments.parse_issue_ref(args.issue)
        with github.target_org(repo.split("/", 1)[0]):
            return _reply(authors, human, repo, issue, args.request, args.response_text)
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


# The terminal check, named here so the tests can stand in for a terminal.
_is_interactive = human_post.is_interactive


class _Issue:
    """The issue and the exchange the reply answers."""

    def __init__(
        self, authors: TrustedAuthors, human: str, repo: str, issue: int, request_id: int
    ) -> None:
        self.authors = authors
        self.human = human
        self.agent = f"{human}-vergil-user"
        self.repo = repo
        self.issue = issue
        self.request_id = request_id
        self.where = f"{repo}#{issue}"
        if self.agent not in authors.agents:
            msg = f"the agent login {self.agent} is not on the trusted authors' agents list"
            raise RulingError(msg)

    def fetch(self) -> tuple[list[comments.IssueComment], frozenset[int]]:
        """The issue's trusted comment stream and every comment ID on it."""
        every = comments.fetch_comments(self.repo, self.issue)
        return comments.trusted(every, self.authors), frozenset(c.id for c in every)

    def check_turn(self, stream: list[comments.IssueComment], all_ids: frozenset[int]) -> None:
        """Refuse unless the request is open and waiting for this reply.

        Refuses when the request is not the issue's open request (answered, or
        the turn has moved on) or already has a reply; a broken exchange is a
        :class:`FatalCheck`.
        """
        pending = open_request(stream, self.agent)
        if pending != self.request_id:
            msg = (
                f"request {self.request_id} is not the open request on {self.where};"
                " it is already answered or the turn has moved on. Nothing was posted"
            )
            raise RulingError(msg)
        reply = read_reply(
            stream,
            request_id=self.request_id,
            agent=self.agent,
            human=self.human,
            all_ids=all_ids,
        )
        if reply is not None:
            msg = (
                f"request {self.request_id} on {self.where} already has reply {reply.id};"
                " nothing was posted"
            )
            raise RulingError(msg)


def _read_reply_once(ctx: _Issue, response_text: str | None) -> str:
    """The reply to post: ``--response-text``, else the request's draft, read once."""
    if response_text is not None:
        reply = response_text
    else:
        draft = draft_ref.fetch_draft(ctx.repo, ctx.issue, ctx.request_id)
        if draft is None:
            msg = (
                f"there is no draft reply for request {ctx.request_id} on {ctx.where}"
                f" ({draft_ref.draft_ref(ctx.issue, ctx.request_id)}); ask the agent to"
                " push one with vrg-ruling draft, or pass --response-text"
            )
            raise RulingError(msg)
        reply = draft
    check_plain_text(reply, what="the reply")
    if not reply.strip():
        msg = "the reply is blank; nothing was posted"
        raise RulingError(msg)
    return reply


def _reply(
    authors: TrustedAuthors,
    human: str,
    repo: str,
    issue: int,
    request_id: int,
    response_text: str | None,
) -> int:
    human_post.require_login(human, _PROG)
    ctx = _Issue(authors, human, repo, issue, request_id)
    shown_stream, all_ids = ctx.fetch()
    ctx.check_turn(shown_stream, all_ids)
    reply = _read_reply_once(ctx, response_text)
    human_post.show(reply, "the reply")
    if not confirm.confirm(f"Post this reply as {human}?", assume_yes=False):
        print(f"{_PROG}: declined; nothing was posted and the draft is kept", file=sys.stderr)
        return EXIT_ERROR

    # The issue may have moved while the prompt waited: any change to its
    # trusted comments refuses, and the checks run again on what is there now.
    stream, all_ids = ctx.fetch()
    if stream != shown_stream:
        msg = (
            f"the trusted comments on {ctx.where} changed while the prompt waited;"
            f" nothing was posted. Re-run {_PROG} to see the issue's state"
        )
        raise RulingError(msg)
    ctx.check_turn(stream, all_ids)

    try:
        comment_id = post_comment_once(repo, issue, reply, author=human)
    except PostNotLanded as exc:
        msg = f"{exc}. Re-running {_PROG} is safe"
        raise RulingError(msg) from exc
    except PostOutcomeUnknown as exc:
        msg = (
            f"{exc}. Re-run {_PROG} to settle it: if the reply landed, the re-run"
            " is refused because the request is already answered"
        )
        raise RulingError(msg) from exc
    print(f"{_PROG}: posted reply {comment_id} to request {request_id} on {ctx.where}")

    try:
        draft_ref.delete_draft_api(repo, issue, request_id)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        msg = (
            f"the reply {comment_id} is posted, but deleting the draft ref"
            f" {draft_ref.draft_ref(issue, request_id)} on {repo} failed: {exc}."
            f" A re-run of {_PROG} is refused now that the request is answered;"
            " vrg-ruling respond deletes the leftover ref"
        )
        raise RulingError(msg) from exc
    return EXIT_OK


# --- arguments --------------------------------------------------------------


def _comment_id(text: str) -> int:
    if not text.isascii() or not text.isdigit() or text.startswith("0"):
        msg = "must be a positive comment ID"
        raise argparse.ArgumentTypeError(msg)
    return int(text)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Post the human's reply to a ruling request on a task issue, as the human. "
            "A human-only command: run it in your own shell, never through '!' in an "
            "agent session. It shows the exact reply (the agent's draft, or your own "
            "--response-text) with its byte length and SHA-256, asks for confirmation, "
            "re-checks the issue and posts exactly what it showed, once."
        ),
        epilog=(
            "Exit codes: 0 posted, 1 refused, declined or error, 2 usage error, "
            "4 a fatal turn check."
        ),
    )
    parser.add_argument("--issue", required=True, help="The task issue, owner/repo#N")
    parser.add_argument(
        "--request", required=True, type=_comment_id, help="The request's comment ID"
    )
    parser.add_argument(
        "--response-text",
        default=None,
        help="Post this text instead of the agent's draft (printable ASCII and newline only)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
