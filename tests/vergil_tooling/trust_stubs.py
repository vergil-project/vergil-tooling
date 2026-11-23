"""A fake GraphQL backend for the trust layer, for the tests of its callers.

The tests of a module that reads issues through
:func:`vergil_tooling.lib.trust.read_issue` (or judges a close) drive the real
layer: they patch ``github.graphql`` with :class:`FakeItems`, which answers the
layer's item and events queries for each issue it holds, in the field shapes
of a recorded GitHub response (``{ __typename login }`` actors, paged
connections), and patch the loader with :data:`AUTHORS`.
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


def closed_timeline(actor: Any = HUMAN) -> dict[str, Any]:
    """The events query's answer for an issue closed once, by *actor*."""
    return {
        "repository": {
            "issueOrPullRequest": {
                "__typename": "Issue",
                "timelineItems": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"__typename": "ClosedEvent", "actor": actor, "createdAt": EDITED}],
                },
            }
        }
    }


def commit_author(user: Any = HUMAN, *, at: str = CREATED) -> dict[str, Any]:
    """The commit query's answer: a commit whose author GitHub links to *user*.

    A ``None`` *user* is an author email no GitHub user account carries (a
    bot or App email included).
    """
    return {
        "repository": {
            "object": {"__typename": "Commit", "authoredDate": at, "author": {"user": user}}
        }
    }


def _linked(repo: str, number: int) -> dict[str, Any]:
    return {"number": number, "repository": {"nameWithOwner": repo}}


def closing_pr(
    repo: str,
    number: int,
    *closes: tuple[str, int, str],
    state: str = "MERGED",
    body: str | None = None,
    author: Any = HUMAN,
    editor: Any = None,
    links: tuple[tuple[Any, str, int], ...] = (),
    merged_at: str | None = EDITED,
) -> dict[str, Any]:
    """The answer to :func:`trust.read_pr_closing`'s query for PR *repo*#*number*.

    Each of *closes* is a closing reference GitHub lists, ``(repo, number,
    state)``; by default the body names each with a ``Closes`` line. Each of
    *links* is a hand link, ``(actor, repo, number)``.
    """
    if body is None:
        body = "Summary.\n\n" + "\n".join(f"Closes {r}#{n}" for r, n, _ in closes)
    return {
        "repository": {
            "pullRequest": {
                "number": number,
                "state": state,
                "mergedAt": merged_at if state == "MERGED" else None,
                "repository": {"nameWithOwner": repo},
                "author": author,
                "editor": editor,
                "body": body,
                "createdAt": CREATED,
                "lastEditedAt": EDITED if editor is not None else None,
                "userContentEdits": _edits([editor] if editor is not None else []),
                "closingIssuesReferences": {
                    "totalCount": len(closes),
                    "nodes": [{**_linked(r, n), "state": s} for r, n, s in closes],
                },
                "timelineItems": {
                    "filteredCount": len(links),
                    "nodes": [
                        {
                            "actor": actor,
                            "createdAt": EDITED,
                            "subject": _linked(r, n),
                            "source": {},
                        }
                        for actor, r, n in links
                    ],
                },
            }
        }
    }


class FakeItems:
    """A ``github.graphql`` stand-in answering the layer's item and events queries per issue.

    *items* maps ``(owner/name, number)`` to an :func:`item` answer; an issue it
    does not hold is a trusted item with no comments, or, with ``strict=True``,
    a test failure. *closers* maps the same keys to the actor of the issue's
    one close event (:func:`closed_timeline`); an issue it does not hold was
    closed by the trusted human, or, with ``strict=True``, is a test failure.
    """

    def __init__(
        self,
        items: dict[tuple[str, int], dict[str, Any]] | None = None,
        *,
        strict: bool = False,
        closers: dict[tuple[str, int], Any] | None = None,
    ) -> None:
        self.items = items if items is not None else {}
        self.closers = closers if closers is not None else {}
        self.strict = strict
        self.reads: list[tuple[str, int]] = []
        self.event_reads: list[tuple[str, int]] = []

    def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        assert query in (trust._ITEM_QUERY, trust._EVENTS_QUERY), "only items and events are faked"
        key = (f"{variables['owner']}/{variables['name']}", int(variables["number"]))
        if query == trust._EVENTS_QUERY:
            self.event_reads.append(key)
            assert key in self.closers or not self.strict, f"unexpected events read of {key}"
            return closed_timeline(self.closers.get(key, HUMAN))
        self.reads.append(key)
        if key not in self.items:
            assert not self.strict, f"unexpected read of {key}"
            return item()
        return self.items[key]
