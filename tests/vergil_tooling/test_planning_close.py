"""Tests for the planning issue a documentation task's PR also closes (.github#950)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.lib import epic_chain, planning_close, queue_view, trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.planning_close import PlanningClose
from vergil_tooling.lib.queue_view import AdvisoryHold
from vergil_tooling.lib.security_records import Advisory
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Callable

TASK = IssueRef("acme", ".github", 20)
EPIC = IssueRef("acme", ".github", 19)
PLANNING = IssueRef("acme", ".github", 7)
QUEUE = IssueRef("acme", ".github", 1)


def _comment(body: str, author: Any = trust_stubs.AGENT, *, editor: Any = None) -> dict[str, Any]:
    """An ``IssueComment`` node by *author*; *editor* makes it edited once, by that editor."""
    return trust_stubs.comment(1, body, author, editor=editor)


def _docs_marker(
    task: IssueRef = TASK, author: Any = trust_stubs.AGENT, *, editor: Any = None
) -> dict[str, Any]:
    return _comment(
        f"Epic created; next: the spec.\n\nEpic: {EPIC.slug}\nDocs task: {task.slug}\n",
        author,
        editor=editor,
    )


def _numbered(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*nodes* renumbered from 1, as GitHub gives each comment its own id."""
    return [
        {**node, "id": f"IC_{i}", "fullDatabaseId": str(i)} for i, node in enumerate(nodes, start=1)
    ]


class _GitHub:
    """A fake GitHub holding the epic body and each planning issue.

    Each planning issue's *views* entry holds its ``state`` and ``labels``
    (what ``gh issue view`` reads), and what the trust layer reads: its
    ``body`` (empty unless given), the body's ``author`` and ``editor`` (the
    trusted human, unedited, unless given) and its ``comments`` (comment
    nodes). The epic body is written by *epic_author*.
    """

    def __init__(
        self,
        *,
        epic_body: str = f"Planning: {PLANNING.slug}\n\nThe epic.",
        epic_author: Any = trust_stubs.HUMAN,
        views: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.epic_body = epic_body
        self.epic_author = epic_author
        self.views = (
            views
            if views is not None
            else {PLANNING.number: {"state": "OPEN", "comments": [_docs_marker()]}}
        )

    def read_json(self, *args: str) -> dict[str, Any]:
        assert args[:2] == ("issue", "view")
        assert args[-2:] == ("--json", "state,labels"), "content is read through the trust layer"
        view = self.views[int(args[2])]
        return {"state": view.get("state"), "labels": view.get("labels") or []}

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        assert query == trust._ITEM_QUERY, "only the item read is faked"
        assert (variables["owner"], variables["name"]) == ("acme", ".github")
        number = int(variables["number"])
        if number == EPIC.number:
            return trust_stubs.item(body=self.epic_body, author=self.epic_author)
        view = self.views[number]
        return trust_stubs.item(
            body=view.get("body", ""),
            author=view.get("author", trust_stubs.HUMAN),
            editor=view.get("editor"),
            nodes=_numbered(view.get("comments") or []),
        )


def _no_read(*args: str) -> str:
    raise AssertionError(f"the epic body is read through the trust layer, not {args!r}")


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _GitHub]:
    """Install a fake GitHub; the planning issue sits in the planning queue."""

    def install(**kwargs: Any) -> _GitHub:
        gh = _GitHub(**kwargs)
        monkeypatch.setattr(planning_close.github, "read_output", _no_read)
        monkeypatch.setattr(planning_close.github, "read_json", gh.read_json)
        monkeypatch.setattr(planning_close.github, "graphql", gh.graphql)
        return gh

    monkeypatch.setattr(planning_close.epics, "parent_of", lambda _task: EPIC)
    monkeypatch.setattr(planning_close.epics, "native_parent_of", lambda _ref: QUEUE)
    monkeypatch.setattr(
        planning_close.epics, "queue_kind", lambda ref: "planning" if ref == QUEUE else None
    )
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    monkeypatch.setattr(
        planning_close.queue_view, "read_advisory_hold", lambda *_args: AdvisoryHold()
    )
    return install


