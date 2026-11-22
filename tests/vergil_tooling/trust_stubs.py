"""A fake GraphQL backend for the trust layer, for the tests of its callers.

The tests of a module that reads issues through
:func:`vergil_tooling.lib.trust.read_issue` drive the real layer: they patch
``github.graphql`` with :class:`FakeItems`, which answers the layer's item query
for each issue it holds, in the field shapes of a recorded GitHub response
(``{ __typename login }`` actors, paged connections), and patch the loader with
:data:`AUTHORS`.
"""

from __future__ import annotations

from typing import Any

from vergil_tooling.lib import trust
from vergil_tooling.lib.trusted_authors import TrustedAuthors

CREATED = "2026-11-01T09:30:00Z"
EDITED = "2026-11-02T10:00:00Z"

AUTHORS = TrustedAuthors(human=("human",), agents=("vergil-user",))

HUMAN: dict[str, str] = {"__typename": "User", "login": "human"}
AGENT: dict[str, str] = {"__typename": "Bot", "login": "vergil-user"}
OUTSIDER: dict[str, str] = {"__typename": "User", "login": "drive-by"}
# A User account named like the agent App: a role match with the wrong type.
IMPOSTOR: dict[str, str] = {"__typename": "User", "login": "vergil-user"}

# Synthetic spam with a well-formed marker on its own line; neutralised URLs.
SPAM_TEXT = "SPAM-MARKER-TEXT see [here](https://example.invalid/a)"


def _edits(editors: list[Any] | None = None) -> dict[str, Any]:
    return {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"editedAt": EDITED, "editor": e} for e in (editors or [])],
    }


def comment(
    db_id: str | int,
    body: str,
    author: Any = HUMAN,
    *,
    editor: Any = None,
) -> dict[str, Any]:
    """An ``IssueComment`` node; an *editor* makes it edited once, by that editor."""
    return {
        "id": f"IC_{db_id}",
        "fullDatabaseId": str(db_id),
        "author": author,
        "editor": editor,
        "body": body,
        "createdAt": CREATED,
        "lastEditedAt": EDITED if editor is not None else None,
        "userContentEdits": _edits([editor] if editor is not None else []),
    }


def comments(*bodies: str, author: Any = HUMAN) -> list[dict[str, Any]]:
    """One comment node per body, all by *author*, numbered from 1."""
    return [comment(i, body, author) for i, body in enumerate(bodies, start=1)]


def item(
    *,
    body: str = "a trusted body",
    author: Any = HUMAN,
    editor: Any = None,
    nodes: list[dict[str, Any]] | None = None,
    typename: str = "Issue",
) -> dict[str, Any]:
    """The item query's answer for one issue (one page of comments)."""
    return {
        "repository": {
            "issueOrPullRequest": {
                "__typename": typename,
                "author": author,
                "editor": editor,
                "body": body,
                "createdAt": CREATED,
                "lastEditedAt": EDITED if editor is not None else None,
                "userContentEdits": _edits([editor] if editor is not None else []),
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes or [],
                },
            }
        }
    }


class FakeItems:
    """A ``github.graphql`` stand-in answering the layer's item query per issue.

    *items* maps ``(owner/name, number)`` to an :func:`item` answer; an issue it
    does not hold is a trusted item with no comments, or, with ``strict=True``,
    a test failure.
    """

    def __init__(
        self, items: dict[tuple[str, int], dict[str, Any]] | None = None, *, strict: bool = False
    ) -> None:
        self.items = items if items is not None else {}
        self.strict = strict
        self.reads: list[tuple[str, int]] = []

    def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        assert query == trust._ITEM_QUERY, "only the item query is faked"
        key = (f"{variables['owner']}/{variables['name']}", int(variables["number"]))
        self.reads.append(key)
        if key not in self.items:
            assert not self.strict, f"unexpected read of {key}"
            return item()
        return self.items[key]
