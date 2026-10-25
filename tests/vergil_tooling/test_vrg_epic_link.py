"""Tests for vergil_tooling.bin.vrg_epic_link."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_epic_link import main, parse_args
from vergil_tooling.lib import epics, github

if TYPE_CHECKING:
    from collections.abc import Generator

_MOD = "vergil_tooling.bin.vrg_epic_link"


@pytest.fixture(autouse=True)
def _task_is_a_task() -> Generator[MagicMock]:
    """Every --task is a plain task unless a test says otherwise (no gh label read)."""
    with patch(f"{_MOD}.epics.non_task_kind", return_value=None) as mock_kind:
        yield mock_kind


def test_parse_args_requires_epic_and_task() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "org/.github#40"])  # missing --task


def test_main_links_task_under_epic() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.is_public", return_value=True),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 0
    mock_add.assert_called_once_with(
        epics.IssueRef("org", ".github", 40),
        epics.IssueRef("org", "repo", 42),
    )


@pytest.mark.parametrize(
    ("task_ref", "public", "expected_epic"),
    [
        # Public task: its epics home in <org>/.github.
        ("org/tooling#9", {"org/tooling", "org/.github"}, epics.IssueRef("org", ".github", 407)),
        # Private task with a public .github: its epics home in the repo itself.
        ("org/lab#9", {"org/.github"}, epics.IssueRef("org", "lab", 407)),
    ],
)
def test_main_bare_epic_resolves_against_task_epic_home(
    task_ref: str, public: set[str], expected_epic: epics.IssueRef
) -> None:
    """A bare '--epic #N' reads against the task's epic home, not the cwd repo (#3411).

    Every visibility probe runs under the task owner's App token.
    """
    seen_orgs: list[str | None] = []

    def is_public(nwo: str) -> bool:
        seen_orgs.append(github._target_org.get())
        return nwo in public

    task = epics.parse_issue_ref(task_ref, default_repo="unused/unused")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.is_public", side_effect=is_public),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "#407", "--task", task_ref])
    assert rc == 0
    mock_add.assert_called_once_with(expected_epic, task)
    assert seen_orgs
    assert set(seen_orgs) == {"org"}


def test_main_bare_epic_and_bare_task_use_cwd_repo_as_task() -> None:
    """A bare task stays cwd-relative; the bare epic follows that task's home."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch("vergil_tooling.lib.github.is_public", return_value=True),
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
        patch(f"{_MOD}.github.is_public", return_value=True),
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
        patch(f"{_MOD}.github.is_public", return_value=True),
        patch(
            f"{_MOD}.epics.add_child",
            side_effect=github.NoInstallationError("org", []),
        ),
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1


def test_refuses_public_task_under_private_epic(capsys: pytest.CaptureFixture[str]) -> None:
    # A public task must not be a native sub-issue of a private epic: it would
    # leak the private repo's name and break cross-boundary roll-up.
    def pub(nwo: str) -> bool:
        return {"org/tooling": True, "org/lab": False}[nwo]

    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.github.is_public", side_effect=pub),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/lab#5", "--task", "org/tooling#9"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to link public task" in err
    assert "Blocked-by" in err
    mock_add.assert_not_called()


def test_allows_private_task_under_public_epic() -> None:
    # The reverse is fine: a private child under a public parent doesn't leak and
    # roll-up reads the public parent.
    def pub(nwo: str) -> bool:
        return {"org/.github": True, "org/lab": False}[nwo]

    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/lab"),
        patch(f"{_MOD}.github.is_public", side_effect=pub),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/.github#5", "--task", "org/lab#9"])
    assert rc == 0
    mock_add.assert_called_once()


@pytest.mark.parametrize("kind", ["epic", "archive"])
def test_refuses_epic_or_archive_as_task(
    kind: str, _task_is_a_task: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    # #2842: --task must be a task; an epic or archive is refused before any link.
    _task_is_a_task.return_value = kind
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.is_public", return_value=True),
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
