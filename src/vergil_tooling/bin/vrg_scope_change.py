"""Post a scope change on a task issue, as the human (vergil-tooling#4020).

``vrg-scope-change --issue OWNER/REPO#N [--text TEXT]`` posts a maintainer's
change to a task's scope (an addition, a narrowing, a ruling on an item
awaiting one) as a comment on the task issue, **as the human**. Merge review
counts only scope the human posted as themselves, because the implementing
agent shares the agent identity and could otherwise widen its own scope.

It is a human-only command, run in the human's own shell on the host, logged
in as themselves, the way ``vrg-ruling-reply`` is run; never through ``!`` in
an agent session, whose shell carries the agent's identity.

In order, it:

1. refuses in an agent identity mode (before parsing, so ``--help`` also
   redirects);
2. refuses unless stdin and stdout are interactive terminals;
3. refuses unless the login its credentials post as (``gh api user``) is the
   one human login of the trusted-authors configuration;
4. refuses while the issue has an open ruling exchange (a request with no
   response yet), since a human comment there would break its turn order;
5. reads the text **once**: ``--text``, else the agent's draft from the
   issue's scope draft ref (no draft is a refusal);
6. refuses text that is not plain text or is blank, and renders the comment:
   the header ``Scope change``, a blank line and the text;
7. if the human has already posted exactly this comment on the issue, says
   so, deletes a draft it came from, posts nothing and exits 0;
8. prints the comment exactly as it will be posted, with its byte length and
   SHA-256, and asks for confirmation;
9. on yes, reads the issue again and refuses, posting nothing, if its trusted
   comments changed while the prompt waited;
10. posts exactly the bytes it showed, once, then deletes the scope draft ref
    when the text came from it. On no, it exits 1 and keeps the draft.

Every GitHub call runs scoped to the issue's owner. Exit codes: ``0`` the
scope change is posted (or was already), ``1`` any refusal or error (a
declined prompt included), ``2`` a usage error.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import confirm, github, identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.rulings import RulingError, comments, draft_ref, human_post
from vergil_tooling.lib.rulings.exchange import open_request
from vergil_tooling.lib.rulings.identity import human_login
from vergil_tooling.lib.rulings.post import (
    PostNotLanded,
    PostOutcomeUnknown,
    post_comment_once,
)
from vergil_tooling.lib.rulings.scope import render_scope_change
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_PROG = "vrg-scope-change"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

_AGENT_HELP = """\
vrg-scope-change is a human-only command: it posts a scope change on a task
issue as the human, and agent identities cannot run it.

The human runs it in their own shell on the host, never through '!' in an
agent session. The agent drafts the scope change with
'vrg-ruling draft-scope', which prints the command for the human to run.
"""

_AGENT_REFUSAL = (
    f"{_PROG}: this is a human-only command and cannot run in an agent identity;"
    " the human runs it in their own shell. Draft the scope change with"
    " 'vrg-ruling draft-scope'."
)


@cli_main
def main(argv: list[str] | None = None) -> int:
    # The identity gate runs before parsing so an agent's --help reaches it,
    # as vrg-ruling-reply's does.
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
                "refusing to run without an interactive terminal: the scope change is"
                " shown and confirmed at a prompt before it is posted"
            )
            raise RulingError(msg)
        authors = load_trusted_authors()
        human = human_login(authors)
        repo, issue = comments.parse_issue_ref(args.issue)
        with github.target_org(repo.split("/", 1)[0]):
            return _post(_Issue(authors, human, repo, issue), args.text)
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
    """The task issue the scope change is posted on."""

    def __init__(self, authors: TrustedAuthors, human: str, repo: str, issue: int) -> None:
        self.authors = authors
        self.human = human
        self.agent = f"{human}-vergil-user"
        self.repo = repo
        self.issue = issue
        self.where = f"{repo}#{issue}"
        if self.agent not in authors.agents:
            msg = f"the agent login {self.agent} is not on the trusted authors' agents list"
            raise RulingError(msg)

    def fetch(self) -> list[comments.IssueComment]:
        """The issue's trusted comment stream."""
        return comments.trusted(comments.fetch_comments(self.repo, self.issue), self.authors)

    def check_no_open_exchange(self, stream: list[comments.IssueComment]) -> None:
        """Refuse while a ruling request on the issue has no response yet.

        Between a request and its response, the only human comment allowed is
        the reply; a scope change there would be read as the reply or break
        the turn order.
        """
        pending = open_request(stream, self.agent)
        if pending is not None:
            msg = (
                f"{self.where} has open ruling request {pending}; a scope change cannot"
                " be posted until its exchange is closed by the agent's response."
                " Nothing was posted"
            )
            raise RulingError(msg)


