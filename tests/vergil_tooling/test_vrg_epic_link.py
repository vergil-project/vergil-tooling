"""Tests for vergil_tooling.bin.vrg_epic_link."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin.vrg_epic_link import main, parse_args
from vergil_tooling.lib import epic_chain, epics, github

if TYPE_CHECKING:
    from collections.abc import Generator

_MOD = "vergil_tooling.bin.vrg_epic_link"
_REAL_LABELS = epics._labels
_REAL_CONTAINER_PARENT = epics._container_parent
_EPIC = epics.IssueRef("org", ".github", 40)
_TASK = epics.IssueRef("org", "repo", 42)


@pytest.fixture(autouse=True)
def _task_is_a_task() -> Generator[MagicMock]:
    """Every --task is a plain task unless a test says otherwise (no gh label read)."""
    with patch(f"{_MOD}.epics.non_task_kind", return_value=None) as mock_kind:
        yield mock_kind


@pytest.fixture(autouse=True)
def _epic_labels() -> Generator[MagicMock]:
    """Every --epic is a plain epic head unless a test says otherwise (no gh label read)."""
    with patch("vergil_tooling.lib.epics._labels", return_value={"epic"}) as mock_labels:
        yield mock_labels


@pytest.fixture(autouse=True)
def _epic_is_top_level() -> Generator[MagicMock]:
    """Every --epic is a top-level issue unless a test says otherwise (no gh parent read)."""
    with patch("vergil_tooling.lib.epics._container_parent", return_value=None) as mock_parent:
        yield mock_parent


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    """The sub-issue graph, holding the epic ``org/.github#40`` and the task ``org/repo#42``."""
    fake = FakeSubIssueGraph().install(monkeypatch)
    fake.add_issue(_EPIC, title="Epic: big", labels=["epic", "epic-development"])
    fake.add_issue(_TASK, title="a task")
    return fake


def _fill(graph: FakeSubIssueGraph, head: epics.IssueRef, count: int) -> None:
    """Link *count* new tasks under *head*."""
    for number in range(1000, 1000 + count):
        graph.link(head, graph.add_issue(epics.IssueRef("org", "repo-a", number), title="a task"))


def _assert_linked(graph: FakeSubIssueGraph, epic: epics.IssueRef, task: epics.IssueRef) -> None:
    """*task* is in *epic* and nowhere else, by one link."""
    assert graph.parent_of(task) == epic
    holders = [issue.ref for issue in graph.issues.values() if task in issue.children]
    assert holders == [epic]
    assert graph.mutations == [("add", epic, task)]


def test_main_refuses_an_epic_ref_that_is_not_an_epic(
    graph: FakeSubIssueGraph, _epic_labels: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _epic_labels.return_value = set()
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1
    assert "org/.github#40 is not an epic (missing the 'epic' label)" in capsys.readouterr().err
    assert graph.mutations == []
    assert graph.parent_of(_TASK) is None


def test_parse_args_requires_epic_and_task() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "org/.github#40"])  # missing --task


def test_main_links_task_under_epic(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 0
    _assert_linked(graph, _EPIC, _TASK)
    assert capsys.readouterr().out == "Linked org/repo#42 under epic org/.github#40.\n"


def test_main_bare_epic_resolves_against_task_epic_home(graph: FakeSubIssueGraph) -> None:
    """A bare '--epic #N' reads against the task's epic home, not the cwd repo (#3411).

    The home's existence check runs under the task owner's App token.
    """
    epic = graph.add_issue(epics.IssueRef("org", ".github", 407), title="Epic: e", labels=["epic"])
    task = graph.add_issue(epics.IssueRef("org", "lab", 9), title="a task")
    seen: list[tuple[str, str, str | None]] = []

    def exists(owner: str, name: str) -> bool:
        seen.append((owner, name, github._target_org.get()))
        return True

    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.repo_exists", side_effect=exists),
    ):
        rc = main(["--epic", "#407", "--task", "org/lab#9"])
    assert rc == 0
    _assert_linked(graph, epic, task)
    assert seen == [("org", ".github", "org")]


def test_main_bare_epic_and_bare_task_use_cwd_repo_as_task(graph: FakeSubIssueGraph) -> None:
    """A bare task stays cwd-relative; the bare epic follows that task's home."""
    epic = graph.add_issue(epics.IssueRef("org", ".github", 407), title="Epic: e", labels=["epic"])
    task = graph.add_issue(epics.IssueRef("org", "tooling", 9), title="a task")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch("vergil_tooling.lib.github.repo_exists", return_value=True),
    ):
        rc = main(["--epic", "#407", "--task", "#9"])
    assert rc == 0
    _assert_linked(graph, epic, task)


def test_main_rejects_bad_ref(graph: FakeSubIssueGraph) -> None:
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", "not-a-ref", "--task", "#42"])
    assert rc == 1
    assert graph.mutations == []