def test_docs_task_closes_its_planning_issue(fake: Callable[..., _GitHub]) -> None:
    fake()
    assert planning_close.find(TASK) == PlanningClose(TASK, PLANNING, None)


@pytest.mark.parametrize("login", ["app/vergil-user", "vergil-user[bot]"])
def test_app_prefixed_author_is_trusted(fake: Callable[..., _GitHub], login: str) -> None:
    agent = {"__typename": "Bot", "login": login}
    fake(views={7: {"state": "OPEN", "comments": [_docs_marker(author=agent)]}})
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


@pytest.mark.parametrize(
    ("author", "login", "cause"),
    [
        (trust_stubs.OUTSIDER, "drive-by", "untrusted"),
        (trust_stubs.IMPOSTOR, "vergil-user", "wrong-type"),
    ],
    ids=["outsider", "user-named-like-app"],
)
def test_untrusted_docs_marker_is_ignored(
    fake: Callable[..., _GitHub],
    capsys: pytest.CaptureFixture[str],
    author: Any,
    login: str,
    cause: str,
) -> None:
    fake(views={7: {"state": "OPEN", "comments": [_docs_marker(author=author)]}})
    assert planning_close.find(TASK) is None
    out, err = capsys.readouterr()
    assert err == (
        f"skipped untrusted content: acme/.github#7 comment {login} at {trust_stubs.CREATED}"
        f" on comment 1 ({cause})\n"
    )
    assert "Docs task:" not in out + err


def test_planning_close_marker_edited_by_outsider(
    fake: Callable[..., _GitHub], capsys: pytest.CaptureFixture[str]
) -> None:
    # A trusted Docs task: marker an outsider has edited no longer counts: the
    # task is not the docs task, and the edit is reported, metadata only.
    edited = _docs_marker(editor=trust_stubs.OUTSIDER)
    edited["body"] += f"{trust_stubs.SPAM_TEXT}\n"
    fake(views={7: {"state": "OPEN", "comments": [edited]}})
    assert planning_close.find(TASK) is None
    out, err = capsys.readouterr()
    assert err == (
        f"skipped untrusted content: acme/.github#7 edit drive-by at {trust_stubs.EDITED}"
        " on comment 1 (untrusted)\n"
    )
    assert "Docs task:" not in out + err
    assert "SPAM-MARKER-TEXT" not in out + err


@pytest.mark.parametrize(
    ("author", "cause"),
    [(trust_stubs.OUTSIDER, "untrusted"), (trust_stubs.IMPOSTOR, "wrong-type")],
    ids=["outsider", "user-named-like-app"],
)
def test_untrusted_epic_body_is_a_stop(
    fake: Callable[..., _GitHub], capsys: pytest.CaptureFixture[str], author: Any, cause: str
) -> None:
    # The Planning: line counts only from a trusted epic body: an untrusted
    # one is a stop, metadata only, never "not a documentation task".
    fake(epic_body=f"{trust_stubs.SPAM_TEXT}\nPlanning: {PLANNING.slug}\n", epic_author=author)
    with pytest.raises(trust.UntrustedContentError) as excinfo:
        planning_close.find(TASK)
    assert excinfo.value.item.number == EPIC.number
    assert excinfo.value.item.field == "body"
    assert excinfo.value.item.cause == cause
    out, err = capsys.readouterr()
    text = str(excinfo.value) + out + err
    assert "SPAM-MARKER-TEXT" not in text
    assert "Planning:" not in text


