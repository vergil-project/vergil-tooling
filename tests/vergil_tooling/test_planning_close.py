"""Tests for the planning issue a documentation task's PR also closes (.github#950)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib import planning_close
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.planning_close import PlanningClose

if TYPE_CHECKING:
    from collections.abc import Callable

TASK = IssueRef("acme", ".github", 20)
EPIC = IssueRef("acme", ".github", 19)
PLANNING = IssueRef("acme", ".github", 7)
QUEUE = IssueRef("acme", ".github", 1)
TRUSTED = frozenset({"human", "agent-user"})


def _comment(body: str, login: str = "agent-user") -> dict[str, Any]:
    return {"author": {"login": login}, "body": body}


def _docs_marker(task: IssueRef = TASK, login: str = "agent-user") -> dict[str, Any]:
    return _comment(
        f"Epic created; next: the spec.\n\nEpic: {EPIC.slug}\nDocs task: {task.slug}\n", login
    )


class _GitHub:
    """A fake GitHub holding the epic body and each planning issue's view."""

    def __init__(
        self,
        *,
        epic_body: str = f"Planning: {PLANNING.slug}\n\nThe epic.",
        views: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.epic_body = epic_body
        self.views = (
            views
            if views is not None
            else {PLANNING.number: {"state": "OPEN", "comments": [_docs_marker()]}}
        )

    def read_output(self, *args: str) -> str:
        assert args == ("api", f"repos/acme/.github/issues/{EPIC.number}", "--jq", ".body")
        return self.epic_body

    def read_json(self, *args: str) -> dict[str, Any]:
        assert args[:2] == ("issue", "view")
        return self.views[int(args[2])]


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _GitHub]:
    """Install a fake GitHub; the planning issue sits in the planning queue."""

    def install(**kwargs: Any) -> _GitHub:
        gh = _GitHub(**kwargs)
        monkeypatch.setattr(planning_close.github, "read_output", gh.read_output)
        monkeypatch.setattr(planning_close.github, "read_json", gh.read_json)
        return gh

    monkeypatch.setattr(planning_close.epics, "parent_of", lambda _task: EPIC)
    monkeypatch.setattr(planning_close.epics, "native_parent_of", lambda _ref: QUEUE)
    monkeypatch.setattr(
        planning_close.epics, "queue_kind", lambda ref: "planning" if ref == QUEUE else None
    )
    monkeypatch.setattr(planning_close.epic_size, "trusted_logins", lambda: TRUSTED)
    monkeypatch.setattr(planning_close.queue_view, "tracking_advisories", lambda _ref, _body: ())
    return install


def test_docs_task_closes_its_planning_issue(fake: Callable[..., _GitHub]) -> None:
    fake()
    assert planning_close.find(TASK) == PlanningClose(TASK, PLANNING, None)


def test_app_prefixed_author_is_trusted(fake: Callable[..., _GitHub]) -> None:
    fake(views={7: {"state": "OPEN", "comments": [_docs_marker(login="app/agent-user")]}})
    result = planning_close.find(TASK)
    assert result is not None
    assert result.closes