def test_main_scopes_token_to_target_owner(graph: FakeSubIssueGraph) -> None:
    """The link runs under the epic/task owner's App installation, not the
    cwd org's (#2070)."""
    epic = graph.add_issue(epics.IssueRef("other-org", ".github", 40), title="E", labels=["epic"])
    task = graph.add_issue(epics.IssueRef("other-org", "repo", 42), title="a task")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/repo"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
    ):
        rc = main(["--epic", "other-org/.github#40", "--task", "other-org/repo#42"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")
    _assert_linked(graph, epic, task)


def test_main_rejects_cross_org_refs(graph: FakeSubIssueGraph) -> None:
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", "org-a/.github#40", "--task", "org-b/repo#42"])
    assert rc == 1
    assert graph.mutations == []


def test_main_reports_missing_installation(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The link's own write finds no App installation: reported, exit 1.
    answer = graph.graphql

    def no_installation(query: str, **variables: Any) -> dict[str, Any]:
        if query == epics._ADD_SUBISSUE:
            raise github.NoInstallationError("org", [])
        return answer(query, **variables)

    monkeypatch.setattr(github, "graphql", no_installation)
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1
    assert capsys.readouterr().err.startswith("vrg-epic-link: ")
    assert graph.mutations == []
    assert graph.parent_of(_TASK) is None


@pytest.mark.parametrize("kind", ["epic", "archive"])
def test_refuses_epic_or_archive_as_task(
    graph: FakeSubIssueGraph,
    kind: str,
    _task_is_a_task: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #2842: --task must be a task; an epic or archive is refused before any link.
    _task_is_a_task.return_value = kind
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"vrg-epic-link: --task (org/repo#42) is an {kind}, not a task" in err
    _task_is_a_task.assert_called_once_with(_TASK)
    assert graph.mutations == []


def test_task_label_lookup_failure_propagates(
    graph: FakeSubIssueGraph, _task_is_a_task: MagicMock
) -> None:
    # Fail closed: an unreadable --task is never linked as though it were a task.
    _task_is_a_task.side_effect = subprocess.CalledProcessError(1, ["gh"])
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        pytest.raises(subprocess.CalledProcessError),
    ):
        main(["--epic", "org/.github#40", "--task", "#42"])
    assert graph.mutations == []


def test_epic_link_continuation_uses_head(
    _epic_labels: MagicMock,
    _epic_is_top_level: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A continuation's ref (copied from GitHub's UI) means its chain's head.
    _epic_labels.side_effect = _REAL_LABELS
    _epic_is_top_level.side_effect = _REAL_CONTAINER_PARENT
    graph = FakeSubIssueGraph().install(monkeypatch)
    head = graph.add_issue(epics.IssueRef("org", ".github", 40), title="Epic: big", labels=["epic"])
    continuation = graph.add_continuation(epics.IssueRef("org", ".github", 50), head, head)
    task = graph.add_issue(epics.IssueRef("org", "repo", 42), title="a task")
    graph.clear_log()
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", continuation.slug, "--task", "#42"])
    assert rc == 0
    assert graph.mutations == [("add", head, task)]
    assert graph.parent_of(task) == head
    captured = capsys.readouterr()
    assert "org/.github#50 is a continuation of org/.github#40; using org/.github#40" in (
        captured.err
    )
    assert "Linked org/repo#42 under epic org/.github#40." in captured.out


def test_epic_link_into_full_epic_rolls_over(
    graph: FakeSubIssueGraph,
    _epic_labels: MagicMock,
    _epic_is_top_level: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _epic_labels.side_effect = _REAL_LABELS
    _epic_is_top_level.side_effect = _REAL_CONTAINER_PARENT
    _fill(graph, _EPIC, epic_chain.ITEM_CAP)
    graph.clear_log()
    with patch(f"{_MOD}.github.current_repo", return_value="org/repo"):
        rc = main(["--epic", _EPIC.slug, "--task", "#42"])
    assert rc == 0
    (c2,) = [event[1] for event in graph.events if event[0] == "create"]
    assert graph.issues[c2].labels == ["epic", epics.CONTINUATION_LABEL]
    assert graph.parent_of(c2) == _EPIC
    assert graph.children_of(_EPIC)[-1] == c2
    assert len(graph.children_of(_EPIC)) == epic_chain.SUBISSUE_LIMIT
    assert graph.parent_of(_TASK) == c2
    assert graph.children_of(c2) == [_TASK]
    assert [m for m in graph.mutations if m[2] == _TASK] == [("add", c2, _TASK)]
    out = capsys.readouterr().out
    assert out.endswith(
        f"Linked {_TASK.slug} under epic {_EPIC.slug} (in continuation {c2.slug}).\n"
    )