def _read_text_once(ctx: _Issue, text: str | None) -> tuple[str, bool]:
    """The comment to post and whether its text came from the draft ref, read once."""
    from_draft = text is None
    if text is None:
        text = draft_ref.fetch_scope_draft(ctx.repo, ctx.issue)
        if text is None:
            msg = (
                f"there is no draft scope change for {ctx.where}"
                f" ({draft_ref.scope_draft_ref(ctx.issue)}); ask the agent to push one"
                " with vrg-ruling draft-scope, or pass --text"
            )
            raise RulingError(msg)
    return render_scope_change(text), from_draft


def _delete_draft(ctx: _Issue, posted: str) -> None:
    try:
        draft_ref.delete_scope_draft_api(ctx.repo, ctx.issue)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        msg = (
            f"{posted}, but deleting the draft ref {draft_ref.scope_draft_ref(ctx.issue)}"
            f" on {ctx.repo} failed: {exc}. Re-running {_PROG} posts nothing and"
            " retries the deletion"
        )
        raise RulingError(msg) from exc


def _already_posted(ctx: _Issue, stream: list[comments.IssueComment], body: str) -> int | None:
    """The ID of the human's comment on the issue whose body is exactly ``body``."""
    for comment in stream:
        if comment.author == ctx.human and comment.body == body:
            return comment.id
    return None


def _post(ctx: _Issue, text: str | None) -> int:
    human_post.require_login(ctx.human, _PROG)
    shown_stream = ctx.fetch()
    ctx.check_no_open_exchange(shown_stream)
    body, from_draft = _read_text_once(ctx, text)

    existing = _already_posted(ctx, shown_stream, body)
    if existing is not None:
        print(
            f"{_PROG}: this scope change is already posted on {ctx.where} as comment"
            f" {existing}; nothing was posted",
            file=sys.stderr,
        )
        if from_draft:
            _delete_draft(ctx, f"the scope change {existing} is already posted")
        print(existing)
        return EXIT_OK

    human_post.show(body, "the scope change")
    if not confirm.confirm(f"Post this scope change as {ctx.human}?", assume_yes=False):
        print(f"{_PROG}: declined; nothing was posted and the draft is kept", file=sys.stderr)
        return EXIT_ERROR

    # The issue may have moved while the prompt waited: any change to its
    # trusted comments refuses, so the human re-runs and sees it.
    if ctx.fetch() != shown_stream:
        msg = (
            f"the trusted comments on {ctx.where} changed while the prompt waited;"
            f" nothing was posted. Re-run {_PROG} to see the issue's state"
        )
        raise RulingError(msg)

    try:
        comment_id = post_comment_once(ctx.repo, ctx.issue, body, author=ctx.human)
    except PostNotLanded as exc:
        msg = f"{exc}. Re-running {_PROG} is safe"
        raise RulingError(msg) from exc
    except PostOutcomeUnknown as exc:
        msg = (
            f"{exc}. Re-run {_PROG} to settle it: if the scope change landed, the"
            " re-run finds it and posts nothing"
        )
        raise RulingError(msg) from exc
    print(f"{_PROG}: posted scope change {comment_id} on {ctx.where}", file=sys.stderr)
    print(comment_id)
    if from_draft:
        _delete_draft(ctx, f"the scope change {comment_id} is posted")
    return EXIT_OK


# --- arguments --------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Post a scope change on a task issue, as the human. A human-only command: "
            "run it in your own shell, never through '!' in an agent session. It shows "
            "the exact comment (the agent's draft, or your own --text, under the header "
            "'Scope change') with its byte length and SHA-256, asks for confirmation, "
            "re-checks the issue and posts exactly what it showed, once."
        ),
        epilog="Exit codes: 0 posted (or already posted), 1 refused, declined or error, 2 usage.",
    )
    parser.add_argument("--issue", required=True, help="The task issue, owner/repo#N")
    parser.add_argument(
        "--text",
        default=None,
        help=(
            "Post this text instead of the agent's draft (printable ASCII and newline "
            "only; the header is added for you)"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
