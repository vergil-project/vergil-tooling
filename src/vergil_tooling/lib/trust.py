"""The trust layer: who may steer automation, and the content it may act on.

Epic vergil-project/.github#1467 (spec §5, §6.1). Every trust decision goes
through this module, and the roles come only from
:func:`~vergil_tooling.lib.trusted_authors.load_trusted_authors`.

**Roles** (spec §5.1). An identity holds one of three roles, or none:

- ``human``: a ``human`` login of the configuration, and a GraphQL ``User``;
- ``agents``: an ``agents`` login, and a ``Bot``;
- ``automation``: the ``automation_app`` of the stanza of the org whose repo
  holds the content, and a ``Bot``. It is not trusted in another org's repos.

A login is compared after a leading ``app/`` and a trailing ``[bot]`` are
stripped, so each form GitHub returns maps to the same role. A login that
matches a role with the wrong account type, or with no type, is never trusted
(cause ``wrong-type``); a deleted account (a ``null`` actor, or ``ghost``) is
untrusted (cause ``deleted``), never a read failure.

**Content** (spec §5.2). A body, or a comment, is trusted when its author and
its editors (every editor, or the last one, by the reader's
:data:`EditorRule`) hold a role the reader accepts. Readers return the trusted
items and a :class:`Skipped` record for each untrusted one; where the content
is load-bearing they raise :class:`UntrustedContentError` instead; for a
report, :func:`read_issue_edits` also lists every untrusted edit of trusted
content, and ``read_events(renames=True)`` every rename. Reviews and
inline review comments follow the same rule (:func:`read_reviews`). A close
or reopen is trusted when its actor is (:func:`read_events`,
:func:`close_judgement`, :func:`require_trusted_close`). A PR found by its
branch is same-repo and has a trusted author (:func:`prs_for_branch`); a fork
PR never matches. A search or list result is trusted when its body is, and
everyone who renamed it holds a role (:func:`search_items`, :func:`list_items`);
a search can judge each result's latest close in the same call, and
:func:`search_window` splits a date window that would meet GitHub's search cap.

**Failing loudly** (spec §3). An API error, a missing field or a connection
that is not read in full raises :class:`TrustReadError`, never "trusted",
"absent" or "nothing found".

**Metadata only** (spec §5.3). Errors and reports carry repo, number, kind,
login, time and field, never the untrusted text. Under GitHub Actions, whose
logs are public, :func:`report_skipped` and :class:`BatchStops` print counts
by cause, listing only read-failure stops by repo and number.

Every read here is GraphQL with ``__typename`` on each actor.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.trusted_authors import (
    GITHUB_ACTIONS_ENV,
    TrustedAuthors,
    load_trusted_authors,
    org_config,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import TextIO


class Role(StrEnum):
    HUMAN = "human"
    AGENTS = "agents"
    AUTOMATION = "automation"


ALL_ROLES: frozenset[Role] = frozenset(Role)

# The causes a Skipped record or a stop carries. The first three come from
# role_of; NOT_ACCEPTED is a trusted role the reader does not accept (its
# ``roles`` parameter); READ_FAILURE is a BatchStops cause only.
UNTRUSTED = "untrusted"
DELETED = "deleted"
WRONG_TYPE = "wrong-type"
NOT_ACCEPTED = "not-accepted"
READ_FAILURE = "read-failure"
# A PR whose head is in a fork: it never matches a branch lookup (spec §5.2).
CROSS_REPOSITORY = "cross-repository"

# The login GitHub shows for an account that no longer exists.
GHOST = "ghost"

_USER = "User"
_BOT = "Bot"
_APP_PREFIX = "app/"
_BOT_SUFFIX = "[bot]"

EditorRule = Literal["every", "last"]

# GraphQL caps a connection page at 100 nodes; every read follows every page.
_PAGE = 100
_ACTOR = "{ __typename login }"
_PAGE_INFO = "pageInfo { hasNextPage endCursor }"


def _edits_selection(after: str = "") -> str:
    return (
        f"userContentEdits(first: {_PAGE}{after}) {{ {_PAGE_INFO}"
        f" nodes {{ editedAt editor {_ACTOR} }} }}"
    )


COMMENT_FIELDS = (
    f"id fullDatabaseId author {_ACTOR} editor {_ACTOR} body createdAt lastEditedAt"
    f" {_edits_selection()}"
)
"""The GraphQL selection on an ``IssueComment`` that :func:`judge_comments` needs."""

_BODY_FIELDS = f"author {_ACTOR} editor {_ACTOR} body createdAt lastEditedAt {_edits_selection()}"
BODY_FIELDS = _BODY_FIELDS
"""The GraphQL selection on an ``Issue`` or ``PullRequest`` that :func:`judge_body` needs."""


def _on_issue_and_pr(selection: str) -> str:
    return f"... on Issue {{ {selection} }} ... on PullRequest {{ {selection} }}"


def _comments_selection(after: str = "") -> str:
    return f"comments(first: {_PAGE}{after}) {{ {_PAGE_INFO} nodes {{ {COMMENT_FIELDS} }} }}"


_ITEM_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    issueOrPullRequest(number: $number) {{
      __typename
      {_on_issue_and_pr(_BODY_FIELDS)}
      {_on_issue_and_pr(_comments_selection())}
    }}
  }}
}}
"""

_BODY_EDITS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $after: String!) {{
  repository(owner: $owner, name: $name) {{
    issueOrPullRequest(number: $number) {{
      {_on_issue_and_pr(_edits_selection(", after: $after"))}
    }}
  }}
}}
"""

_COMMENTS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $after: String!) {{
  repository(owner: $owner, name: $name) {{
    issueOrPullRequest(number: $number) {{
      {_on_issue_and_pr(_comments_selection(", after: $after"))}
    }}
  }}
}}
"""


def _node_query(typename: str, selection: str) -> str:
    """A query for a page of a connection on the node ``$id``, a *typename*.

    ``$after`` is left out for the first page.
    """
    return f"""
query($id: ID!, $after: String) {{
  node(id: $id) {{
    ... on {typename} {{ {selection} }}
  }}
}}
"""


_COMMENT_EDITS_QUERY = _node_query("IssueComment", _edits_selection(", after: $after"))

# --- events, branch lookups and reviews: the selections ------------------------

# The timeline events the layer reads, by GraphQL type, and the kind each
# becomes: a close or reopen changes an item's state; the rest are the traces
# the release guard warns on.
_EVENT_KINDS = {
    "ClosedEvent": "closed",
    "ReopenedEvent": "reopened",
    "LabeledEvent": "labeled",
    "UnlabeledEvent": "unlabeled",
    "MentionedEvent": "mentioned",
    "CrossReferencedEvent": "cross-referenced",
    "SubscribedEvent": "subscribed",
}
_EVENT_TYPES = ", ".join(
    (
        "CLOSED_EVENT",
        "REOPENED_EVENT",
        "LABELED_EVENT",
        "UNLABELED_EVENT",
        "MENTIONED_EVENT",
        "CROSS_REFERENCED_EVENT",
        "SUBSCRIBED_EVENT",
    )
)
# With ``renames=True``, read_events also reads every title rename (the
# outsider report lists each untrusted one); no other caller sees the kind.
_RENAME_EVENT_KINDS = {**_EVENT_KINDS, "RenamedTitleEvent": "renamed"}


def _timeline_selection(kinds: Mapping[str, str], types: str) -> str:
    fragments = " ".join(f"... on {typename} {{ actor {_ACTOR} createdAt }}" for typename in kinds)
    return (
        f"timelineItems(first: {_PAGE}, after: $after, itemTypes: [{types}])"
        f" {{ {_PAGE_INFO} nodes {{ __typename {fragments} }} }}"
    )


def _events_query(kinds: Mapping[str, str], types: str) -> str:
    return f"""
query($owner: String!, $name: String!, $number: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    issueOrPullRequest(number: $number) {{
      __typename
      {_on_issue_and_pr(_timeline_selection(kinds, types))}
    }}
  }}
}}
"""


_EVENTS_QUERY = _events_query(_EVENT_KINDS, _EVENT_TYPES)
_EVENTS_WITH_RENAMES_QUERY = _events_query(
    _RENAME_EVENT_KINDS, f"{_EVENT_TYPES}, RENAMED_TITLE_EVENT"
)

