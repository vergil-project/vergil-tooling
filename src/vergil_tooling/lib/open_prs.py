"""Every open PR on the remote: the data layer of ``vrg-repo-status``'s open-PR view.

:func:`gather_open_prs` reads the open PRs of the remote's GitHub repository
and returns one :class:`OpenPR` each, oldest first. The caller drops the ones a
local worktree already shows, so what is left are the PRs that never touched
this checkout: automation and lifecycle PRs, dependency updates, and PRs
prepared on another machine (issue vergil-project/vergil-tooling#4581).

It is one GraphQL query per :data:`_PAGE_SIZE` PRs, never one per PR. The
collected list is checked against the connection's ``totalCount``, and a
connection a node truncates (its labels) is an error, so the answer is
complete or a :class:`OpenPRsError` is raised; there is never a partial list,
and a failed lookup is never shown as "no open PRs". Error reasons pass
through :func:`vergil_tooling.lib.pending_release.lookup_failure_reason`, which
strips URL userinfo.
"""

from __future__ import annotations

import datetime
import enum
from dataclasses import dataclass
from typing import cast

from vergil_tooling.lib import git, github
from vergil_tooling.lib.pending_release import (
    LOOKUP_ERRORS,
    ChangeKind,
    classify,
    lookup_failure_reason,
)

_PAGE_SIZE = 100
_LABEL_LIMIT = 20

# Language lifecycle PRs (a version added or dropped) are opened from
# ``lifecycle/<language>-<add|drop>-<version>`` branches.
_LIFECYCLE_BRANCH_PREFIX = "lifecycle/"

# GitHub shows a deleted account as this login.
_GHOST = "ghost"

_QUERY = f"""query($owner: String!, $name: String!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(
      states: OPEN, first: {_PAGE_SIZE}, after: $cursor,
      orderBy: {{field: CREATED_AT, direction: ASC}}
    ) {{
      totalCount
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        number title headRefName baseRefName isDraft isCrossRepository createdAt
        author {{ login }}
        headRepositoryOwner {{ login }}
        labels(first: {_LABEL_LIMIT}) {{ totalCount nodes {{ name }} }}
        commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}
      }}
    }}
  }}
}}"""


class OpenPRsError(RuntimeError):
    """The open PRs could not be read completely; the message is the reason."""


class OpenPRKind(enum.Enum):
    """What kind of PR an open row is."""

    AUTOMATION = "automation"
    LIFECYCLE = "lifecycle"
    DEPS = "deps"
    RELEASE = "release"
    OTHER = "other"


_KIND_BY_CHANGE = {
    ChangeKind.AUTOMATION: OpenPRKind.AUTOMATION,
    ChangeKind.DEPS: OpenPRKind.DEPS,
    ChangeKind.RELEASE: OpenPRKind.RELEASE,
    ChangeKind.OTHER: OpenPRKind.OTHER,
}


@dataclass(frozen=True)
class OpenPR:
    """One open PR on the remote."""

    number: int
    title: str
    branch: str
    """The head branch name, as it is in the head repository."""
    head_owner: str | None
    """The head repository's owner for a PR from a fork, else ``None``."""
    base: str
    kind: OpenPRKind
    type_label: str
    """``lifecycle``, ``automation``, ``deps``, ``release``, the conventional type, or ``-``."""
    author: str
    draft: bool
    ci: str
    """The head commit's combined check state, lowercased (``success``,
    ``failure``, ``pending``, ``error``, ``expected``), or ``none`` when the
    commit has no checks."""
    created_ts: float

    @property
    def cross_repository(self) -> bool:
        return self.head_owner is not None


def classify_open(title: str, branch: str, labels: tuple[str, ...]) -> tuple[OpenPRKind, str]:
    """The kind and TYPE label of an open PR: a lifecycle branch, else as a pending change."""
    if branch.startswith(_LIFECYCLE_BRANCH_PREFIX):
        return OpenPRKind.LIFECYCLE, "lifecycle"
    kind, type_label = classify(title, branch, labels)
    return _KIND_BY_CHANGE[kind], type_label


