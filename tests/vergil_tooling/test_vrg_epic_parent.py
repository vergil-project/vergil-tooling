"""Tests for ``vrg-epic-parent``: a task's epic and kind, read through the chain."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin.vrg_epic_parent import _as_line, main
from vergil_tooling.lib import epics, github
from vergil_tooling.lib.epics import IssueRef

_MOD = "vergil_tooling.bin.vrg_epic_parent"

HEAD = IssueRef("org", ".github", 40)
C2 = IssueRef("org", ".github", 50)
TASK = IssueRef("org", "repo", 42)


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    return FakeSubIssueGraph().install(monkeypatch)


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _json(graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    graph.clear_log()
    rc, out, _ = _run([TASK.slug, "--json"], capsys)
    assert rc == 0
    assert graph.events == []  # a reader: nothing is written
    return json.loads(out)


def test_task_in_head(graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]) -> None:
    graph.add_issue(HEAD, title="Epic: big", labels=["epic", "epic-development"])
    graph.add_issue(TASK, title="a task")
    graph.link(HEAD, TASK)
    rc, out, _ = _run([TASK.slug], capsys)
    assert (rc, out) == (0, "org/.github#40 development\n")
    assert _json(graph, capsys) == {
        "task": TASK.slug,
        "epic": HEAD.slug,
        "kind": "development",
        "container": HEAD.slug,
    }


def test_task_in_continuation_reports_head(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    graph.add_issue(HEAD, title="Epic (intake): org/repo", labels=["epic", "intake"])
    graph.add_continuation(C2, HEAD, HEAD)
    graph.add_issue(TASK, title="an idea")
    graph.link(C2, TASK)
    rc, out, _ = _run([TASK.slug], capsys)
    assert (rc, out) == (0, "org/.github#40 intake\n")
    assert _json(graph, capsys) == {
        "task": TASK.slug,
        "epic": HEAD.slug,
        "kind": "intake",
        "container": C2.slug,
    }


@pytest.mark.parametrize(
    ("labels", "kind"),
    [
        (["epic", "ad-hoc"], "ad-hoc"),
        (["epic", "planning"], "planning"),
        (["epic", "deferred"], "deferred"),
        (["epic", "hoshin"], "hoshin"),
        (["epic", "operations"], "operations"),
    ],
)
def test_standing_kinds(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str], labels: list[str], kind: str
) -> None:
    graph.add_issue(HEAD, title="Epic: standing", labels=labels)
    graph.add_issue(TASK, title="a task")
    graph.link(HEAD, TASK)
    rc, out, _ = _run([TASK.slug], capsys)
    assert (rc, out) == (0, f"org/.github#40 {kind}\n")


def test_proxy_task_kind(graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]) -> None:
    graph.add_issue(HEAD, title="Epic (proxy): other/.github#7", labels=["epic", "proxy"])
    graph.add_issue(TASK, title="a task")
    graph.link(HEAD, TASK)
    rc, out, _ = _run([TASK.slug], capsys)
    assert (rc, out) == (0, "org/.github#40 proxy\n")


def test_unparented_task(graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]) -> None:
    graph.add_issue(TASK, title="a task")
    rc, out, _ = _run([TASK.slug], capsys)
    assert (rc, out) == (0, "none\n")
    assert _json(graph, capsys) == {
        "task": TASK.slug,
        "epic": None,
        "kind": "none",
        "container": None,
    }


def test_non_epic_parent_is_not_epic(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = graph.add_issue(IssueRef("org", "repo", 30), title="a plain issue", labels=["bug"])
    graph.add_issue(TASK, title="a sub-task")
    graph.link(parent, TASK)
    rc, out, _ = _run([TASK.slug], capsys)
    assert (rc, out) == (0, "org/repo#30 not-epic\n")
    assert _json(graph, capsys) == {
        "task": TASK.slug,
        "epic": None,
        "kind": "not-epic",
        "container": parent.slug,
    }


def test_archive_parent_is_archive(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = graph.add_issue(
        IssueRef("org", ".github", 60),
        title="Archive (intake): org/repo 2026-Q4",
        labels=["archive", "intake"],
    )
    graph.add_issue(TASK, title="a drained idea", state="CLOSED")
    graph.link(archive, TASK)
    rc, out, _ = _run([TASK.slug], capsys)
    assert (rc, out) == (0, "org/.github#60 archive\n")
    assert _json(graph, capsys) == {
        "task": TASK.slug,
        "epic": archive.slug,
        "kind": "archive",
        "container": archive.slug,
    }


def test_chain_drift_exits_1(graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]) -> None:
    graph.add_issue(HEAD, title="Epic (intake): org/repo", labels=["epic", "intake"])
    graph.add_continuation(C2, HEAD, HEAD, author=trust_stubs.OUTSIDER)
    graph.add_issue(TASK, title="an idea")
    graph.link(C2, TASK)
    rc, out, err = _run([TASK.slug], capsys)
    assert (rc, out) == (1, "")
    assert "vrg-epic-parent: chain drift: org/.github#50 breaks the untrusted-body rule" in err


def test_ambiguous_head_kind_exits_1(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two kind markers on one head: no kind is guessed.
    graph.add_issue(HEAD, title="Epic: confused", labels=["epic", "intake", "proxy"])
    graph.add_issue(TASK, title="a task")
    graph.link(HEAD, TASK)
    rc, out, err = _run([TASK.slug], capsys)
    assert (rc, out) == (1, "")
    assert "vrg-epic-parent: " in err
    assert "intake" in err
    assert "proxy" in err


def test_read_failure_exits_1(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph.add_issue(TASK, title="a task")

    def fail(*_args: object, **_kwargs: object) -> object:
        raise github.GitHubAPIError(1, ["gh", "api", "graphql"], output="", stderr="gh: boom\n")

    monkeypatch.setattr(github, "graphql", fail)
    rc, out, err = _run([TASK.slug], capsys)
    assert (rc, out) == (1, "")
    assert err.startswith("vrg-epic-parent: ")
    assert "boom" in err


def test_head_without_epic_label_exits_1(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    # A continuation whose head lost its epic label: the head's kind is never guessed.
    graph.add_issue(HEAD, title="Epic: big", labels=["epic-development"])
    graph.add_continuation(C2, HEAD, HEAD)
    graph.add_issue(TASK, title="a task")
    graph.link(C2, TASK)
    rc, out, err = _run([TASK.slug], capsys)
    assert (rc, out) == (1, "")
    assert "vrg-epic-parent: the chain head org/.github#40 lacks the 'epic' label" in err


def test_head_that_is_a_continuation_raises() -> None:
    with pytest.raises(RuntimeError, match=r"org/\.github#40 is itself a continuation"):
        epics._head_kind(HEAD, {"epic", epics.CONTINUATION_LABEL})


def test_parent_read_without_parent_field_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A read that does not say whether the parent is itself nested is never guessed past.
    parent = {
        "number": 40,
        "repository": {"name": ".github", "owner": {"login": "org"}},
        "labels": {"totalCount": 1, "nodes": [{"name": "epic"}]},
    }
    monkeypatch.setattr(epics, "_node_id", lambda _ref: "I_1")
    monkeypatch.setattr(github, "graphql", lambda *_a, **_k: {"node": {"parent": parent}})
    with pytest.raises(RuntimeError, match="did not say whether org/.github#40 has one"):
        epics.task_epic(TASK)


def test_line_without_a_ref_raises() -> None:
    found = epics.TaskEpic(task=TASK, epic=None, kind="development", container=None)
    with pytest.raises(RuntimeError, match="no ref to print"):
        _as_line(found)


def test_bare_ref_uses_current_repo(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    graph.add_issue(TASK, title="a task")
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo") as current:
        rc, out, _ = _run(["#42"], capsys)
    current.assert_called_once()
    assert (rc, out) == (0, "none\n")


def test_malformed_ref_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = _run(["not-a-ref"], capsys)
    assert (rc, out) == (1, "")
    assert err.startswith("vrg-epic-parent: not an issue ref")


def test_scopes_token_to_task_owner(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    graph.add_issue(TASK, title="a task")
    with patch(f"{_MOD}.github.target_org", wraps=github.target_org) as target:
        rc, _, _ = _run([TASK.slug], capsys)
    assert rc == 0
    target.assert_called_once_with("org")