# A commit's author as GitHub links it to an account: ``user`` is ``null`` when
# no account carries the commit's author email.
_COMMIT_QUERY = f"""
query($owner: String!, $name: String!, $oid: GitObjectID!) {{
  repository(owner: $owner, name: $name) {{
    object(oid: $oid) {{
      __typename
      ... on Commit {{ authoredDate author {{ user {_ACTOR} }} }}
    }}
  }}
}}
"""

# What :func:`is_same_repo` reads on a PR: the same names in GraphQL and in
# ``gh pr list --json``.
SAME_REPO_FIELDS = "isCrossRepository headRepositoryOwner { login } headRepository { name }"
"""The GraphQL selection on a ``PullRequest`` that :func:`is_same_repo` needs."""

_PR_STATES = {"open": "states: [OPEN], ", "merged": "states: [MERGED], ", "all": ""}


def _branch_query(state: str) -> str:
    return f"""
query($owner: String!, $name: String!, $branch: String!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(headRefName: $branch, {_PR_STATES[state]}first: {_PAGE}, after: $after) {{
      {_PAGE_INFO}
      nodes {{
        number url title headRefName headRefOid state createdAt author {_ACTOR}
        {SAME_REPO_FIELDS}
      }}
    }}
  }}
}}
"""


_BRANCH_QUERIES = {state: _branch_query(state) for state in _PR_STATES}


# GitHub refuses a query whose ``first:`` arguments, multiplied down its
# nesting, could return more than 500,000 nodes. Each query here nests at most
# two connections (100 x 100), so a review's inline comments, each with its
# own edit history, are read per review, not inside the reviews query.
_REVIEWS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      reviews(first: {_PAGE}, after: $after) {{ {_PAGE_INFO} nodes {{ {COMMENT_FIELDS} }} }}
    }}
  }}
}}
"""

_REVIEW_EDITS_QUERY = _node_query("PullRequestReview", _edits_selection(", after: $after"))
_REVIEW_COMMENTS_QUERY = _node_query(
    "PullRequestReview",
    f"comments(first: {_PAGE}, after: $after) {{ {_PAGE_INFO} nodes {{ {COMMENT_FIELDS} }} }}",
)
_REVIEW_COMMENT_EDITS_QUERY = _node_query(
    "PullRequestReviewComment", _edits_selection(", after: $after")
)


@dataclass(frozen=True)
class Identity:
    """An actor as GraphQL returns it; a ``None`` login is a deleted account."""

    login: str | None
    typename: str | None


@dataclass(frozen=True)
class Judgement:
    """An identity's role, or ``None`` with the cause it is untrusted."""

    role: Role | None
    cause: str | None


@dataclass(frozen=True)
class Skipped:
    """Metadata of one untrusted item: never its text.

    *kind* is ``issue``, ``pr``, ``comment``, ``review``, ``review-comment``,
    ``edit``, ``event`` or ``commit``: who did the untrusted thing (the body's
    author, a comment's author, an editor, a commit's author). A ``commit``
    belongs to no issue or PR: its *number* is 0 and is not shown. *field*
    names the content concerned (``body``, ``comment <id>``, ``review <id>``,
    ``review comment <id>``, a branch-lookup PR's ``author`` or fork ``head``,
    or what a commit carries). *login* is bare, ``None`` for a deleted
    account. *cause* is one of the module's causes.
    """

    repo: str
    number: int
    kind: str
    login: str | None
    at: str
    field: str
    cause: str

    def describe(self) -> str:
        """The one-line metadata description every report and error uses."""
        if self.kind == "commit":
            who = self.login if self.login is not None else "an author with no GitHub account"
            return f"{self.repo} commit {who} at {self.at} on {self.field}"
        who = self.login if self.login is not None else "a deleted account"
        return f"{self.repo}#{self.number} {self.kind} {who} at {self.at} on {self.field}"


class UntrustedContentError(Exception):
    """A stop: content the action relies on is untrusted (spec §5.3)."""

    def __init__(self, item: Skipped, reason: str) -> None:
        self.item = item
        self.reason = reason
        super().__init__(
            f"stopped before any write: {reason}: {item.describe()} ({item.cause})."
            " The human decides next."
        )


class TrustReadError(Exception):
    """A read failure: who wrote the content could not be read in full."""

    def __init__(self, call: str, detail: str) -> None:
        self.call = call
        self.detail = detail
        super().__init__(f"cannot read who wrote the content ({call}): {detail}")


@dataclass(frozen=True)
class TrustedComment:
    """A trusted comment: its ``fullDatabaseId`` (the REST id), body, author role and time."""

    id: str
    body: str
    role: Role
    at: str


@dataclass(frozen=True)
class IssueContent:
    """What :func:`read_issue` trusts on an issue or PR.

    *body* and *body_role* are ``None`` when the body is untrusted;
    *author_role* is ``None`` when the author is (an outsider-opened item).
    """

    body: str | None
    body_role: Role | None
    author_role: Role | None
    comments: tuple[TrustedComment, ...]
    skipped: tuple[Skipped, ...]


@dataclass(frozen=True)
class IssueEvent:
    """One timeline event: its kind (``closed``, ``labeled``…), its actor and its time."""

    kind: str
    actor: Identity
    at: str


@dataclass(frozen=True)
class PRRef:
    """A same-repo PR with a trusted author, found by its branch.

    *head* is the branch name and *head_oid* the head commit; *state* is
    GraphQL's ``OPEN``, ``CLOSED`` or ``MERGED``.
    """

    number: int
    url: str
    title: str
    head: str
    head_oid: str
    author_role: Role
    state: str


@dataclass(frozen=True)
class PRReviewContent:
    """What :func:`read_reviews` trusts on a PR: its reviews and inline review comments."""

    reviews: tuple[TrustedComment, ...]
    review_comments: tuple[TrustedComment, ...]
    skipped: tuple[Skipped, ...]


def normalise_login(login: str) -> str:
    """*login* with a leading ``app/`` and a trailing ``[bot]`` stripped."""
    return login.removeprefix(_APP_PREFIX).removesuffix(_BOT_SUFFIX)


def _bare(identity: Identity) -> str | None:
    return None if identity.login is None else normalise_login(identity.login)


def role_of(identity: Identity, *, org: str, authors: TrustedAuthors) -> Judgement:
    """The role *identity* holds for content in a repo of *org*, or why it holds none."""
    login = _bare(identity)
    if login is None or login == GHOST:
        return Judgement(None, DELETED)
    automation = org_config(authors, org)
    if login in authors.human:
        role, wanted = Role.HUMAN, _USER
    elif login in authors.agents:
        role, wanted = Role.AGENTS, _BOT
    elif automation is not None and login == automation.automation_app:
        role, wanted = Role.AUTOMATION, _BOT
    else:
        return Judgement(None, UNTRUSTED)
    if identity.typename != wanted:
        return Judgement(None, WRONG_TYPE)
    return Judgement(role, None)


def _accepted(
    identity: Identity, *, org: str, roles: frozenset[Role], authors: TrustedAuthors
) -> Judgement:
    """:func:`role_of`, a role outside *roles* judged :data:`NOT_ACCEPTED`."""
    judgement = role_of(identity, org=org, authors=authors)
    if judgement.role is not None and judgement.role not in roles:
        return Judgement(None, NOT_ACCEPTED)
    return judgement


def judge_identity(
    identity: Identity,
    *,
    org: str,
    roles: frozenset[Role] = ALL_ROLES,
    authors: TrustedAuthors,
) -> Judgement:
    """The role *identity* holds among *roles* for content in a repo of *org*, or why none.

    :func:`role_of` for a reader that accepts only *roles*: a trusted role
    outside them is judged :data:`NOT_ACCEPTED`, so ``role`` is ``None``.
    """
    return _accepted(identity, org=org, roles=roles, authors=authors)


def identity(actor: object, *, call: str, what: str) -> Identity:
    """The :class:`Identity` of a GraphQL actor read with ``{ __typename login }``.

    A ``null`` actor is a deleted account. An actor without a usable
    ``login`` or ``__typename`` raises :class:`TrustReadError`: its type is
    never guessed.
    """
    if actor is None:
        return Identity(None, None)
    return _actor({"actor": actor}, "actor", call, what)


# --- reading -----------------------------------------------------------------