def _obj(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{what} is not an object: {type(value).__name__}"
        raise TypeError(msg)
    return cast("dict[str, object]", value)


def _list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"{what} is not a list: {type(value).__name__}"
        raise TypeError(msg)
    return cast("list[object]", value)


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        msg = f"{what} is not a string: {type(value).__name__}"
        raise TypeError(msg)
    return value


def _int(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{what} is not an integer: {type(value).__name__}"
        raise TypeError(msg)
    return value


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        msg = f"{what} is not a boolean: {type(value).__name__}"
        raise TypeError(msg)
    return value


def _ci_state(node: dict[str, object], number: int) -> str:
    commits = _list(
        _obj(node["commits"], f"commits of #{number}")["nodes"], f"commits of #{number}"
    )
    if not commits:
        return "none"
    commit = _obj(_obj(commits[0], f"commit of #{number}")["commit"], f"commit of #{number}")
    rollup = commit["statusCheckRollup"]
    if rollup is None:
        return "none"
    return _str(_obj(rollup, f"checks of #{number}")["state"], f"check state of #{number}").lower()


def _login(value: object, what: str) -> str | None:
    if value is None:
        return None
    return _str(_obj(value, what)["login"], f"{what} login")


def _open_pr(node: dict[str, object]) -> OpenPR:
    number = _int(node["number"], "PR number")
    title = _str(node["title"], f"title of #{number}")
    branch = _str(node["headRefName"], f"headRefName of #{number}")
    labels_conn = _obj(node["labels"], f"labels of #{number}")
    label_nodes = _list(labels_conn["nodes"], f"labels of #{number}")
    if _int(labels_conn["totalCount"], f"labels totalCount of #{number}") > len(label_nodes):
        raise OpenPRsError(f"labels of #{number} truncated")
    labels = tuple(
        _str(_obj(label, f"label of #{number}")["name"], f"label name of #{number}")
        for label in label_nodes
    )
    head_owner: str | None = None
    if _bool(node["isCrossRepository"], f"isCrossRepository of #{number}"):
        # A fork that was deleted has no owner left to name.
        head_owner = _login(node["headRepositoryOwner"], f"head owner of #{number}") or _GHOST
    kind, type_label = classify_open(title, branch, labels)
    return OpenPR(
        number=number,
        title=title,
        branch=branch,
        head_owner=head_owner,
        base=_str(node["baseRefName"], f"baseRefName of #{number}"),
        kind=kind,
        type_label=type_label,
        author=_login(node["author"], f"author of #{number}") or _GHOST,
        draft=_bool(node["isDraft"], f"isDraft of #{number}"),
        ci=_ci_state(node, number),
        created_ts=datetime.datetime.fromisoformat(
            _str(node["createdAt"], f"createdAt of #{number}")
        ).timestamp(),
    )


def _gather(remote: str) -> tuple[OpenPR, ...]:
    repo = github.github_remote_repo(git.remote_url(remote))
    if repo is None:
        raise OpenPRsError(f"{remote} is not a github.com remote")
    owner, name = repo.split("/", 1)
    prs: dict[int, OpenPR] = {}
    total = 0
    cursor: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": name}
        if cursor is not None:
            variables["cursor"] = cursor
        data = github.graphql(_QUERY, **variables)
        repository = _obj(data["repository"], "repository")
        connection = _obj(repository["pullRequests"], "pullRequests")
        total = _int(connection["totalCount"], "pullRequests totalCount")
        for node in _list(connection["nodes"], "pullRequests nodes"):
            pr = _open_pr(_obj(node, "pullRequests node"))
            prs[pr.number] = pr
        page_info = _obj(connection["pageInfo"], "pageInfo")
        if not _bool(page_info["hasNextPage"], "hasNextPage"):
            break
        cursor = _str(page_info["endCursor"], "endCursor")
    if len(prs) != total:
        # A PR opened or closed between pages: the list is not one snapshot.
        msg = f"read {len(prs)} of {total} open PRs; the list changed during the read"
        raise OpenPRsError(msg)
    return tuple(sorted(prs.values(), key=lambda pr: pr.number))


def gather_open_prs(*, remote: str = "origin") -> tuple[OpenPR, ...]:
    """Every open PR of *remote*'s GitHub repository, in PR-number order.

    Read-only. Raises :class:`OpenPRsError` when the lookup fails or is
    incomplete, with the underlying error as its ``__cause__``.
    """
    try:
        return _gather(remote)
    except OpenPRsError:
        # Its own reasons name only the remote and PR numbers, never a URL.
        raise
    except LOOKUP_ERRORS as exc:
        raise OpenPRsError(lookup_failure_reason(exc)) from exc
