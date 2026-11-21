"""The tracking issue's lock and identity rule (spec §4.2, epic vergil-project/.github#1440).

The tracking issue holds the release state, so ``vrg-release`` trusts it only
while every identity that can change that state is a ``human`` login of
``vrg-trusted-authors``: the issue's author, its body's last editor and every
editor in its edit history, and every actor that closed or reopened it. Any
other such identity is a suspected security breach (:class:`TrackingIssueBreach`)
that stops the run before it writes anything. Other traces (a comment, a
cross-reference, a mention, a subscription or a label by another identity)
cannot change the state, so they are only warnings.

The issue is locked, so only repository collaborators can comment;
:func:`ensure_locked` locks one that is not, an inherited issue included.

Every read here fails loudly: a GraphQL failure, a response that is not what
was asked for, or a connection that cannot be read in full raises, never
passes as "no other identity" (spec §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.trusted_authors import bare_login

if TYPE_CHECKING:
    from collections.abc import Sequence

# GraphQL caps a connection page at 100 nodes; the read follows every page.
_PAGE = 100

# The login GitHub shows for an account that no longer exists. A deleted
# account's author, editor or actor reads as ``null``; it is never a human login.
GHOST = "ghost"

# What each timeline item records, by its GraphQL type: the field naming the
# identity, and the action it took. A close or reopen changes the issue's
# state; every other item is a trace.
_STATE_EVENTS = {
    "ClosedEvent": ("actor", "closed the issue"),
    "ReopenedEvent": ("actor", "reopened the issue"),
}
_TRACE_EVENTS = {
    "IssueComment": ("author", "commented"),
    "CrossReferencedEvent": ("actor", "cross-referenced the issue"),
    "MentionedEvent": ("actor", "mentioned the issue"),
    "SubscribedEvent": ("actor", "subscribed to the issue"),
    "LabeledEvent": ("actor", "added a label"),
    "UnlabeledEvent": ("actor", "removed a label"),
}
_TIMELINE_TYPES = (
    "CLOSED_EVENT",
    "REOPENED_EVENT",
    "ISSUE_COMMENT",
    "CROSS_REFERENCED_EVENT",
    "MENTIONED_EVENT",
    "SUBSCRIBED_EVENT",
    "LABELED_EVENT",
    "UNLABELED_EVENT",
)
_TIMELINE_FRAGMENTS = "\n".join(
    f"          ... on {typename} {{ {field} {{ login }} }}"
    for typename, (field, _) in {**_STATE_EVENTS, **_TRACE_EVENTS}.items()
)

_IDENTITIES_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $edits: String, $timeline: String) {{
  repository(owner: $owner, name: $name) {{
    issue(number: $number) {{
      author {{ login }}
      editor {{ login }}
      userContentEdits(first: {_PAGE}, after: $edits) {{
        totalCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ editor {{ login }} }}
      }}
      timelineItems(first: {_PAGE}, after: $timeline, itemTypes: [{", ".join(_TIMELINE_TYPES)}]) {{
        totalCount
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          __typename
{_TIMELINE_FRAGMENTS}
        }}
      }}
    }}
  }}
}}
"""