def _graphql(call: str, query: str, **variables: object) -> dict[str, Any]:
    """:func:`github.graphql`, its failures raised as :class:`TrustReadError`.

    Only gh's last stderr line is kept: a failed response's body may echo the
    content it was reading.
    """
    try:
        return github.graphql(query, **variables)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        lines = [line for line in str(exc.stderr or "").splitlines() if line.strip()]
        last = lines[-1].strip()[:200] if lines else "no error output"
        raise TrustReadError(call, f"the GraphQL call failed: {last}") from None
    except RuntimeError, ValueError:
        raise TrustReadError(call, "the GraphQL response carried errors or no data") from None


def _obj(value: object, call: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TrustReadError(call, f"{what} is missing or not an object")
    return cast("dict[str, Any]", value)


def _get(node: Mapping[str, Any], key: str, call: str, what: str) -> Any:
    if key not in node:
        raise TrustReadError(call, f"{what} has no {key}")
    return node[key]


def _text(node: Mapping[str, Any], key: str, call: str, what: str, *, empty: bool = False) -> str:
    value = _get(node, key, call, what)
    if not isinstance(value, str) or (not value and not empty):
        raise TrustReadError(call, f"{what} has no usable {key}")
    return value


def _optional_text(node: Mapping[str, Any], key: str, call: str, what: str) -> str | None:
    value = _get(node, key, call, what)
    if value is not None and not isinstance(value, str):
        raise TrustReadError(call, f"{what} has no usable {key}")
    return value


def _actor(node: Mapping[str, Any], key: str, call: str, what: str) -> Identity:
    value = _get(node, key, call, what)
    if value is None:
        return Identity(None, None)
    actor = _obj(value, call, f"{what}'s {key}")
    return Identity(
        _text(actor, "login", call, f"{what}'s {key}"),
        _text(actor, "__typename", call, f"{what}'s {key}"),
    )


def _connection(value: object, call: str, what: str) -> tuple[list[Any], str | None]:
    """A connection's nodes, and the cursor of its next page (``None`` on the last)."""
    conn = _obj(value, call, what)
    nodes = _get(conn, "nodes", call, what)
    info = _obj(_get(conn, "pageInfo", call, what), call, f"{what}'s pageInfo")
    has_next = _get(info, "hasNextPage", call, f"{what}'s pageInfo")
    if not isinstance(nodes, list) or not isinstance(has_next, bool):
        raise TrustReadError(call, f"{what} is not shaped as a connection")
    if not has_next:
        return nodes, None
    cursor = _get(info, "endCursor", call, f"{what}'s pageInfo")
    if not isinstance(cursor, str) or not cursor:
        raise TrustReadError(call, f"{what} has a next page but no cursor")
    return nodes, cursor


def _edit(node: object, call: str, what: str) -> tuple[Identity, str]:
    edit = _obj(node, call, f"an edit of {what}")
    return (
        _actor(edit, "editor", call, f"an edit of {what}"),
        _text(edit, "editedAt", call, f"an edit of {what}"),
    )


@dataclass
class _Content:
    """A body or a comment, parsed: who wrote and edited it, and when."""

    author: Identity
    body: str
    created_at: str
    # The last editor and when, when the content was edited at all.
    last_edit: tuple[Identity, str] | None
    edits: list[tuple[Identity, str]]
    edits_cursor: str | None


def _content(node: Mapping[str, Any], call: str, what: str) -> _Content:
    last_edited_at = _optional_text(node, "lastEditedAt", call, what)
    editor = _actor(node, "editor", call, what)
    nodes, cursor = _connection(
        _get(node, "userContentEdits", call, what), call, f"the edits of {what}"
    )
    return _Content(
        author=_actor(node, "author", call, what),
        body=_text(node, "body", call, what, empty=True),
        created_at=_text(node, "createdAt", call, what),
        last_edit=None if last_edited_at is None else (editor, last_edited_at),
        edits=[_edit(edit, call, what) for edit in nodes],
        edits_cursor=cursor,
    )


@dataclass
class _Comment:
    node_id: str
    database_id: str
    content: _Content


def _comment(node: object, call: str, label: str = "comment") -> _Comment:
    """A comment, a review or an inline review comment: *label* names which."""
    comment = _obj(node, call, f"a {label}")
    database_id = _text(comment, "fullDatabaseId", call, f"a {label}")
    what = f"{label} {database_id}"
    return _Comment(
        node_id=_text(comment, "id", call, what),
        database_id=database_id,
        content=_content(comment, call, what),
    )


def _judge(
    content: _Content,
    *,
    repo: str,
    number: int,
    kind: str,
    field: str,
    roles: frozenset[Role],
    editors: EditorRule,
    authors: TrustedAuthors,
) -> tuple[Role | None, Role | None, Skipped | None]:
    """``(content role, author role, skipped)``: the role is the author's when all pass."""
    org = repo.partition("/")[0]

    def cause_of(identity: Identity) -> tuple[Role | None, str | None]:
        judgement = _accepted(identity, org=org, roles=roles, authors=authors)
        return judgement.role, judgement.cause

    author_role, cause = cause_of(content.author)
    if cause is not None:
        skipped = Skipped(
            repo, number, kind, _bare(content.author), content.created_at, field, cause
        )
        return None, None, skipped
    checked = [] if content.last_edit is None else [content.last_edit]
    if editors == "every":
        checked += content.edits
    for identity, at in checked:
        _, cause = cause_of(identity)
        if cause is not None:
            return (
                None,
                author_role,
                Skipped(repo, number, "edit", _bare(identity), at, field, cause),
            )
    return author_role, author_role, None


def _judge_parsed(
    comments: Sequence[_Comment],
    *,
    repo: str,
    number: int,
    roles: frozenset[Role],
    editors: EditorRule,
    authors: TrustedAuthors,
    kind: str = "comment",
    label: str = "comment",
) -> tuple[list[TrustedComment], list[Skipped]]:
    trusted: list[TrustedComment] = []
    skipped: list[Skipped] = []
    for comment in comments:
        role, _, skip = _judge(
            comment.content,
            repo=repo,
            number=number,
            kind=kind,
            field=f"{label} {comment.database_id}",
            roles=roles,
            editors=editors,
            authors=authors,
        )
        if skip is not None:
            skipped.append(skip)
        else:
            content = comment.content
            trusted.append(
                TrustedComment(
                    comment.database_id, content.body, cast("Role", role), content.created_at
                )
            )
    return trusted, skipped


def judge_comments(
    comments: Sequence[Mapping[str, object]],
    *,
    repo: str,
    number: int,
    roles: frozenset[Role] = ALL_ROLES,
    editors: EditorRule = "every",
    authors: TrustedAuthors,
) -> tuple[list[TrustedComment], list[Skipped]]:
    """Judge comment nodes a caller fetched with :data:`COMMENT_FIELDS`.

    Raises :class:`TrustReadError` when a node lacks a field
    :data:`COMMENT_FIELDS` names (``__typename`` on every actor included) or a
    comment's edit history has a next page: the input must be complete. The
    caller pages the comments connection itself. Callers that can use
    :func:`read_issue` do.
    """
    call = f"judge_comments {repo}#{number}"
    parsed = [_comment(node, call) for node in comments]
    for comment in parsed:
        if comment.content.edits_cursor is not None:
            raise TrustReadError(
                call, f"the edits of comment {comment.database_id} are not read in full"
            )
    return _judge_parsed(
        parsed, repo=repo, number=number, roles=roles, editors=editors, authors=authors
    )


def judge_body(
    node: Mapping[str, object],
    *,
    repo: str,
    number: int,
    kind: Literal["issue", "pr"],
    roles: frozenset[Role] = ALL_ROLES,
    editors: EditorRule = "every",
    authors: TrustedAuthors,
) -> tuple[Role | None, Skipped | None]:
    """Judge the body of an issue or PR node a caller fetched with :data:`BODY_FIELDS`.

    Returns the body's role (its author's, when the author and its editors by
    *editors* hold a role in *roles*) and ``None``; or ``None`` and the
    :class:`Skipped` record (field ``body``) of the first identity that does
    not. Raises :class:`TrustReadError` when a field :data:`BODY_FIELDS`
    names is missing or the body's edit history has a next page: the input
    must be complete. Callers that can use :func:`read_issue` do.
    """
    call = f"judge_body {repo}#{number}"
    body = _content(node, call, f"the {kind} body")
    if body.edits_cursor is not None:
        raise TrustReadError(call, "the edits of the body are not read in full")
    role, _, skipped = _judge(
        body,
        repo=repo,
        number=number,
        kind=kind,
        field="body",
        roles=roles,
        editors=editors,
        authors=authors,
    )
    return role, skipped


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        msg = f"repo must be owner/name, got {repo!r}"
        raise ValueError(msg)
    return owner, name


def _item(data: Mapping[str, Any], call: str) -> dict[str, Any]:
    repository = _obj(data.get("repository"), call, "the repository")
    return _obj(repository.get("issueOrPullRequest"), call, "the issue or pull request")


def _kind_of(item: Mapping[str, Any], call: str) -> str:
    """``issue`` or ``pr``, by the item's ``__typename``."""
    typename = item.get("__typename")
    if typename not in ("Issue", "PullRequest"):
        raise TrustReadError(call, "the item is neither an issue nor a pull request")
    return "issue" if typename == "Issue" else "pr"


def _node_page(
    call: str, query: str, node_id: str, cursor: str | None, what: str
) -> dict[str, Any]:
    """The node *node_id* from a page of *query*: at *cursor*, or the first page."""
    variables: dict[str, object] = {"id": node_id}
    if cursor is not None:
        variables["after"] = cursor
    return _obj(_graphql(call, query, **variables).get("node"), call, what)


def _read_all_edits(comment: _Comment, call: str, query: str, label: str = "comment") -> None:
    """Follow every further page of *comment*'s edit history, through *query*."""
    content = comment.content
    what = f"{label} {comment.database_id}"
    while content.edits_cursor is not None:
        node = _node_page(call, query, comment.node_id, content.edits_cursor, what)
        nodes, content.edits_cursor = _connection(
            _get(node, "userContentEdits", call, what), call, f"the edits of {what}"
        )
        content.edits += [_edit(edit, call, what) for edit in nodes]


def read_issue(
    repo: str,
    number: int,
    *,
    roles: frozenset[Role] = ALL_ROLES,
    editors: EditorRule = "every",
    require_body: bool = False,
    authors: TrustedAuthors | None = None,
) -> IssueContent:
    """Read *repo*#*number*, an issue or a PR, and judge its body and comments.

    One GraphQL read through ``repository.issueOrPullRequest``, then every
    further page of the body's edit history, of the comments, and of each
    comment's edit history. *roles* are the roles the reader accepts and
    *editors* which editors must hold one. ``require_body=True`` raises
    :class:`UntrustedContentError` when the body is untrusted; otherwise it is
    reported in ``skipped``. *authors* defaults to the loaded configuration.
    """
    if authors is None:
        authors = load_trusted_authors()
    kind, body, comments = _read_item(repo, number)
    return _issue_content(
        repo,
        number,
        kind,
        body,
        comments,
        roles=roles,
        editors=editors,
        require_body=require_body,
        authors=authors,
    )


def read_issue_edits(
    repo: str,
    number: int,
    *,
    roles: frozenset[Role] = ALL_ROLES,
    authors: TrustedAuthors | None = None,
) -> tuple[IssueContent, tuple[Skipped, ...]]:
    """:func:`read_issue`, and every untrusted edit of the item's trusted content.

    The one read :func:`read_issue` makes (author and every editor). Where
    that judges a body or comment by its first untrusted editor only, this
    also lists each untrusted edit (kind ``edit``) of a body or comment whose
    author holds a role in *roles*, newest and oldest alike, for a report of
    possible tampering. Edits of content an untrusted account wrote are not
    listed: that content is untrusted already.
    """
    if authors is None:
        authors = load_trusted_authors()
    kind, body, comments = _read_item(repo, number)
    content = _issue_content(
        repo, number, kind, body, comments, roles=roles, editors="every", authors=authors
    )
    org = _split_repo(repo)[0]
    edits: dict[Skipped, None] = {}
    pieces = [(body, "body")] + [(c.content, f"comment {c.database_id}") for c in comments]
    for piece, field in pieces:
        if _accepted(piece.author, org=org, roles=roles, authors=authors).role is None:
            continue
        history = ([] if piece.last_edit is None else [piece.last_edit]) + piece.edits
        for identity, at in history:
            cause = _accepted(identity, org=org, roles=roles, authors=authors).cause
            if cause is not None:
                edits[Skipped(repo, number, "edit", _bare(identity), at, field, cause)] = None
    return content, tuple(edits)


def _read_item(repo: str, number: int) -> tuple[str, _Content, list[_Comment]]:
    """An issue's or PR's kind, body and comments, every page of each read."""
    owner, name = _split_repo(repo)
    call = f"read_issue {repo}#{number}"
    where = {"owner": owner, "name": name, "number": number}
    item = _item(_graphql(call, _ITEM_QUERY, **where), call)
    kind = _kind_of(item, call)
    body = _content(item, call, f"the {kind} body")
    while body.edits_cursor is not None:
        page = _item(_graphql(call, _BODY_EDITS_QUERY, **where, after=body.edits_cursor), call)
        nodes, body.edits_cursor = _connection(
            _get(page, "userContentEdits", call, "the body"), call, "the edits of the body"
        )
        body.edits += [_edit(edit, call, "the body") for edit in nodes]

    nodes, cursor = _connection(_get(item, "comments", call, f"the {kind}"), call, "the comments")
    comments = [_comment(node, call) for node in nodes]
    while cursor is not None:
        page = _item(_graphql(call, _COMMENTS_QUERY, **where, after=cursor), call)
        nodes, cursor = _connection(
            _get(page, "comments", call, f"the {kind}"), call, "the comments"
        )
        comments += [_comment(node, call) for node in nodes]
    for comment in comments:
        _read_all_edits(comment, call, _COMMENT_EDITS_QUERY)
    return kind, body, comments


def _issue_content(
    repo: str,
    number: int,
    kind: str,
    body: _Content,
    comments: Sequence[_Comment],
    *,
    roles: frozenset[Role],
    editors: EditorRule,
    authors: TrustedAuthors,
    require_body: bool = False,
) -> IssueContent:
    body_role, author_role, body_skip = _judge(
        body,
        repo=repo,
        number=number,
        kind=kind,
        field="body",
        roles=roles,
        editors=editors,
        authors=authors,
    )
    if body_skip is not None and require_body:
        raise UntrustedContentError(body_skip, f"the {kind} body is untrusted")
    trusted, skipped = _judge_parsed(
        comments, repo=repo, number=number, roles=roles, editors=editors, authors=authors
    )
    return IssueContent(
        body=None if body_skip is not None else body.body,
        body_role=body_role,
        author_role=author_role,
        comments=tuple(trusted),
        skipped=tuple(([] if body_skip is None else [body_skip]) + skipped),
    )


def outsider_opened(content: IssueContent) -> Skipped | None:
    """The author's :class:`Skipped` record when an outsider opened the item, else ``None``.

    An item whose author holds no role the read accepted is outsider-opened
    (spec §5.3): a run that merely encounters one reports this record, and a
    tool pointed at one raises :class:`UntrustedContentError` with it.
    """
    if content.author_role is not None:
        return None
    for item in content.skipped:
        if item.field == "body" and item.kind in ("issue", "pr"):
            return item
    # read_issue always records an untrusted author's body; never guess one.
    raise TrustReadError("outsider_opened", "an untrusted author has no skip record")


# --- events -------------------------------------------------------------------


def _event(node: object, call: str, kinds: Mapping[str, str] = _EVENT_KINDS) -> IssueEvent:
    event = _obj(node, call, "a timeline event")
    typename = _get(event, "__typename", call, "a timeline event")
    kind = kinds.get(typename) if isinstance(typename, str) else None
    if kind is None:
        raise TrustReadError(call, "a timeline event is of a type that was not asked for")
    what = f"a {kind} event"
    return IssueEvent(
        kind, _actor(event, "actor", call, what), _text(event, "createdAt", call, what)
    )


def read_events(repo: str, number: int, *, renames: bool = False) -> tuple[IssueEvent, ...]:
    """The close, reopen and trace events of *repo*#*number*, oldest first.

    A GraphQL read of the issue's or PR's ``timelineItems``, type-filtered to
    the kinds :class:`IssueEvent` names, followed page by page until
    ``hasNextPage`` is false. ``renames=True`` also reads every title rename,
    as kind ``renamed``. A failed call, a malformed page or a next page
    with no cursor raises :class:`TrustReadError`.
    """
    owner, name = _split_repo(repo)
    call = f"read_events {repo}#{number}"
    query, kinds = (
        (_EVENTS_WITH_RENAMES_QUERY, _RENAME_EVENT_KINDS)
        if renames
        else (_EVENTS_QUERY, _EVENT_KINDS)
    )
    events: list[IssueEvent] = []
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": name, "number": number}
        if cursor is not None:
            variables["after"] = cursor
        item = _item(_graphql(call, query, **variables), call)
        _kind_of(item, call)
        nodes, cursor = _connection(
            _get(item, "timelineItems", call, "the item"), call, "the timeline"
        )
        events += [_event(node, call, kinds) for node in nodes]
        if cursor is None:
            return tuple(events)


def close_judgement(
    repo: str,
    number: int,
    *,
    roles: frozenset[Role] = ALL_ROLES,
    authors: TrustedAuthors | None = None,
) -> Judgement:
    """The role of whoever closed *repo*#*number* last, or why it holds none.

    A close by an untrusted actor is not a close for any decision the
    automation makes (spec §5.2): a caller that acts on the close raises
    :class:`UntrustedContentError` when the judgement's ``role`` is ``None``.
    An item with no close event raises :class:`TrustReadError`: the close
    the caller asks about cannot be read.
    """
    if authors is None:
        authors = load_trusted_authors()
    close = _latest_close(repo, number, "close_judgement")
    return _accepted(close.actor, org=_split_repo(repo)[0], roles=roles, authors=authors)


def require_trusted_close(
    repo: str,
    number: int,
    *,
    roles: frozenset[Role] = ALL_ROLES,
    authors: TrustedAuthors | None = None,
) -> Role:
    """The role of whoever closed *repo*#*number* last; an untrusted close is a stop.

    For a caller that acts on the close: where :func:`close_judgement` gives no
    role, this raises :class:`UntrustedContentError` on the close event (kind
    ``event``, field ``close``), metadata only. An item with no close event
    raises :class:`TrustReadError`.
    """
    if authors is None:
        authors = load_trusted_authors()
    close = _latest_close(repo, number, "require_trusted_close")
    judgement = _accepted(close.actor, org=_split_repo(repo)[0], roles=roles, authors=authors)
    if judgement.role is None:
        item = Skipped(
            repo, number, "event", _bare(close.actor), close.at, "close", str(judgement.cause)
        )
        raise UntrustedContentError(item, "the close is untrusted")
    return judgement.role


def _latest_close(repo: str, number: int, caller: str) -> IssueEvent:
    closes = [event for event in read_events(repo, number) if event.kind == "closed"]
    if not closes:
        raise TrustReadError(f"{caller} {repo}#{number}", "the item has no close event")
    return closes[-1]


# --- commits --------------------------------------------------------------------


def require_trusted_commit(
    repo: str,
    oid: str,
    *,
    field: str,
    roles: frozenset[Role] = ALL_ROLES,
    authors: TrustedAuthors | None = None,
) -> Role:
    """The role of the author of commit *oid* in *repo*; an untrusted author is a stop.

    A GraphQL read of the commit's author as GitHub links it to an account
    (``author.user``, with ``__typename``). An author no account carries is
    untrusted, as a deleted account is. Where the author holds no role in
    *roles*, this raises :class:`UntrustedContentError` (kind ``commit``,
    *field* naming what the commit carries), metadata only. A failed call, a
    missing commit or a malformed answer raises :class:`TrustReadError`.
    """
    if authors is None:
        authors = load_trusted_authors()
    owner, name = _split_repo(repo)
    call = f"require_trusted_commit {repo}@{oid[:12]}"
    data = _graphql(call, _COMMIT_QUERY, owner=owner, name=name, oid=oid)
    repository = _obj(_get(data, "repository", call, "the answer"), call, "the repository")
    commit = _obj(_get(repository, "object", call, "the repository"), call, "the commit")
    if _get(commit, "__typename", call, "the commit") != "Commit":
        raise TrustReadError(call, "the object is not a commit")
    at = _text(commit, "authoredDate", call, "the commit")
    author = _obj(_get(commit, "author", call, "the commit"), call, "the commit's author")
    identity = _actor(author, "user", call, "the commit's author")
    judgement = _accepted(identity, org=owner, roles=roles, authors=authors)
    if judgement.role is None:
        item = Skipped(repo, 0, "commit", _bare(identity), at, field, str(judgement.cause))
        raise UntrustedContentError(item, "the commit's author is untrusted")
    return judgement.role


# --- branch lookups -------------------------------------------------------------


def is_same_repo(repo: str, item: Mapping[str, object]) -> bool:
    """True when the PR *item*'s head is in *repo* itself, not a fork.

    *item* carries :data:`SAME_REPO_FIELDS` (a GraphQL node, or a
    ``gh pr list --json`` entry: the names are the same). ``isCrossRepository``
    decides it; a same-repo answer is cross-checked against the head
    repository's owner and name, and a disagreement raises
    :class:`TrustReadError` rather than guess.
    """
    number = item.get("number")
    what = f"PR {number}" if isinstance(number, int) and not isinstance(number, bool) else "a PR"
    call = f"is_same_repo {repo}"
    cross = item.get("isCrossRepository")
    if not isinstance(cross, bool):
        raise TrustReadError(call, f"{what} has no isCrossRepository flag")
    if cross:
        return False
    owner = item.get("headRepositoryOwner")
    head_repo = item.get("headRepository")
    login = owner.get("login") if isinstance(owner, dict) else None
    name = head_repo.get("name") if isinstance(head_repo, dict) else None
    if not isinstance(login, str) or not isinstance(name, str):
        raise TrustReadError(call, f"{what} has no head repository")
    if f"{login}/{name}".lower() != repo.lower():
        raise TrustReadError(
            call, f"{what} is not cross-repository, but its head is {login}/{name}"
        )
    return True


def _branch_pr(
    node: object,
    *,
    repo: str,
    branch: str,
    call: str,
    roles: frozenset[Role],
    authors: TrustedAuthors,
) -> PRRef | Skipped:
    pr = _obj(node, call, "a pull request")
    number = _get(pr, "number", call, "a pull request")
    if not isinstance(number, int) or isinstance(number, bool):
        raise TrustReadError(call, "a pull request has no usable number")
    what = f"PR {number}"
    head = _text(pr, "headRefName", call, what)
    if head != branch:
        raise TrustReadError(call, f"{what} is not on the branch asked for")
    author = _actor(pr, "author", call, what)
    at = _text(pr, "createdAt", call, what)
    try:
        same = is_same_repo(repo, pr)
    except TrustReadError as exc:
        raise TrustReadError(call, exc.detail) from None
    if not same:
        return Skipped(repo, number, "pr", _bare(author), at, "head", CROSS_REPOSITORY)
    judgement = _accepted(author, org=_split_repo(repo)[0], roles=roles, authors=authors)
    if judgement.role is None:
        return Skipped(
            repo, number, "pr", _bare(author), at, "author", cast("str", judgement.cause)
        )
    return PRRef(
        number=number,
        url=_text(pr, "url", call, what),
        title=_text(pr, "title", call, what, empty=True),
        head=head,
        head_oid=_text(pr, "headRefOid", call, what),
        author_role=judgement.role,
        state=_text(pr, "state", call, what),
    )


def prs_for_branch(
    repo: str,
    branch: str,
    *,
    state: Literal["open", "merged", "all"],
    roles: frozenset[Role] = ALL_ROLES,
    authors: TrustedAuthors | None = None,
) -> tuple[list[PRRef], list[Skipped]]:
    """The same-repo PRs in *repo* from *branch*, with a trusted author (spec §5.2).

    A GraphQL read of ``pullRequests(headRefName:)`` in *state*, every page.
    A fork PR on the branch is skipped (cause :data:`CROSS_REPOSITORY`), and
    so is a PR whose author holds no role in *roles*; both are returned as
    :class:`Skipped` for the caller to report. A fork never matches.
    """
    if authors is None:
        authors = load_trusted_authors()
    owner, name = _split_repo(repo)
    call = f"prs_for_branch {repo} {branch}"
    found: list[PRRef] = []
    skipped: list[Skipped] = []
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": name, "branch": branch}
        if cursor is not None:
            variables["after"] = cursor
        data = _graphql(call, _BRANCH_QUERIES[state], **variables)
        repository = _obj(data.get("repository"), call, "the repository")
        nodes, cursor = _connection(
            _get(repository, "pullRequests", call, "the repository"), call, "the pull requests"
        )
        for node in nodes:
            result = _branch_pr(
                node, repo=repo, branch=branch, call=call, roles=roles, authors=authors
            )
            if isinstance(result, Skipped):
                skipped.append(result)
            else:
                found.append(result)
        if cursor is None:
            return found, skipped


# --- reviews --------------------------------------------------------------------


def _review_comments(review: _Comment, call: str) -> list[_Comment]:
    """Every inline comment of *review*, page by page through its node."""
    what = f"review {review.database_id}"
    comments: list[_Comment] = []
    cursor: str | None = None
    while True:
        page = _node_page(call, _REVIEW_COMMENTS_QUERY, review.node_id, cursor, what)
        nodes, cursor = _connection(
            _get(page, "comments", call, what), call, f"the comments of {what}"
        )
        comments += [_comment(comment, call, "review comment") for comment in nodes]
        if cursor is None:
            return comments


def read_reviews(
    repo: str,
    pr: int,
    *,
    roles: frozenset[Role] = ALL_ROLES,
    editors: EditorRule = "every",
    authors: TrustedAuthors | None = None,
) -> PRReviewContent:
    """Read *repo*#*pr*'s reviews and inline review comments, and judge each.

    A paged GraphQL read of the PR's reviews, each review's inline comments,
    and every edit history among them. A review or comment is trusted when
    its author and its editors (by *editors*) hold a role in *roles*; each
    untrusted one is returned as :class:`Skipped` (kind ``review`` or
    ``review-comment``, or ``edit`` for an untrusted editor).
    """
    if authors is None:
        authors = load_trusted_authors()
    owner, name = _split_repo(repo)
    call = f"read_reviews {repo}#{pr}"
    reviews: list[_Comment] = []
    inline: list[_Comment] = []
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": name, "number": pr}
        if cursor is not None:
            variables["after"] = cursor
        data = _graphql(call, _REVIEWS_QUERY, **variables)
        repository = _obj(data.get("repository"), call, "the repository")
        pull = _obj(repository.get("pullRequest"), call, "the pull request")
        nodes, cursor = _connection(
            _get(pull, "reviews", call, "the pull request"), call, "the reviews"
        )
        reviews += [_comment(node, call, "review") for node in nodes]
        if cursor is None:
            break
    for review in reviews:
        inline += _review_comments(review, call)
        _read_all_edits(review, call, _REVIEW_EDITS_QUERY, "review")
    for comment in inline:
        _read_all_edits(comment, call, _REVIEW_COMMENT_EDITS_QUERY, "review comment")

    trusted_reviews, skipped = _judge_parsed(
        reviews,
        repo=repo,
        number=pr,
        roles=roles,
        editors=editors,
        authors=authors,
        kind="review",
        label="review",
    )
    trusted_inline, skipped_inline = _judge_parsed(
        inline,
        repo=repo,
        number=pr,
        roles=roles,
        editors=editors,
        authors=authors,
        kind="review-comment",
        label="review comment",
    )
    return PRReviewContent(
        reviews=tuple(trusted_reviews),
        review_comments=tuple(trusted_inline),
        skipped=tuple(skipped + skipped_inline),
    )


# --- searches and lists -----------------------------------------------------------

ItemKind = Literal["issue", "pr"]
ListState = Literal["open", "closed", "merged", "all"]

_KINDS: tuple[ItemKind, ...] = ("issue", "pr")
_TYPENAMES: dict[str, str] = {"issue": "Issue", "pr": "PullRequest"}

# GitHub's search returns at most this many results for one query; a query
# that matches more is refused rather than judged on a part of its results.
SEARCH_RESULT_CAP = 1000


def _renames_selection(after: str = "") -> str:
    return (
        f"timelineItems(first: {_PAGE}{after}, itemTypes: [RENAMED_TITLE_EVENT])"
        f" {{ {_PAGE_INFO} nodes {{ __typename ... on RenamedTitleEvent"
        f" {{ actor {_ACTOR} createdAt }} }} }}"
    )


# The latest close of a result, under its own alias: ``last: 1`` of its
# close events, so a search judges every result's close in the same call.
_CLOSE_SELECTION = (
    "closes: timelineItems(last: 1, itemTypes: [CLOSED_EVENT])"
    f" {{ nodes {{ __typename ... on ClosedEvent {{ actor {_ACTOR} createdAt }} }} }}"
)


def _hit_selection(kind: ItemKind, *, close: bool = False) -> str:
    """What a search or list reads of each result: one GraphQL type per query.

    *close* adds the result's latest close event (:data:`_CLOSE_SELECTION`).
    """
    extra = f" {_CLOSE_SELECTION}" if close else ""
    return (
        f"__typename ... on {_TYPENAMES[kind]} {{"
        f" number title url state repository {{ nameWithOwner }}"
        f" labels(first: {_PAGE}) {{ {_PAGE_INFO} nodes {{ name }} }}"
        f" {_BODY_FIELDS} {_renames_selection()}{extra} }}"
    )


def _search_query(kind: ItemKind, *, close: bool = False) -> str:
    return f"""
query($q: String!, $after: String) {{
  search(query: $q, type: ISSUE, first: {_PAGE}, after: $after) {{
    issueCount {_PAGE_INFO}
    nodes {{ {_hit_selection(kind, close=close)} }}
  }}
}}
"""


_SEARCH_QUERIES = {kind: _search_query(kind) for kind in _KINDS}
# The same searches, each result carrying its latest close (``judge_close``).
_CLOSE_SEARCH_QUERIES = {kind: _search_query(kind, close=True) for kind in _KINDS}

# The date qualifiers a windowed search splits on (:func:`search_window`).
WindowField = Literal["created", "updated", "closed"]

_LIST_STATES: dict[tuple[str, str], str] = {
    ("issue", "open"): "states: [OPEN], ",
    ("issue", "closed"): "states: [CLOSED], ",
    ("issue", "all"): "",
    ("pr", "open"): "states: [OPEN], ",
    # A merged PR is closed too, as in GitHub search (is:closed) and gh pr list.
    ("pr", "closed"): "states: [CLOSED, MERGED], ",
    ("pr", "merged"): "states: [MERGED], ",
    ("pr", "all"): "",
}


def _list_query(kind: str, state: str) -> str:
    connection = "issues" if kind == "issue" else "pullRequests"
    order = "orderBy: {field: CREATED_AT, direction: DESC}, "
    return f"""
query($owner: String!, $name: String!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    {connection}({_LIST_STATES[kind, state]}{order}first: {_PAGE}, after: $after) {{
      {_PAGE_INFO}
      nodes {{ {_hit_selection(cast("ItemKind", kind))} }}
    }}
  }}
}}
"""


_LIST_QUERIES = {key: _list_query(*key) for key in _LIST_STATES}

_RENAMES_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $after: String!) {{
  repository(owner: $owner, name: $name) {{
    issueOrPullRequest(number: $number) {{
      {_on_issue_and_pr(_renames_selection(", after: $after"))}
    }}
  }}
}}
"""


@dataclass(frozen=True)
class TrustedHit:
    """A search or list result whose body and title the layer trusts.

    *kind* is ``issue`` or ``pr``; *state* is GraphQL's ``OPEN``, ``CLOSED``
    or ``MERGED``. *body_role* is the body's author's role; every editor of
    the body and everyone who renamed the item (by the reader's
    :data:`EditorRule`) holds a role the reader accepts. With
    ``judge_close``, *close_role* is the role of whoever closed the item
    last and *closed_at* the time of that close; both are ``None`` otherwise.
    """

    repo: str
    number: int
    kind: str
    title: str
    url: str
    state: str
    labels: tuple[str, ...]
    body: str
    body_role: Role
    close_role: Role | None = None
    closed_at: str | None = None


@dataclass
class _Hit:
    repo: str
    number: int
    title: str
    url: str
    state: str
    labels: tuple[str, ...]
    body: _Content
    renames: list[tuple[Identity, str]]
    renames_cursor: str | None
    # The latest close's actor and time, when the read asked for it.
    close: tuple[Identity, str] | None = None


def _rename(node: object, call: str, what: str) -> tuple[Identity, str]:
    event = _obj(node, call, f"a rename of {what}")
    if _get(event, "__typename", call, f"a rename of {what}") != "RenamedTitleEvent":
        raise TrustReadError(call, f"a rename of {what} is of a type that was not asked for")
    return (
        _actor(event, "actor", call, f"a rename of {what}"),
        _text(event, "createdAt", call, f"a rename of {what}"),
    )


def _latest_close_of(item: Mapping[str, Any], call: str, what: str) -> tuple[Identity, str]:
    """The actor and time of the latest close a result carries (:data:`_CLOSE_SELECTION`).

    A result with no close event raises :class:`TrustReadError`: whoever
    closed it cannot be read.
    """
    closes = _obj(_get(item, "closes", call, what), call, f"the closes of {what}")
    nodes = _get(closes, "nodes", call, f"the closes of {what}")
    if not isinstance(nodes, list) or len(nodes) > 1:
        raise TrustReadError(call, f"the closes of {what} are not the latest close alone")
    if not nodes:
        raise TrustReadError(call, f"{what} has no close event")
    event = _obj(nodes[0], call, f"the close of {what}")
    if _get(event, "__typename", call, f"the close of {what}") != "ClosedEvent":
        raise TrustReadError(call, f"the close of {what} is of a type that was not asked for")
    return (
        _actor(event, "actor", call, f"the close of {what}"),
        _text(event, "createdAt", call, f"the close of {what}"),
    )


def _hit(node: object, call: str, kind: ItemKind, *, close: bool = False) -> _Hit:
    item = _obj(node, call, "a result")
    if _get(item, "__typename", call, "a result") != _TYPENAMES[kind]:
        raise TrustReadError(call, f"a result is not of the kind asked for ({kind})")
    number = _get(item, "number", call, "a result")
    if not isinstance(number, int) or isinstance(number, bool):
        raise TrustReadError(call, "a result has no usable number")
    repository = _obj(_get(item, "repository", call, f"#{number}"), call, f"#{number}'s repo")
    repo = _text(repository, "nameWithOwner", call, f"#{number}'s repository")
    what = f"{repo}#{number}"
    label_nodes, label_cursor = _connection(
        _get(item, "labels", call, what), call, f"the labels of {what}"
    )
    if label_cursor is not None:
        raise TrustReadError(call, f"the labels of {what} are not read in full")
    labels = tuple(
        _text(_obj(label, call, f"a label of {what}"), "name", call, f"a label of {what}")
        for label in label_nodes
    )
    rename_nodes, renames_cursor = _connection(
        _get(item, "timelineItems", call, what), call, f"the renames of {what}"
    )
    return _Hit(
        repo=repo,
        number=number,
        title=_text(item, "title", call, what, empty=True),
        url=_text(item, "url", call, what),
        state=_text(item, "state", call, what),
        labels=labels,
        body=_content(item, call, f"the body of {what}"),
        renames=[_rename(rename, call, what) for rename in rename_nodes],
        renames_cursor=renames_cursor,
        close=_latest_close_of(item, call, what) if close else None,
    )


def _read_hit_history(hit: _Hit, call: str) -> None:
    """Follow every further page of *hit*'s body edits and title renames."""
    owner, name = _split_repo(hit.repo)
    where = {"owner": owner, "name": name, "number": hit.number}
    what = f"{hit.repo}#{hit.number}"
    body = hit.body
    while body.edits_cursor is not None:
        page = _item(_graphql(call, _BODY_EDITS_QUERY, **where, after=body.edits_cursor), call)
        nodes, body.edits_cursor = _connection(
            _get(page, "userContentEdits", call, what), call, f"the edits of {what}"
        )
        body.edits += [_edit(edit, call, what) for edit in nodes]
    while hit.renames_cursor is not None:
        page = _item(_graphql(call, _RENAMES_QUERY, **where, after=hit.renames_cursor), call)
        nodes, hit.renames_cursor = _connection(
            _get(page, "timelineItems", call, what), call, f"the renames of {what}"
        )
        hit.renames += [_rename(rename, call, what) for rename in nodes]


