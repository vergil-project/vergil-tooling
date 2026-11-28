"""Tests for the whole-group rule on a PR: every group a PR touches is whole, in build order.

Epic vergil-project/.github#1672, phase 3 Task 11 (spec §5.2). Group records are faked at
:func:`record.groups_of`; a PR's closing references and body are faked at
:func:`trust.read_pr_closing`, the one read :mod:`pr_check` makes of a PR.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib import epics, github, trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.pr_workflow.batch_rules import BatchRuleError
from vergil_tooling.lib.release_groups import pr_check, record
from vergil_tooling.lib.release_groups.record import Group, GroupRecordError

if TYPE_CHECKING:
    from collections.abc import Sequence

EPIC = IssueRef("vergil-project", ".github", 1672)
OTHER_EPIC = IssueRef("vergil-project", ".github", 1818)
REPO = "vergil-project/vergil-tooling"
TOOLS = "vergil-project/vergil-tooling"


def _ref(number: int) -> IssueRef:
    return IssueRef("vergil-project", "vergil-tooling", number)


A, B, C, X = _ref(900), _ref(901), _ref(902), _ref(910)


def _group(
    group_id: str, members: Sequence[IssueRef], epic: IssueRef = EPIC, comment: str = "100"
) -> Group:
    return Group(
        epic=epic,
        group=group_id,
        rev=1,
        members=tuple(members),
        dissolved=False,
        comment_id=comment,
    )


class FakeGroups:
    """The live groups per task, with a failure per task where one is set."""

    def __init__(self) -> None:
        self.groups: list[Group] = []
        self.fail: dict[IssueRef, Exception] = {}
        self.calls: list[IssueRef] = []

    def __call__(self, task: IssueRef, *, authors: Any = None) -> list[Group]:
        self.calls.append(task)
        if task in self.fail:
            raise self.fail[task]
        return [g for g in self.groups if task in g.members]


@pytest.fixture
def groups(monkeypatch: pytest.MonkeyPatch) -> FakeGroups:
    fake = FakeGroups()
    monkeypatch.setattr(record, "groups_of", fake)
    return fake


def _refused(closing: Sequence[IssueRef], *, order: Sequence[IssueRef] | None = None) -> str:
    with pytest.raises(BatchRuleError) as info:
        pr_check.check_whole_groups(closing, order=order)
    return str(info.value)


def _rule(closing: Sequence[IssueRef], *, order: Sequence[IssueRef] | None = None) -> str:
    with pytest.raises(BatchRuleError) as info:
        pr_check.check_whole_groups(closing, order=order)
    return info.value.rule


# -- check_whole_groups ------------------------------------------------------------


def test_whole_group_and_standalone_allowed(groups: FakeGroups) -> None:
    groups.groups = [_group("G1", [A, B])]
    pr_check.check_whole_groups([A, B])
    pr_check.check_whole_groups([C])


def test_no_records_refuses_nothing(groups: FakeGroups) -> None:
    # The rule is live with no group: any PR passes, in any order.
    pr_check.check_whole_groups([A, B], order=[B, A])
    assert groups.calls == [A, B]


def test_empty_closing_reads_no_group(groups: FakeGroups) -> None:
    pr_check.check_whole_groups([], order=[])
    assert groups.calls == []


def test_one_member_group_single_pr_allowed(groups: FakeGroups) -> None:
    groups.groups = [_group("G1", [A])]
    pr_check.check_whole_groups([A])


def test_group_order_violation_refused(groups: FakeGroups) -> None:
    groups.groups = [_group("G1", [A, B])]
    assert _rule([A, B], order=[B, A]) == "group order"
    message = _refused([A, B], order=[B, A])
    assert "G1 of vergil-project/.github#1672" in message
    assert "out of order: vergil-project/vergil-tooling#901" in message


def test_interleaved_groups_keep_own_order(groups: FakeGroups) -> None:
    # Review Focus 5: G1 is A then B; G2 is X. A batch may interleave the groups.
    groups.groups = [_group("G1", [A, B]), _group("G2", [X], epic=OTHER_EPIC)]
    pr_check.check_whole_groups([A, X, B], order=[A, X, B])
    assert _rule([A, X, B], order=[B, X, A]) == "group order"


def test_partial_group_refused_naming_group_epic_and_missing(groups: FakeGroups) -> None:
    groups.groups = [_group("G1", [A, B, C])]
    assert _rule([A]) == "whole groups"
    message = _refused([A])
    assert (
        "G1 of vergil-project/.github#1672 is missing "
        "vergil-project/vergil-tooling#901, vergil-project/vergil-tooling#902" in message
    )


def test_each_partial_group_is_named(groups: FakeGroups) -> None:
    groups.groups = [_group("G1", [A, B]), _group("G2", [X, C], epic=OTHER_EPIC)]
    message = _refused([A, X])
    assert "G1 of vergil-project/.github#1672 is missing vergil-project/vergil-tooling#901" in (
        message
    )
    assert "G2 of vergil-project/.github#1818 is missing vergil-project/vergil-tooling#902" in (
        message
    )


def test_members_match_case_insensitively_and_dedupe(groups: FakeGroups) -> None:
    groups.groups = [_group("G1", [A, B])]
    pr_check.check_whole_groups(
        [IssueRef("Vergil-Project", "Vergil-Tooling", 900), A, B, B], order=[A, B]
    )


def test_edited_record_refuses_pr(groups: FakeGroups) -> None:
    groups.fail[A] = GroupRecordError(
        "vergil-project/.github group G1 cannot be read: comment 100 was edited after it was posted"
    )
    assert _rule([A]) == "whole groups"
    message = _refused([A])
    assert "could not read the group records of vergil-project/vergil-tooling#900" in message
    assert "was edited after it was posted" in message


def test_unreadable_epic_refuses_pr(groups: FakeGroups) -> None:
    groups.fail[B] = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
    assert _rule([A, B]) == "whole groups"
    assert "could not read the group records of vergil-project/vergil-tooling#901" in _refused(
        [A, B]
    )


def test_unreadable_trust_read_refuses_pr(groups: FakeGroups) -> None:
    groups.fail[A] = trust.TrustReadError("read", "the comments were cut short")
    assert _rule([A]) == "whole groups"


def test_missing_token_propagates(groups: FakeGroups) -> None:
    groups.fail[A] = github.MissingGitHubTokenError(1, ["gh", "api"], "", "no token")
    with pytest.raises(github.MissingGitHubTokenError):
        pr_check.check_whole_groups([A])


# -- closing_of_pr and closing_body_refs -------------------------------------------


def _pr_node(
    body: str,
    closing: Sequence[tuple[str, int]] = (),
    *,
    total: int | None = None,
) -> dict[str, Any]:
    return {
        "body": body,
        "closingIssuesReferences": {
            "totalCount": len(closing) if total is None else total,
            "nodes": [
                {"number": number, "state": "OPEN", "repository": {"nameWithOwner": repo}}
                for repo, number in closing
            ],
        },
    }


@pytest.fixture
def pr(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The PR read by :func:`trust.read_pr_closing`: set ``pr["node"]`` to answer it."""
    state: dict[str, Any] = {"node": _pr_node("")}

    def read(repo: str, number: int) -> trust.PRClosingNode:
        return trust.PRClosingNode(
            repo=repo, number=number, state="OPEN", merged_at=None, node=state["node"]
        )

    monkeypatch.setattr(trust, "read_pr_closing", read)
    return state