_LOCKED_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { locked }
  }
}
"""


class TrackingIssueBreach(ReleaseError):  # noqa: N818 - the name the plan gives it
    """A non-human identity authored, edited, closed or reopened the tracking issue.

    Its message starts ``suspected security breach on tracking issue #<N>:``
    once the issue's number is known, and tells the human to inspect, clean
    up and resume. It is a :class:`ReleaseError` so it reports as one, but a
    caller that comments on a failed stage must let it pass uncommented: the
    run stops before any write.
    """

    def __init__(self, login: str, action: str, *, number: int | None = None) -> None:
        self.login = login
        self.action = action
        self.number = number
        where = f"tracking issue #{number}" if number is not None else "the tracking issue"
        super().__init__(
            phase="state",
            command="check_tracking_issue",
            message=(
                f"suspected security breach on {where}: {login} {action}, and only a "
                "human login of vrg-trusted-authors may author, edit, close or reopen it. "
                "vrg-release stopped before writing anything. Inspect the issue's edit "
                "history and events, clean it up, then resume with vrg-release --resume."
            ),
        )


@dataclass(frozen=True)
class IssueIdentities:
    """Every identity on a tracking issue, as read by :func:`read_identities`.

    *editors* holds the body's last editor and every editor in its edit
    history, once each, in that order. *state_actors* and *traces* are
    ``(login, action)`` pairs in timeline order. Logins are bare (a leading
    ``app/`` stripped), and a deleted account is :data:`GHOST`.
    """

    author: str
    editors: tuple[str, ...]
    state_actors: tuple[tuple[str, str], ...]
    traces: tuple[tuple[str, str], ...]


def _read_error(repo: str, number: int, detail: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(
        1,
        ("gh", "api", "graphql"),
        stderr=f"cannot read the identities on tracking issue {repo}#{number}: {detail}",
    )


def _obj(value: object, repo: str, number: int, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _read_error(repo, number, f"{what} is {value!r}")
    return cast("dict[str, Any]", value)


def _login(value: object, repo: str, number: int, what: str) -> str:
    """The bare login of an actor object; ``null`` (a deleted account) is :data:`GHOST`."""
    if value is None:
        return GHOST
    login = _obj(value, repo, number, what).get("login")
    if not isinstance(login, str) or not login:
        raise _read_error(repo, number, f"{what} has no login: {value!r}")
    return bare_login(login)


class _Connection:
    """One paginated connection of the identities read, gathered page by page."""

    def __init__(self, field: str) -> None:
        self.field = field
        self.nodes: list[object] = []
        self.cursor: str | None = None
        self.done = False
        self.total: int | None = None

    def add_page(self, issue: dict[str, Any], repo: str, number: int) -> None:
        conn = _obj(issue.get(self.field), repo, number, self.field)
        total = conn.get("totalCount")
        nodes = conn.get("nodes")
        info = _obj(conn.get("pageInfo"), repo, number, f"{self.field}.pageInfo")
        has_next = info.get("hasNextPage")
        end = info.get("endCursor")
        if not isinstance(total, int) or isinstance(total, bool) or not isinstance(nodes, list):
            raise _read_error(repo, number, f"{self.field} has no totalCount or nodes: {conn!r}")
        if not isinstance(has_next, bool):
            raise _read_error(repo, number, f"{self.field}.pageInfo has no hasNextPage: {info!r}")
        if self.done:
            # A finished connection is re-read at its last cursor while the
            # other one pages on; it must stay finished.
            if nodes or has_next:
                raise _read_error(repo, number, f"{self.field} grew while it was read")
            return
        self.nodes.extend(cast("list[object]", nodes))
        self.total = total
        if has_next:
            if not isinstance(end, str) or not end:
                raise _read_error(
                    repo, number, f"{self.field} has a next page but no cursor: {info!r}"
                )
            self.cursor = end
        else:
            self.done = True
            if isinstance(end, str) and end:
                self.cursor = end

    def complete(self, repo: str, number: int) -> list[object]:
        if len(self.nodes) != self.total:
            raise _read_error(
                repo,
                number,
                f"GitHub lists {self.total} {self.field} but returned {len(self.nodes)}",
            )
        return self.nodes


def _timeline_pair(node: object, repo: str, number: int) -> tuple[str, str, bool]:
    """``(login, action, changes_state)`` for one timeline item."""
    item = _obj(node, repo, number, "a timeline item")
    typename = item.get("__typename")
    if typename in _STATE_EVENTS:
        field, action = _STATE_EVENTS[typename]
        is_state = True
    elif typename in _TRACE_EVENTS:
        field, action = _TRACE_EVENTS[typename]
        is_state = False
    else:
        raise _read_error(repo, number, f"a timeline item of an unasked type: {node!r}")
    return _login(item.get(field), repo, number, f"a {typename}'s {field}"), action, is_state


def read_identities(repo: str, number: int) -> IssueIdentities:
    """Every identity on *repo*#*number*: its author, editors, state actors and traces.

    One GraphQL query, followed page by page until both its edit history and
    its timeline are read in full. A failed call raises from
    :func:`github.graphql`; a response that is not what was asked for, or a
    connection whose items do not add up to its ``totalCount``, raises
    :class:`github.GitHubAPIError` naming the issue.
    """
    owner, name = repo.split("/", 1)
    edits, timeline = _Connection("userContentEdits"), _Connection("timelineItems")
    author = last_editor = None
    first = True
    while first or not (edits.done and timeline.done):
        variables: dict[str, object] = {"owner": owner, "name": name, "number": number}
        if edits.cursor is not None:
            variables["edits"] = edits.cursor
        if timeline.cursor is not None:
            variables["timeline"] = timeline.cursor
        data = github.graphql(_IDENTITIES_QUERY, **variables)
        repository = _obj(data.get("repository"), repo, number, "repository")
        issue = _obj(repository.get("issue"), repo, number, "issue")
        if first:
            author = _login(issue.get("author"), repo, number, "the author")
            raw_editor = issue.get("editor")
            last_editor = (
                None if raw_editor is None else _login(raw_editor, repo, number, "the editor")
            )
            first = False
        edits.add_page(issue, repo, number)
        timeline.add_page(issue, repo, number)

    editors: list[str] = [] if last_editor is None else [last_editor]
    for node in edits.complete(repo, number):
        login = _login(
            _obj(node, repo, number, "an edit").get("editor"), repo, number, "an edit's editor"
        )
        if login not in editors:
            editors.append(login)
    state_actors: list[tuple[str, str]] = []
    traces: list[tuple[str, str]] = []
    for node in timeline.complete(repo, number):
        login, action, is_state = _timeline_pair(node, repo, number)
        (state_actors if is_state else traces).append((login, action))
    return IssueIdentities(
        author=cast("str", author),
        editors=tuple(editors),
        state_actors=tuple(state_actors),
        traces=tuple(traces),
    )


def check_tracking_issue(identities: IssueIdentities, humans: Sequence[str]) -> list[str]:
    """Apply the identity rule; return one warning per foreign trace.

    Raises :class:`TrackingIssueBreach` for the first author, editor, or close
    or reopen actor that is not in *humans*. A trace by any identity not in
    *humans* is a warning, ``"<login> <action>"``, and the run carries on.
    """
    trusted = {bare_login(login) for login in humans}
    changes = [
        (identities.author, "authored the issue"),
        *((editor, "edited the body") for editor in identities.editors),
        *identities.state_actors,
    ]
    for login, action in changes:
        if login not in trusted:
            raise TrackingIssueBreach(login, action)
    return [f"{login} {action}" for login, action in identities.traces if login not in trusted]


def ensure_locked(repo: str, number: int) -> None:
    """Lock *repo*#*number* unless it is already locked; a failed read or lock raises."""
    owner, name = repo.split("/", 1)
    data = github.graphql(_LOCKED_QUERY, owner=owner, name=name, number=number)
    repository = _obj(data.get("repository"), repo, number, "repository")
    locked = _obj(repository.get("issue"), repo, number, "issue").get("locked")
    if not isinstance(locked, bool):
        raise _read_error(repo, number, f"the issue's locked flag is {locked!r}")
    if not locked:
        github.run("issue", "lock", str(number), "--repo", repo)