def _judge_hit(
    hit: _Hit,
    *,
    kind: ItemKind,
    roles: frozenset[Role],
    editors: EditorRule,
    authors: TrustedAuthors,
) -> TrustedHit | Skipped:
    """The hit, trusted, or the first untrusted thing about its body or title."""
    role, _, skipped = _judge(
        hit.body,
        repo=hit.repo,
        number=hit.number,
        kind=kind,
        field="body",
        roles=roles,
        editors=editors,
        authors=authors,
    )
    if skipped is not None:
        return skipped
    org = hit.repo.partition("/")[0]
    renames = hit.renames if editors == "every" else hit.renames[-1:]
    for identity, at in renames:
        judgement = _accepted(identity, org=org, roles=roles, authors=authors)
        if judgement.cause is not None:
            return Skipped(
                hit.repo, hit.number, "event", _bare(identity), at, "title", judgement.cause
            )
    close_role: Role | None = None
    if hit.close is not None:
        actor, at = hit.close
        judgement = _accepted(actor, org=org, roles=roles, authors=authors)
        if judgement.cause is not None:
            return Skipped(
                hit.repo, hit.number, "event", _bare(actor), at, "close", judgement.cause
            )
        close_role = judgement.role
    return TrustedHit(
        repo=hit.repo,
        number=hit.number,
        kind=kind,
        title=hit.title,
        url=hit.url,
        state=hit.state,
        labels=hit.labels,
        body=hit.body.body,
        body_role=cast("Role", role),
        close_role=close_role,
        closed_at=None if hit.close is None else hit.close[1],
    )


