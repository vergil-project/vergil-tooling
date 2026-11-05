"""Posting a comment exactly once, and finding out whether a failed post landed.

A retried POST that had already landed would post a duplicate, and a
duplicate request or reply is a fatal turn violation. So the POST goes through
:func:`vergil_tooling.lib.github.post_json_once`, bounded by a timeout, and is
never repeated. After any failure the issue is re-read, up to three times two
seconds apart:

- a new comment by the posting login whose body is exactly the posted body is
  the post that landed, and its ID is returned;
- a re-read that succeeds without it raises :class:`PostNotLanded`;
- when every re-read fails, :class:`PostOutcomeUnknown` says the outcome
  cannot be told, rather than claiming either.

Each command adds what a re-run of it shows (spec section 10).
"""

from __future__ import annotations

import json
import time
from typing import Any

from vergil_tooling.lib import github
from vergil_tooling.lib.rulings import RulingError, comments

POST_TIMEOUT = 30
REREADS = 3
REREAD_DELAY = 2.0

# What a failed re-read can raise: a gh failure, GraphQL errors, a page that is
# not shaped as expected, or output that is not JSON. Each is carried in the
# PostOutcomeUnknown message, never dropped.
_READ_ERRORS = (github.GitHubAPIError, RuntimeError, RulingError, ValueError)


class PostNotLanded(RulingError):  # noqa: N818 - the name is the plan's interface
    """The post failed and a re-read shows it did not land; nothing more was posted."""


class PostOutcomeUnknown(RulingError):  # noqa: N818 - the name is the plan's interface
    """The post failed and no re-read succeeded, so whether it landed is unknown."""

    def __init__(self, message: str, errors: list[Exception]) -> None:
        self.errors = errors
        super().__init__(message)


def _landed_id(payload: dict[str, Any]) -> int | None:
    comment_id = payload.get("id")
    if isinstance(comment_id, int) and not isinstance(comment_id, bool) and comment_id > 0:
        return comment_id
    return None


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())


def post_comment_once(repo: str, issue: int, body: str, *, author: str) -> int:
    """Post ``body`` on ``repo#issue`` exactly once and return the new comment's ID.

    ``author`` is the login the post is made as, used to recognise the landed
    comment after a failure. Raises :class:`PostNotLanded` or
    :class:`PostOutcomeUnknown` after a failed post; a missing token
    (:class:`github.MissingGitHubTokenError`) means nothing was sent and is
    raised as it is.
    """
    where = f"{repo}#{issue}"
    before = {comment.id for comment in comments.fetch_comments(repo, issue)}
    failure: Exception
    try:
        payload = github.post_json_once(
            f"repos/{repo}/issues/{issue}/comments", {"body": body}, timeout=POST_TIMEOUT
        )
    except github.MissingGitHubTokenError:
        raise
    except (github.GitHubAPIError, json.JSONDecodeError) as exc:
        failure = exc
    else:
        landed = _landed_id(payload)
        if landed is not None:
            return landed
        failure = RulingError(f"the POST to {where} answered with no comment id")

    errors: list[Exception] = []
    read_ok = False
    for _attempt in range(REREADS):
        time.sleep(REREAD_DELAY)
        try:
            after = comments.fetch_comments(repo, issue)
        except _READ_ERRORS as exc:
            errors.append(exc)
            continue
        read_ok = True
        for comment in after:
            if comment.id not in before and comment.author == author and comment.body == body:
                return comment.id
    if read_ok:
        msg = (
            f"comment did not land on {where}; nothing more was posted"
            f" (the post failed: {_one_line(failure)})"
        )
        raise PostNotLanded(msg)
    reasons = "; ".join(_one_line(exc) for exc in errors)
    msg = (
        f"could not tell whether the comment landed on {where}; nothing more was posted"
        f" (the post failed: {_one_line(failure)}; every re-read failed: {reasons})"
    )
    raise PostOutcomeUnknown(msg, errors)