@pytest.mark.parametrize(
    "keyword",
    [
        "close",
        "closes",
        "closed",
        "fix",
        "fixes",
        "fixed",
        "resolve",
        "resolves",
        "resolved",
    ],
)
@pytest.mark.parametrize("form", ["{kw} #900", "{kw}: #900", "{upper}: #900", "{kw}  #900"])
def test_body_keyword_family_counts(keyword: str, form: str) -> None:
    body = "Summary.\n\n" + form.format(kw=keyword, upper=keyword.upper())
    assert pr_check.closing_body_refs(body, REPO) == [A]


def test_body_refs_anywhere_in_text_and_cross_repo_and_url() -> None:
    body = (
        "Fixes vergil-project/.github#1672 and it touches things.\n"
        "Some prose. Resolves https://github.com/vergil-project/vergil-tooling/issues/901."
    )
    assert pr_check.closing_body_refs(body, REPO) == [EPIC, B]


def test_body_refs_ignore_references_that_do_not_close() -> None:
    body = "Refs #7, relates to #8, prefixes #9, closing #10, see the fixes-list."
    assert pr_check.closing_body_refs(body, REPO) == []


def test_body_refs_dedupe_preserving_order() -> None:
    body = "Fixes #902\nCloses #900\nFixes #902"
    assert pr_check.closing_body_refs(body, REPO) == [C, A]