def _judge_hits(
    hits: Sequence[_Hit],
    *,
    call: str,
    kind: ItemKind,
    roles: frozenset[Role],
    editors: EditorRule,
    authors: TrustedAuthors,
) -> tuple[list[TrustedHit], list[Skipped]]:
    trusted: list[TrustedHit] = []
    skipped: list[Skipped] = []
    for hit in hits:
        _read_hit_history(hit, call)
        result = _judge_hit(hit, kind=kind, roles=roles, editors=editors, authors=authors)
        if isinstance(result, Skipped):
            skipped.append(result)
        else:
            trusted.append(result)
    return trusted, skipped


def _search_hits(
    full: str, *, kind: ItemKind, call: str, judge_close: bool
) -> tuple[list[_Hit], int | None]:
    """Every result of the search *full*, or none and the count when it is over the cap.

    The cap is checked on the first page, so an over-cap search costs one
    call. A count that rises over the cap on a later page raises
    :class:`TrustReadError`: the pages already read may not be all of it.
    """
    query = (_CLOSE_SEARCH_QUERIES if judge_close else _SEARCH_QUERIES)[kind]
    hits: list[_Hit] = []
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {"q": full}
        if cursor is not None:
            variables["after"] = cursor
        data = _graphql(call, query, **variables)
        search = _obj(data.get("search"), call, "the search")
        count = _get(search, "issueCount", call, "the search")
        if not isinstance(count, int) or isinstance(count, bool):
            raise TrustReadError(call, "the search has no usable issueCount")
        if count > SEARCH_RESULT_CAP:
            if cursor is None:
                return [], count
            raise TrustReadError(
                call,
                f"the search grew to {count} items while it was read, more than the"
                f" {SEARCH_RESULT_CAP} GitHub's search returns",
            )
        nodes, cursor = _connection(search, call, "the search results")
        hits += [_hit(node, call, kind, close=judge_close) for node in nodes]
        if cursor is None:
            return hits, None


