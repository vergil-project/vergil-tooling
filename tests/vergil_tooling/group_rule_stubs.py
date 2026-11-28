"""Stubs for the whole-group rule's reads, for the tests that run the one PR path for real.

The rule reads a PR's GitHub closing references and body (:func:`trust.read_pr_closing`, the one
read :func:`pr_check.closing_of_pr` makes) and each issue's live groups (:func:`record.groups_of`).
:class:`GroupRule` answers both and records every group read. Its default answers no closing
reference, so the rule passes and reads no group. :func:`judge_pr_up_to_the_rule` lets a test run
the real :func:`pr_path.arm` or :func:`pr_path.push_to_open_pr` up to the rule, for a caller whose
own GitHub fake cannot serve the path's first read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib import finalize_watch, pr_path, trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.release_groups import record

if TYPE_CHECKING:
    import pytest

#: The real :func:`pr_path.arm`, captured at import, for a test whose fixture replaced it.
REAL_ARM = pr_path.arm

#: A PR body with no closing keyword.
PLAIN_BODY = "## Summary\n\nA fix.\n\nRef #5\n"
HEAD = "1" * 40


def two_member_group(first: IssueRef, second: IssueRef) -> record.Group:
    """A live group of two members, *first* and *second*, in that build order."""
    return record.Group(
        epic=IssueRef(first.owner, first.repo, 1672),
        group="g-pair",
        rev=1,
        members=(first, second),
        dissolved=False,
        comment_id="100",
    )


@dataclass
class GroupRule:
    """GitHub's closing references and body for the PR, and the live groups per issue.

    *closes* are GitHub's closing references, ``(repo, number)``. *body* is the PR body the
    rule parses. *groups* are the live groups per issue. *read_error*, when set, is raised by
    the closing read. *group_reads* records every issue whose groups were read.
    """

    closes: list[tuple[str, int]] = field(default_factory=list)
    body: str = PLAIN_BODY
    groups: dict[IssueRef, list[record.Group]] = field(default_factory=dict)
    read_error: Exception | None = None
    group_reads: list[IssueRef] = field(default_factory=list)

    def read_pr_closing(self, repo: str, number: int) -> trust.PRClosingNode:
        if self.read_error is not None:
            raise self.read_error
        nodes = [
            {"number": n, "state": "OPEN", "repository": {"nameWithOwner": r}}
            for r, n in self.closes
        ]
        node = {
            "body": self.body,
            "closingIssuesReferences": {"totalCount": len(nodes), "nodes": nodes},
        }
        return trust.PRClosingNode(
            repo=repo, number=number, state="OPEN", merged_at=None, node=node
        )

    def groups_of(self, task: IssueRef, *, authors: object = None) -> list[record.Group]:
        self.group_reads.append(task)
        return list(self.groups.get(task, []))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> GroupRule:
        monkeypatch.setattr(trust, "read_pr_closing", self.read_pr_closing)
        monkeypatch.setattr(record, "groups_of", self.groups_of)
        return self


def judge_pr_up_to_the_rule(
    monkeypatch: pytest.MonkeyPatch, *, repo: str, number: int, head_ref: str = "feature/7-fix"
) -> None:
    """Let the real arm or push run for PR *repo*#*number* up to the whole-group rule.

    The PR's first read is answered as an open, non-draft, same-repo PR with a trusted author
    on *head_ref*, and its provenance check passes. Nothing else is faked: the rule and every
    step after it run as they are. This does not touch :func:`pr_path.arm` itself: a caller
    that runs the real arm, and has replaced it, puts :data:`REAL_ARM` back.
    """
    url = f"https://github.com/{repo}/pull/{number}"
    snap = finalize_watch.Snapshot(
        node_id=f"PR_{number}",
        url=url,
        title="fix: a fix",
        state="OPEN",
        base="develop",
        head_ref=head_ref,
        head=HEAD,
        auto_merge_on=False,
        merge_state="BLOCKED",
        rollup=(),
        merge_commit="",
        labels=(),
    )
    pr = pr_path._PR(snap=snap, draft=False, repo=repo, number=number, branch=head_ref)  # noqa: SLF001
    monkeypatch.setattr(pr_path, "_read", lambda _url: pr)
    monkeypatch.setattr(pr_path, "_violations", lambda _url: "")