def test_uncounted_fixes_reference_counts_for_rule(
    pr: dict[str, Any], groups: FakeGroups
) -> None:
    # The body's Fixes line is not a closing reference GitHub counts, yet it closes #900.
    pr["node"] = _pr_node("Fixes #900")
    assert pr_check.closing_of_pr(REPO, 5) == [A]
    groups.groups = [_group("G1", [A, B])]
    with pytest.raises(BatchRuleError) as info:
        pr_check.check_pr_whole_groups(REPO, 5)
    assert info.value.rule == "whole groups"
    assert "missing vergil-project/vergil-tooling#901" in str(info.value)


def test_cross_repo_closing_reference_counts_for_rule(
    pr: dict[str, Any], groups: FakeGroups
) -> None:
    # A closing reference GitHub lists, in another repo, is counted and judged too.
    pr["node"] = _pr_node("Summary.", [("vergil-project/.github", 1672)])
    assert pr_check.closing_of_pr(REPO, 5) == [EPIC]
    groups.groups = [_group("G1", [EPIC, C])]
    assert _rule(pr_check.closing_of_pr(REPO, 5)) == "whole groups"


def test_untrusted_body_reference_counts_for_rule(
    pr: dict[str, Any], groups: FakeGroups
) -> None:
    # The body is parsed whatever its author: an untrusted reference refuses, never allows.
    pr["node"] = _pr_node("Fixes #900 by an outsider")
    groups.groups = [_group("G1", [A, B])]
    assert pr_check.closing_of_pr(REPO, 5) == [A]
    assert _rule(pr_check.closing_of_pr(REPO, 5)) == "whole groups"


def test_closing_of_pr_joins_closing_and_body_refs_once(pr: dict[str, Any]) -> None:
    pr["node"] = _pr_node("Closes #900", [(TOOLS, 900), (TOOLS, 901)])
    assert pr_check.closing_of_pr(REPO, 5) == [A, B]


def test_truncated_closing_references_refuse(pr: dict[str, Any]) -> None:
    pr["node"] = _pr_node("Summary.", [(TOOLS, 900)], total=3)
    with pytest.raises(trust.TrustReadError, match="truncated: 3 listed, 1 read"):
        pr_check.closing_of_pr(REPO, 5)


def test_malformed_closing_node_refuses(pr: dict[str, Any]) -> None:
    pr["node"] = {
        "body": "Summary.",
        "closingIssuesReferences": {"totalCount": 1, "nodes": [{"number": 900}]},
    }
    with pytest.raises(trust.TrustReadError):
        pr_check.closing_of_pr(REPO, 5)


def test_pr_read_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(repo: str, number: int) -> trust.PRClosingNode:
        raise trust.TrustReadError("read_pr_closing", "the pull request is gone")

    monkeypatch.setattr(trust, "read_pr_closing", fail)
    with pytest.raises(trust.TrustReadError, match="the pull request is gone"):
        pr_check.check_pr_whole_groups(REPO, 5)