def search_items(
    query: str,
    *,
    kind: ItemKind,
    judge_close: bool = False,
    roles: frozenset[Role] = ALL_ROLES,
    editors: EditorRule = "every",
    authors: TrustedAuthors | None = None,
) -> tuple[list[TrustedHit], list[Skipped]]:
    """The issues (or PRs) GitHub's search finds for *query*, trusted ones only.

    *query* is GitHub search syntax; ``is:issue`` or ``is:pr`` is added by
    *kind*. Every page is read, and every result is judged as a body is
    (spec §5.2: its author and its editors), and its title too: everyone who
    renamed it must hold a role in *roles*. ``judge_close=True``, for a
    search of closed items, reads each result's latest close in the same
    call and judges its actor too (an untrusted close is skipped with kind
    ``event`` and field ``close``); a result with no close event raises
    :class:`TrustReadError`. Each other result is returned as
    :class:`Skipped` for the caller to report, never dropped. A query that
    matches more than :data:`SEARCH_RESULT_CAP` items raises
    :class:`TrustReadError`: GitHub would return only part of them.
    """
    if authors is None:
        authors = load_trusted_authors()
    call = f"search_items {kind}"
    hits, over = _search_hits(f"{query} is:{kind}", kind=kind, call=call, judge_close=judge_close)
    if over is not None:
        raise TrustReadError(
            call,
            f"the search matches {over} items, more than the {SEARCH_RESULT_CAP}"
            " GitHub's search returns; narrow the query",
        )
    return _judge_hits(hits, call=call, kind=kind, roles=roles, editors=editors, authors=authors)