def test_task_with_no_epic_is_not_a_docs_task(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake()
    monkeypatch.setattr(planning_close.epics, "parent_of", lambda _task: None)
    assert planning_close.find(TASK) is None


def test_epic_with_no_planning_line_is_not_planned(fake: Callable[..., _GitHub]) -> None:
    fake(epic_body="An ad-hoc epic.\n\nPlanning issue lives elsewhere: no line here.")
    assert planning_close.find(TASK) is None


def test_other_task_of_a_planned_epic_is_not_the_docs_task(fake: Callable[..., _GitHub]) -> None:
    fake(views={7: {"state": "OPEN", "comments": [_docs_marker(IssueRef("acme", ".github", 21))]}})
    assert planning_close.find(TASK) is None


def test_untrusted_docs_marker_is_ignored(fake: Callable[..., _GitHub]) -> None:
    fake(views={7: {"state": "OPEN", "comments": [_docs_marker(login="stranger")]}})
    assert planning_close.find(TASK) is None


def test_latest_trusted_docs_marker_wins(fake: Callable[..., _GitHub]) -> None:
    other = IssueRef("acme", ".github", 21)
    fake(views={7: {"state": "OPEN", "comments": [_docs_marker(), _docs_marker(other)]}})
    assert planning_close.find(TASK) is None


def test_closed_planning_issue_is_skipped(fake: Callable[..., _GitHub]) -> None:
    fake(views={7: {"state": "CLOSED", "comments": [_docs_marker()]}})
    result = planning_close.find(TASK)
    assert result == PlanningClose(TASK, PLANNING, "it is already closed")
    assert "does not close it: it is already closed." in planning_close.describe(result)


def test_planning_issue_outside_the_queue_is_skipped(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake()
    deferred = IssueRef("acme", ".github", 2)
    monkeypatch.setattr(planning_close.epics, "native_parent_of", lambda _ref: deferred)
    result = planning_close.find(TASK)
    assert result is not None
    assert result.skipped == f"it is not in the planning queue (parent {deferred.slug})"


def test_unparented_planning_issue_is_skipped(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake()
    monkeypatch.setattr(planning_close.epics, "native_parent_of", lambda _ref: None)
    result = planning_close.find(TASK)
    assert result is not None
    assert result.skipped == "it is not in the planning queue (no parent)"


def test_planning_issue_held_by_an_advisory_is_skipped(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "Neutral body.\n\nSecurity advisory: acme/.github GHSA-aaaa-aaaa-aaaa"
    fake(views={7: {"state": "OPEN", "body": body, "comments": [_docs_marker()]}})
    seen: list[tuple[IssueRef, str]] = []

    def tracking(ref: IssueRef, issue_body: str) -> tuple[str, ...]:
        seen.append((ref, issue_body))
        return ("GHSA-aaaa-aaaa-aaaa", "GHSA-bbbb-bbbb-bbbb")

    monkeypatch.setattr(planning_close.queue_view, "tracking_advisories", tracking)
    result = planning_close.find(TASK)
    assert seen == [(PLANNING, body)]
    assert result is not None
    assert result.skipped == (
        "2 advisories still track it (GHSA-aaaa-aaaa-aaaa, GHSA-bbbb-bbbb-bbbb); retarget "
        "each to the task or tasks that fix it with vrg-security-record retarget, then "
        "close the planning issue by hand"
    )


def test_cross_repo_planning_issue_is_skipped(fake: Callable[..., _GitHub]) -> None:
    task = IssueRef("acme", "private-repo", 20)
    fake(views={7: {"state": "OPEN", "comments": [_docs_marker(task)]}})
    result = planning_close.find(task)
    assert result is not None
    assert not result.closes
    assert result.skipped is not None
    assert "another repo" in result.skipped


def test_two_planning_issues_naming_the_task_are_skipped(fake: Callable[..., _GitHub]) -> None:
    fake(
        epic_body="Planning: acme/.github#7\nPlanning: acme/.github#8\n",
        views={
            7: {"state": "OPEN", "comments": [_docs_marker()]},
            8: {"state": "OPEN", "comments": [_docs_marker()]},
        },
    )
    result = planning_close.find(TASK)
    assert result is not None
    assert result.skipped == (
        "more than one planning issue names it as their docs task (acme/.github#7, acme/.github#8)"
    )


def test_second_planning_line_naming_another_task_is_ignored(
    fake: Callable[..., _GitHub],
) -> None:
    other = IssueRef("acme", ".github", 21)
    fake(
        epic_body="Planning: acme/.github#7\nPlanning: acme/.github#8\n",
        views={
            7: {"state": "OPEN", "comments": [_docs_marker()]},
            8: {"state": "OPEN", "comments": [_docs_marker(other)]},
        },
    )
    assert planning_close.find(TASK) == PlanningClose(TASK, PLANNING, None)


def test_failed_read_propagates(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake()

    def boom(*_args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(planning_close.github, "read_output", boom)
    with pytest.raises(subprocess.CalledProcessError):
        planning_close.find(TASK)


def test_planning_refs_reads_only_whole_lines() -> None:
    body = (
        "Planning: acme/.github#7\n"
        "  Planning:acme/.github#7  \n"
        "See Planning: acme/.github#9 in prose\n"
        "Planning: acme/other#3\n"
    )
    assert planning_close.planning_refs(body) == [
        IssueRef("acme", ".github", 7),
        IssueRef("acme", "other", 3),
    ]


def test_describe_a_close() -> None:
    assert planning_close.describe(PlanningClose(TASK, PLANNING, None)) == (
        "acme/.github#20 is the documentation task of planning issue acme/.github#7; "
        "the PR also closes it on merge."
    )
