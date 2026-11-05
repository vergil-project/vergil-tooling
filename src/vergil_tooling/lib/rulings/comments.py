"""Reading an issue's comments: every page, oldest first, with 64-bit IDs.

Each comment's ID is GraphQL's ``fullDatabaseId`` (a ``BigInt``, returned as a
string), which equals the REST ``id``; never ``databaseId``, a 32-bit ``Int``
that current comment IDs overflow. A leading ``app/`` is stripped from every
author, and a comment whose author is gone (a deleted account) is kept with
author ``""``, so it is never trusted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import github
from vergil_tooling.lib.rulings import RulingError

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

_APP_PREFIX = "app/"
_ISSUE_REF_RE = re.compile(r"^([\w.-]+/[\w.-]+)#([1-9][0-9]*)$")
_ID_RE = re.compile(r"^[1-9][0-9]*$")

_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { fullDatabaseId author { login } body lastEditedAt }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class IssueComment:
    """One issue comment as the turn checks see it."""

    id: int
    author: str
    body: str
    edited: bool


def parse_issue_ref(ref: str) -> tuple[str, int]:
    """Split ``owner/repo#N`` into ``("owner/repo", N)``; refuse any other form."""
    match = _ISSUE_REF_RE.fullmatch(ref)
    if match is None:
        msg = f"--issue must be owner/repo#N, got {ref!r}"
        raise RulingError(msg)
    return match.group(1), int(match.group(2))


def _comment_from_node(node: Any, where: str) -> IssueComment:
    if not isinstance(node, dict):
        msg = f"{where}: a comment node is not an object: {node!r}"
        raise RulingError(msg)
    raw_id = node.get("fullDatabaseId")
    if not isinstance(raw_id, str) or _ID_RE.fullmatch(raw_id) is None:
        msg = f"{where}: a comment has no usable fullDatabaseId: {raw_id!r}"
        raise RulingError(msg)
    comment_id = int(raw_id)
    body = node.get("body")
    if not isinstance(body, str):
        msg = f"{where}: comment {comment_id} has no body string"
        raise RulingError(msg)
    author_node = node.get("author")
    login = ""
    if author_node is not None:
        raw_login = author_node.get("login") if isinstance(author_node, dict) else None
        if not isinstance(raw_login, str) or not raw_login:
            msg = f"{where}: comment {comment_id} has an author with no login"
            raise RulingError(msg)
        login = raw_login.removeprefix(_APP_PREFIX)
    return IssueComment(
        id=comment_id,
        author=login,
        body=body,
        edited=node.get("lastEditedAt") is not None,
    )


def fetch_comments(repo: str, issue: int) -> list[IssueComment]:
    """Every comment on ``repo#issue``, oldest first, reading every page.

    Raises :class:`RulingError` when the issue cannot be found or a page is not
    shaped as expected, so the turn checks never see a truncated stream.
    """
    owner, _, name = repo.partition("/")
    where = f"{repo}#{issue}"
    found: list[IssueComment] = []
    after: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": name, "number": issue}
        if after is not None:
            variables["after"] = after
        data: Any = github.graphql(_COMMENTS_QUERY, **variables)
        issue_node = ((data or {}).get("repository") or {}).get("issue")
        if not isinstance(issue_node, dict):
            msg = f"{where}: issue not found"
            raise RulingError(msg)
        page = issue_node.get("comments")
        if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
            msg = f"{where}: the comments page is not shaped as expected"
            raise RulingError(msg)
        found.extend(_comment_from_node(node, where) for node in page["nodes"])
        info = page.get("pageInfo")
        if not isinstance(info, dict):
            msg = f"{where}: the comments page has no pageInfo"
            raise RulingError(msg)
        if not info.get("hasNextPage"):
            return found
        cursor = info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            msg = f"{where}: the comments page says there is more but gives no cursor"
            raise RulingError(msg)
        after = cursor


def trusted(comments: list[IssueComment], authors: TrustedAuthors) -> list[IssueComment]:
    """The comments by a trusted login (human or agent), in their original order."""
    logins = set(authors.human) | set(authors.agents)
    return [c for c in comments if c.author and c.author in logins]