def _date_range(lo: date, hi: date | None) -> str:
    """A search date qualifier's value: from *lo*, to *hi* (open-ended when ``None``)."""
    if hi is None:
        return f">={lo.isoformat()}"
    if lo == hi:
        return lo.isoformat()
    return f"{lo.isoformat()}..{hi.isoformat()}"


def search_window(
    query: str,
    *,
    kind: ItemKind,
    field: WindowField,
    since: str,
    until: str | None = None,
    judge_close: bool = False,
    roles: frozenset[Role] = ALL_ROLES,
    editors: EditorRule = "every",
    authors: TrustedAuthors | None = None,
) -> tuple[list[TrustedHit], list[Skipped]]:
    """:func:`search_items` over a date window, split so that no part meets the cap.

    Searches *query* for the items whose *field* (``created``, ``updated``
    or ``closed``) is on or after *since* (``YYYY-MM-DD``) and, when *until*
    is given, on or before it; open-ended otherwise. A part of the window
    that matches more than :data:`SEARCH_RESULT_CAP` items is halved by
    date and each half searched on its own, so a busy window is read in
    full. The GitHub calls are the result pages, plus one first page for
    each halving and any further page of one result's edits or renames. A
    single day over the cap raises :class:`TrustReadError`: GitHub would
    return only part of it.

    Results are judged as :func:`search_items` judges them, *judge_close*
    included, and each item appears once.
    """
    if authors is None:
        authors = load_trusted_authors()
    call = f"search_window {kind} {field}"
    start = date.fromisoformat(since)
    stop = None if until is None else date.fromisoformat(until)
    if stop is not None and stop < start:
        msg = f"until ({until}) is before since ({since})"
        raise ValueError(msg)
    hits: list[_Hit] = []
    seen: set[tuple[str, int]] = set()
    # The parts still to read, oldest first: (from, to), to None for open-ended.
    parts: list[tuple[date, date | None]] = [(start, stop)]
    while parts:
        lo, hi = parts.pop(0)
        full = f"{query} is:{kind} {field}:{_date_range(lo, hi)}"
        found, over = _search_hits(full, kind=kind, call=call, judge_close=judge_close)
        if over is not None:
            last = hi if hi is not None else datetime.now(UTC).date()
            if lo >= last:
                raise TrustReadError(
                    call,
                    f"{field}:{_date_range(lo, hi)} matches {over} items, more than the"
                    f" {SEARCH_RESULT_CAP} GitHub's search returns",
                )
            mid = lo + (last - lo) // 2
            parts[:0] = [(lo, mid), (mid + timedelta(days=1), hi)]
            continue
        for hit in found:
            if (hit.repo, hit.number) not in seen:
                seen.add((hit.repo, hit.number))
                hits.append(hit)
    return _judge_hits(hits, call=call, kind=kind, roles=roles, editors=editors, authors=authors)


