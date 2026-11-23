"""The tracking issue's lock and identity rule (spec §4.2, epic vergil-project/.github#1440).

The tracking issue holds the release state, so ``vrg-release`` trusts it only
while every identity that can change that state holds the ``human`` role
(:data:`TRACKING_ROLES`): the issue's author, its body's last editor and every
editor in its edit history, and every actor that closed or reopened it. Any
other such identity is a suspected security breach (:class:`TrackingIssueBreach`)
that stops the run before it writes anything. Other traces (a comment, an edit
of a comment, a cross-reference, a mention, a subscription or a label by
another identity) cannot change the state, so they are only warnings.

The rule is judged by the trust layer (:mod:`vergil_tooling.lib.trust`,
epic vergil-project/.github#1467) with the ``human`` role only: the body and
comments through :func:`~vergil_tooling.lib.trust.read_issue`, the close,
reopen and trace events through :func:`~vergil_tooling.lib.trust.read_events`.
A ``human`` login whose account is not a ``User`` holds no role, so it is a
breach too.

The issue is locked, so only repository collaborators can comment;
:func:`ensure_locked` locks one that is not, an inherited issue included.

Every read here fails loudly: a GraphQL failure, a response that is not what
was asked for, or a connection that cannot be read in full raises, never
passes as "no other identity" (spec §6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github, trust
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

# The roles that may change the tracking issue's state: the human only.
TRACKING_ROLES: frozenset[trust.Role] = frozenset({trust.Role.HUMAN})

# How a deleted account (a ``null`` author, editor or actor) is named.
GHOST = trust.GHOST

# The action each event kind records. A close or reopen changes the issue's
# state; every other kind is a trace.
_STATE_ACTIONS = {
    "closed": "closed the issue",
    "reopened": "reopened the issue",
}
_TRACE_ACTIONS = {
    "cross-referenced": "cross-referenced the issue",
    "mentioned": "mentioned the issue",
    "subscribed": "subscribed to the issue",
    "labeled": "added a label",
    "unlabeled": "removed a label",
}
_COMMENT_ACTIONS = {"comment": "commented", "edit": "edited a comment"}
# The kinds trust.Skipped gives the body's author and an untrusted editor.
_AUTHOR_KINDS = ("issue", "pr")
_BODY_FIELD = "body"

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
    once the issue's number is known, and names the identity's cause
    (``untrusted``, ``deleted``, ``wrong-type`` or ``not-accepted``) when it
    is known. It tells the human to inspect the issue, and that it cannot be
    cleaned up to pass the rule again, since GitHub keeps the editor of a
    deleted revision and every close and reopen event: the release is
    superseded instead, which rebuilds it from GitHub and git facts
    (:mod:`~vergil_tooling.lib.release.rebuild`). It is a
    :class:`ReleaseError` so it reports as one, but a caller that comments on
    a failed stage must let it pass uncommented: the run stops before any
    write.
    """

    def __init__(
        self, login: str, action: str, *, number: int | None = None, cause: str | None = None
    ) -> None:
        self.login = login
        self.action = action
        self.number = number
        self.cause = cause
        where = f"tracking issue #{number}" if number is not None else "the tracking issue"
        why = f" ({cause})" if cause is not None else ""
        super().__init__(
            phase="state",
            command="check_tracking_issue",
            message=(
                f"suspected security breach on {where}: {login} {action}{why}, and only a "
                "human login of vrg-trusted-authors may author, edit, close or reopen it. "
                "vrg-release stopped before writing anything. Inspect the issue's edit "
                "history and events. GitHub keeps them, so the issue can never pass this "
                "check again and the release cannot be resumed: replace it with "
                "`vrg-release --supersede <version>`, which does not read the issue's state "
                "and rebuilds the release from GitHub and git facts."
            ),
        )


def _name(login: str | None) -> str:
    """A bare login, or :data:`GHOST` for a deleted account."""
    return GHOST if login is None else trust.normalise_login(login)


def check_tracking_issue(repo: str, number: int, *, authors: TrustedAuthors) -> list[str]:
    """Apply the identity rule to *repo*#*number*; return one warning per foreign trace.

    Reads the issue through the trust layer with :data:`TRACKING_ROLES` and
    every editor checked. Raises :class:`TrackingIssueBreach` naming the
    issue for the body's author or editor, then for the first close or
    reopen actor, that holds no accepted role. A comment, an edit of a
    comment or a trace event by such an identity is a warning,
    ``"<login> <action>"``, in time order, and the run carries on. A read
    that cannot complete raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`.
    """
    content = trust.read_issue(repo, number, roles=TRACKING_ROLES, authors=authors)
    for item in content.skipped:
        if item.field == _BODY_FIELD:
            action = "authored the issue" if item.kind in _AUTHOR_KINDS else "edited the body"
            raise TrackingIssueBreach(_name(item.login), action, number=number, cause=item.cause)
    org = repo.partition("/")[0]
    # Every other skip is a comment's: its author ("comment") or an editor ("edit").
    traces = [
        (item.at, f"{_name(item.login)} {_COMMENT_ACTIONS[item.kind]}") for item in content.skipped
    ]
    for event in trust.read_events(repo, number):
        judgement = trust.judge_identity(
            event.actor, org=org, roles=TRACKING_ROLES, authors=authors
        )
        if judgement.role is not None:
            continue
        login = _name(event.actor.login)
        if event.kind in _STATE_ACTIONS:
            raise TrackingIssueBreach(
                login, _STATE_ACTIONS[event.kind], number=number, cause=judgement.cause
            )
        traces.append((event.at, f"{login} {_TRACE_ACTIONS[event.kind]}"))
    return [warning for _, warning in sorted(traces, key=lambda trace: trace[0])]


def _read_error(repo: str, number: int, detail: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(
        1,
        ("gh", "api", "graphql"),
        stderr=f"cannot read the lock on tracking issue {repo}#{number}: {detail}",
    )


def _obj(value: object, repo: str, number: int, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _read_error(repo, number, f"{what} is {value!r}")
    return cast("dict[str, Any]", value)


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
