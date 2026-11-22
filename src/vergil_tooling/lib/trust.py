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
is load-bearing they raise :class:`UntrustedContentError` instead.

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

_COMMENT_EDITS_QUERY = f"""
query($id: ID!, $after: String!) {{
  node(id: $id) {{
    ... on IssueComment {{ {_edits_selection(", after: $after")} }}
  }}
}}
"""


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
    ``edit`` or ``event``: who did the untrusted thing (the body's author, a
    comment's author, an editor). *field* names the content concerned
    (``body``, ``comment <id>``). *login* is bare, ``None`` for a deleted
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


def _comment(node: object, call: str) -> _Comment:
    comment = _obj(node, call, "a comment")
    database_id = _text(comment, "fullDatabaseId", call, "a comment")
    what = f"comment {database_id}"
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
        judgement = role_of(identity, org=org, authors=authors)
        if judgement.role is None:
            return None, judgement.cause
        if judgement.role not in roles:
            return None, NOT_ACCEPTED
        return judgement.role, None

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
) -> tuple[list[TrustedComment], list[Skipped]]:
    trusted: list[TrustedComment] = []
    skipped: list[Skipped] = []
    for comment in comments:
        role, _, skip = _judge(
            comment.content,
            repo=repo,
            number=number,
            kind="comment",
            field=f"comment {comment.database_id}",
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


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        msg = f"repo must be owner/name, got {repo!r}"
        raise ValueError(msg)
    return owner, name


def _item(data: Mapping[str, Any], call: str) -> dict[str, Any]:
    repository = _obj(data.get("repository"), call, "the repository")
    return _obj(repository.get("issueOrPullRequest"), call, "the issue or pull request")


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
    owner, name = _split_repo(repo)
    call = f"read_issue {repo}#{number}"
    where = {"owner": owner, "name": name, "number": number}
    item = _item(_graphql(call, _ITEM_QUERY, **where), call)
    typename = item.get("__typename")
    if typename not in ("Issue", "PullRequest"):
        raise TrustReadError(call, "the item is neither an issue nor a pull request")
    kind = "issue" if typename == "Issue" else "pr"
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
        content = comment.content
        what = f"comment {comment.database_id}"
        while content.edits_cursor is not None:
            data = _graphql(
                call, _COMMENT_EDITS_QUERY, id=comment.node_id, after=content.edits_cursor
            )
            node = _obj(data.get("node"), call, what)
            nodes, content.edits_cursor = _connection(
                _get(node, "userContentEdits", call, what), call, f"the edits of {what}"
            )
            content.edits += [_edit(edit, call, what) for edit in nodes]

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
        self._stops: list[tuple[Skipped, str]] = []

    def add(self, item: Skipped, cause: str) -> None:
        """Record a stop on *item*; *cause* is a role_of cause or :data:`READ_FAILURE`."""
        self._stops.append((item, cause))

    def exit_code(self) -> int:
        return 1 if self._stops else 0

    def print_summary(self, stream: TextIO) -> None:
        """Every stop, metadata only; under Actions, counts by cause and read failures."""
        if not self._stops:
            return
        if _under_actions():
            causes = [cause for _, cause in self._stops]
            print(f"stopped {len(self._stops)} item(s) ({_counts(causes)})", file=stream)
            for item, cause in self._stops:
                if cause == READ_FAILURE:
                    print(f"stopped on a read failure: {item.repo}#{item.number}", file=stream)
            return
        for item, cause in self._stops:
            print(f"stopped: {item.describe()} ({cause})", file=stream)