@pytest.mark.parametrize(
    ("author", "editor", "cause"),
    [
        (trust_stubs.OUTSIDER, None, "untrusted"),
        (trust_stubs.IMPOSTOR, None, "wrong-type"),
        (trust_stubs.HUMAN, trust_stubs.OUTSIDER, "untrusted"),
    ],
    ids=["outsider", "user-named-like-app", "edited-by-outsider"],
)
def test_untrusted_planning_body_closes_nothing(
    fake: Callable[..., _GitHub],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    author: Any,
    editor: Any,
    cause: str,
) -> None:
    # The advisory hold is read from the planning body only when it is trusted:
    # an untrusted body closes nothing, and the hold rule never sees it.
    body = f"{trust_stubs.SPAM_TEXT}\n\nSecurity advisory: acme/.github GHSA-aaaa-aaaa-aaaa"
    view = {
        "state": "OPEN",
        "body": body,
        "author": author,
        "editor": editor,
        "comments": [_docs_marker()],
    }
    fake(views={7: view})

    def hold(*_args: object) -> AdvisoryHold:
        raise AssertionError("an untrusted body's hold is never read")

    monkeypatch.setattr(planning_close.queue_view, "read_advisory_hold", hold)
    result = planning_close.find(TASK)
    assert result == PlanningClose(
        TASK,
        PLANNING,
        f"its body is untrusted ({cause}), so its advisory hold cannot be read; check it"
        " and close the planning issue by hand",
    )
    assert not result.closes
    out, err = capsys.readouterr()
    assert "SPAM-MARKER-TEXT" not in out + err
    assert "GHSA-aaaa" not in out + err


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


_RETARGET_HINT = (
    "retarget each to the task or tasks that fix it with vrg-security-record retarget "
    "<GHSA> --from 'acme/.github#7' --to <task>, then close the planning issue by hand"
)


