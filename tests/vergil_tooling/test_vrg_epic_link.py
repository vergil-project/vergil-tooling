"""Tests for vergil_tooling.bin.vrg_epic_link."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin.vrg_epic_link import main, parse_args
from vergil_tooling.lib import epics, github

if TYPE_CHECKING:
    from collections.abc import Generator

_MOD = "vergil_tooling.bin.vrg_epic_link"
_REAL_LABELS = epics._labels
_REAL_CONTAINER_PARENT = epics._container_parent


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


def test_main_refuses_an_epic_ref_that_is_not_an_epic(
    _epic_labels: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _epic_labels.return_value = set()
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1
    assert "org/.github#40 is not an epic (missing the 'epic' label)" in capsys.readouterr().err
    mock_add.assert_not_called()


def test_parse_args_requires_epic_and_task() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "org/.github#40"])  # missing --task


def test_main_links_task_under_epic() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 0
    mock_add.assert_called_once_with(
        epics.IssueRef("org", ".github", 40),
        epics.IssueRef("org", "repo", 42),
    )


def test_main_bare_epic_resolves_against_task_epic_home() -> None:
    """A bare '--epic #N' reads against the task's epic home, not the cwd repo (#3411).

    The home's existence check runs under the task owner's App token.
    """
    seen: list[tuple[str, str, str | None]] = []

    def exists(owner: str, name: str) -> bool:
        seen.append((owner, name, github._target_org.get()))
        return True

    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.repo_exists", side_effect=exists),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "#407", "--task", "org/lab#9"])
    assert rc == 0
    mock_add.assert_called_once_with(
        epics.IssueRef("org", ".github", 407), epics.IssueRef("org", "lab", 9)
    )
    assert seen == [("org", ".github", "org")]


def test_main_bare_epic_and_bare_task_use_cwd_repo_as_task() -> None:
    """A bare task stays cwd-relative; the bare epic follows that task's home."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch("vergil_tooling.lib.github.repo_exists", return_value=True),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "#407", "--task", "#9"])
    assert rc == 0
    mock_add.assert_called_once_with(
        epics.IssueRef("org", ".github", 407), epics.IssueRef("org", "tooling", 9)
    )


def test_main_rejects_bad_ref() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "not-a-ref", "--task", "#42"])
    assert rc == 1
    mock_add.assert_not_called()


def test_main_scopes_token_to_target_owner() -> None:
    """The link runs under the epic/task owner's App installation, not the
    cwd org's (#2070)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/repo"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "other-org/.github#40", "--task", "other-org/repo#42"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_rejects_cross_org_refs() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org-a/.github#40", "--task", "org-b/repo#42"])
    assert rc == 1
    mock_add.assert_not_called()


def test_main_reports_missing_installation() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.add_child",
            side_effect=github.NoInstallationError("org", []),
        ),
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1


@pytest.mark.parametrize("kind", ["epic", "archive"])
def test_refuses_epic_or_archive_as_task(
    kind: str, _task_is_a_task: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    # #2842: --task must be a task; an epic or archive is refused before any link.
    _task_is_a_task.return_value = kind
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"vrg-epic-link: --task (org/repo#42) is an {kind}, not a task" in err
    _task_is_a_task.assert_called_once_with(epics.IssueRef("org", "repo", 42))
    mock_add.assert_not_called()


def test_task_label_lookup_failure_propagates(_task_is_a_task: MagicMock) -> None:
    # Fail closed: an unreadable --task is never linked as though it were a task.
    _task_is_a_task.side_effect = subprocess.CalledProcessError(1, ["gh"])
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
        pytest.raises(subprocess.CalledProcessError),
    ):
        main(["--epic", "org/.github#40", "--task", "#42"])
    mock_add.assert_not_called()


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
    captured = capsys.readouterr()
    assert "org/.github#50 is a continuation of org/.github#40; using org/.github#40" in (
        captured.err
    )
    assert "Linked org/repo#42 under epic org/.github#40." in captured.out
