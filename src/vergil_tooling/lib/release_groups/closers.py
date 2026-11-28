"""The merged PRs that close a task, read the way the group audit counts them.

Epic vergil-project/.github#1672, phase 3 Task 17 (spec §5.6). A closer is a merged PR of the
task's own repo whose closing reference to the task the trust layer counts
(:func:`~vergil_tooling.lib.pending_release.trusted_closing_refs`, the release's rule). A
merged PR in another repo, or one whose reference is not trusted, is not a closer. The read
refuses a truncated list and a malformed answer with :class:`ClosersReadError`; it never
reads as "no closers". Nothing here writes to GitHub.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import github, pending_release
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

# The task's closing PRs, each with the fields the closing-reference trust check reads.
_PR_LIMIT = 20
_CLOSING_PRS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    issue(number: $number) {{
      closedByPullRequestsReferences(first: {_PR_LIMIT}, includeClosedPrs: true) {{
        totalCount
        nodes {{
          number merged {pending_release.CLOSING_FIELDS}
        }}
      }}
    }}
  }}
}}
"""


class ClosersReadError(Exception):
    """A closing-PR read that cannot be trusted in full: it names the task and why."""


def _as_dict(value: object, what: str, task: IssueRef) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ClosersReadError(f"the closing PRs of {task.slug}: {what} is {value!r}")
    return cast("dict[str, object]", value)


def _connection(data: dict[str, object], task: IssueRef) -> list[object]:
    """The nodes of the closing-PR connection, refused when GitHub holds more than it returned."""
    repository = _as_dict(data.get("repository"), "the repository", task)
    issue = _as_dict(repository.get("issue"), "the issue", task)
    connection = _as_dict(issue.get("closedByPullRequestsReferences"), "the connection", task)
    nodes, total = connection.get("nodes"), connection.get("totalCount")
    if not isinstance(nodes, list) or not isinstance(total, int) or isinstance(total, bool):
        raise ClosersReadError(f"the closing PRs of {task.slug} have no totalCount or nodes")
    if total > len(nodes):
        raise ClosersReadError(
            f"the closing PRs of {task.slug}: GitHub lists {total} but returned {len(nodes)}"
        )
    return cast("list[object]", nodes)


def _merged_pr(node: object, task: IssueRef) -> IssueRef | None:
    """The PR a node names, as its own repo's ref, when the PR merged; else None."""
    pr = _as_dict(node, "a PR", task)
    number = pr.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        raise ClosersReadError(f"a closing PR of {task.slug} has no number: {pr!r}")
    if pr.get("merged") is not True:
        return None
    pr_repo = _as_dict(pr.get("repository"), f"the repository of PR #{number}", task)
    name = pr_repo.get("nameWithOwner")
    if not isinstance(name, str) or name.count("/") != 1:
        raise ClosersReadError(f"PR #{number} of {task.slug} has no repository name: {name!r}")
    owner, repo = name.split("/")
    if (owner.casefold(), repo.casefold()) != (task.owner.casefold(), task.repo.casefold()):
        return None
    return IssueRef(owner, repo, number)


def merged_closers(task: IssueRef, *, authors: TrustedAuthors) -> tuple[IssueRef, ...]:
    """The merged PRs of *task*'s own repo whose closing reference to *task* the trust rule counts.

    Raises :class:`ClosersReadError` on a truncated or malformed read. The trust layer's own
    errors (a read of who wrote a reference, a malformed PR) propagate: a failed read refuses.
    """
    data = github.graphql(_CLOSING_PRS_QUERY, owner=task.owner, name=task.repo, number=task.number)
    closers: list[IssueRef] = []
    for node in _connection(data, task):
        pr = _merged_pr(node, task)
        if pr is None:
            continue
        issues, _ = pending_release.trusted_closing_refs(
            cast("dict[str, object]", node),
            repo=f"{pr.owner}/{pr.repo}",
            authors=authors,
        )
        if task.number in issues:
            closers.append(pr)
    return tuple(closers)