def test_planning_issue_held_by_an_advisory_is_skipped(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "Neutral body.\n\nSecurity advisory: acme/.github GHSA-aaaa-aaaa-aaaa"
    labels = [{"name": "security"}, {"name": "in-progress"}]
    fake(views={7: {"state": "OPEN", "body": body, "labels": labels, "comments": [_docs_marker()]}})
    seen: list[tuple[IssueRef, str, set[str]]] = []

    def hold(ref: IssueRef, issue_body: str, issue_labels: set[str]) -> AdvisoryHold:
        seen.append((ref, issue_body, issue_labels))
        return AdvisoryHold(tracking=("GHSA-aaaa-aaaa-aaaa", "GHSA-bbbb-bbbb-bbbb"))

    monkeypatch.setattr(planning_close.queue_view, "read_advisory_hold", hold)
    result = planning_close.find(TASK)
    assert seen == [(PLANNING, body, {"security", "in-progress"})]
    assert result is not None
    assert result.skipped == (
        "2 advisories still track it (GHSA-aaaa-aaaa-aaaa, GHSA-bbbb-bbbb-bbbb) — " + _RETARGET_HINT
    )


def _with_listing(monkeypatch: pytest.MonkeyPatch, advisories: list[Advisory]) -> None:
    """Run the real hold rule against *advisories*, the org's whole listing."""
    monkeypatch.setattr(planning_close.queue_view, "read_advisory_hold", _REAL_READ_HOLD)
    monkeypatch.setattr(queue_view.security_records, "advisory_home", lambda _org: "acme/.github")
    monkeypatch.setattr(queue_view.security_records, "list_advisories", lambda _home: advisories)


_REAL_READ_HOLD = queue_view.read_advisory_hold
_UNREADABLE_BODY = "Neutral body.\n\nSecurity advisory: acme/.github GHSA-cccc-cccc-cccc"


def test_planning_close_skips_unreadable(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review Focus 5: a labelled planning issue names an advisory the listing
    # does not return; the docs PR does not close it.
    view = {
        "state": "OPEN",
        "body": _UNREADABLE_BODY,
        "labels": [{"name": "security"}],
        "comments": [_docs_marker()],
    }
    fake(views={7: view})
    _with_listing(monkeypatch, [])
    result = planning_close.find(TASK)
    assert result is not None
    assert result.skipped == (
        "1 advisory it names cannot be read (GHSA-cccc-cccc-cccc): re-run with a token that "
        "can read the repository's advisories, or have the human check it — " + _RETARGET_HINT
    )


def test_planning_close_unlabelled_unreadable_line_is_not_held(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same body without the label is a broken pair, which does not hold.
    view = {"state": "OPEN", "body": _UNREADABLE_BODY, "labels": [], "comments": [_docs_marker()]}
    fake(views={7: view})
    _with_listing(monkeypatch, [])
    assert planning_close.find(TASK) == PlanningClose(TASK, PLANNING, None)


def test_planning_close_holds_on_a_tracking_line_alone(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake(views={7: {"state": "OPEN", "body": "Neutral.", "comments": [_docs_marker()]}})
    tracking = Advisory(
        ghsa_id="GHSA-dddd-dddd-dddd",
        state="draft",
        affected=None,
        tracking=(PLANNING,),
        external=False,
    )
    monkeypatch.setattr(queue_view.github, "repo_exists", lambda _org, _repo: True)
    _with_listing(monkeypatch, [tracking])
    result = planning_close.find(TASK)
    assert result is not None
    assert result.skipped == (
        "1 advisory still tracks it (GHSA-dddd-dddd-dddd) — " + _RETARGET_HINT
    )


def test_cross_repo_planning_issue_is_skipped(fake: Callable[..., _GitHub]) -> None:
    task = IssueRef("acme", "other-repo", 20)
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

    def boom(*_args: str) -> dict[str, Any]:
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(planning_close.github, "read_json", boom)
    with pytest.raises(subprocess.CalledProcessError):
        planning_close.find(TASK)


def test_failed_layer_read_propagates(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake()

    def boom(*_args: str, **_variables: Any) -> dict[str, Any]:
        raise RuntimeError("gh failed")

    monkeypatch.setattr(planning_close.github, "graphql", boom)
    with pytest.raises(trust.TrustReadError, match=r"read_issue acme/\.github#19"):
        planning_close.find(TASK)


def test_failed_trusted_author_load_propagates(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake()

    def boom() -> None:
        raise TrustedAuthorsNotConfiguredError("not configured")

    monkeypatch.setattr(trust, "load_trusted_authors", boom)
    with pytest.raises(TrustedAuthorsNotConfiguredError):
        planning_close.find(TASK)


def test_malformed_planning_view_is_refused(
    fake: Callable[..., _GitHub], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake()
    monkeypatch.setattr(planning_close.github, "read_json", lambda *_args: [])
    with pytest.raises(
        RuntimeError, match=r"unexpected 'gh issue view' response for acme/\.github#7"
    ):
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


# -- a docs task and a planning issue in continuations (epic .github#1609) --------


def _fill(graph: FakeSubIssueGraph, head: IssueRef, c2: IssueRef, first: int) -> None:
    """Link 99 items into *head*, then *c2* into its last slot."""
    for number in range(first, first + epic_chain.ITEM_CAP):
        graph.link(head, graph.add_issue(IssueRef("acme", "tool", number), title="an item"))
    graph.add_continuation(c2, head, head)


def test_planning_close_reads_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # The docs task sits in its epic's continuation, and the planning issue in
    # the planning queue's: the epic is the head, whose body names the
    # planning issue, and the planning issue is still in the planning queue.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(
        EPIC,
        title="Epic: big",
        labels=["epic", "epic-development"],
        body=f"Planning: {PLANNING.slug}\n\nThe epic.",
    )
    graph.add_issue(
        QUEUE, title="Epic (planning): acme", labels=["epic", "planning", "epic-management"]
    )
    _fill(graph, EPIC, IssueRef("acme", ".github", 30), 1000)
    _fill(graph, QUEUE, IssueRef("acme", ".github", 31), 2000)
    graph.add_issue(TASK, title="Docs task")
    graph.link(IssueRef("acme", ".github", 30), TASK)
    graph.add_issue(PLANNING, title="Plan it")
    graph.link(IssueRef("acme", ".github", 31), PLANNING)
    graph.layer.items[(f"{PLANNING.owner}/{PLANNING.repo}", PLANNING.number)] = trust_stubs.item(
        body="", nodes=_numbered([_docs_marker()])
    )
    monkeypatch.setattr(
        planning_close.queue_view, "read_advisory_hold", lambda *_args: AdvisoryHold()
    )
    graph.clear_log()
    assert planning_close.find(TASK) == PlanningClose(TASK, PLANNING, None)
    assert graph.events == []