def list_items(
    repo: str,
    *,
    kind: ItemKind,
    state: ListState,
    roles: frozenset[Role] = ALL_ROLES,
    editors: EditorRule = "every",
    authors: TrustedAuthors | None = None,
) -> tuple[list[TrustedHit], list[Skipped]]:
    """*repo*'s issues (or PRs) in *state*, newest first, trusted ones only.

    A GraphQL read of the repository's ``issues`` or ``pullRequests``, every
    page, judged as :func:`search_items` judges its results. ``merged`` is a
    PR state only. Unlike a search, it reads the repository itself, so an
    item opened a moment ago is not missing from it.
    """
    if (kind, state) not in _LIST_STATES:
        msg = f"an issue has no {state!r} state"
        raise ValueError(msg)
    if authors is None:
        authors = load_trusted_authors()
    owner, name = _split_repo(repo)
    call = f"list_items {repo} {kind}"
    connection = "issues" if kind == "issue" else "pullRequests"
    hits: list[_Hit] = []
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": name}
        if cursor is not None:
            variables["after"] = cursor
        data = _graphql(call, _LIST_QUERIES[kind, state], **variables)
        repository = _obj(data.get("repository"), call, "the repository")
        nodes, cursor = _connection(
            _get(repository, connection, call, "the repository"), call, f"the {connection}"
        )
        hits += [_hit(node, call, kind) for node in nodes]
        if cursor is None:
            break
    return _judge_hits(hits, call=call, kind=kind, roles=roles, editors=editors, authors=authors)


# --- reporting ----------------------------------------------------------------


def _under_actions() -> bool:
    return os.environ.get(GITHUB_ACTIONS_ENV) == "true"


def _counts(causes: Sequence[str]) -> str:
    return ", ".join(f"{cause}: {n}" for cause, n in sorted(Counter(causes).items()))


def report_skipped(skipped: Sequence[Skipped], *, stream: TextIO) -> None:
    """Report *skipped* on *stream*, metadata only; under Actions, counts by cause."""
    if not skipped:
        return
    if _under_actions():
        causes = [item.cause for item in skipped]
        print(f"skipped {len(skipped)} untrusted item(s) ({_counts(causes)})", file=stream)
        return
    for item in skipped:
        print(f"skipped untrusted content: {item.describe()} ({item.cause})", file=stream)


class BatchStops:
    """The items a batch run stopped: it carries on, and exits non-zero at the end."""

    def __init__(self) -> None:
        self._stops: list[tuple[Skipped, str, str | None]] = []

    def add(self, item: Skipped, cause: str, *, detail: str | None = None) -> None:
        """Record a stop on *item*; *cause* is a role_of cause or :data:`READ_FAILURE`.

        *detail* is a read failure's :class:`TrustReadError` message, metadata
        only; it is printed outside GitHub Actions.
        """
        self._stops.append((item, cause, detail))

    @property
    def stopped(self) -> tuple[Skipped, ...]:
        """The stopped items, in the order they were added."""
        return tuple(item for item, _, _ in self._stops)

    @property
    def named(self) -> tuple[Skipped, ...]:
        """The stopped items a report may name here, in order.

        Every one outside GitHub Actions; under Actions, whose logs are public,
        only the read failures, since a stop caused by untrusted content or
        an untrusted identity is counted only (spec §5.3).
        """
        under_actions = _under_actions()
        return tuple(
            item for item, cause, _ in self._stops if not under_actions or cause == READ_FAILURE
        )

    def exit_code(self) -> int:
        return 1 if self._stops else 0

    def print_summary(self, stream: TextIO) -> None:
        """Every stop, metadata only; under Actions, counts by cause and read failures.

        A read failure has no author or time to show: it is named by repo,
        number and field, with its detail when there is one.
        """
        if not self._stops:
            return
        if _under_actions():
            causes = [cause for _, cause, _ in self._stops]
            print(f"stopped {len(self._stops)} item(s) ({_counts(causes)})", file=stream)
            for item, cause, _ in self._stops:
                if cause == READ_FAILURE:
                    print(f"stopped on a read failure: {item.repo}#{item.number}", file=stream)
            return
        for item, cause, detail in self._stops:
            if cause != READ_FAILURE:
                print(f"stopped: {item.describe()} ({cause})", file=stream)
                continue
            line = f"stopped on a read failure: {item.repo}#{item.number} on {item.field}"
            print(line if detail is None else f"{line}: {detail}", file=stream)
