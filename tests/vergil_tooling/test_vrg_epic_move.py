"""Tests for vergil_tooling.bin.vrg_epic_move."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_epic_move import main, parse_args
from vergil_tooling.lib import epics, github

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_epic_move"

OLD = epics.IssueRef("org", "repo", 100)
NEW = epics.IssueRef("org", "repo", 200)
TASK = epics.IssueRef("org", "repo", 42)


@pytest.fixture(autouse=True)
def queue_kind() -> Iterator[MagicMock]:
    """Every epic is a non-deferred epic unless a test says otherwise (#3673)."""
    with patch(f"{_MOD}.epics.queue_kind", return_value=None) as mock_kind:
        yield mock_kind


def test_parse_args_requires_task_and_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "adhoc"])  # missing --task


def test_main_reparents_unlink_then_link() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=OLD),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_remove.assert_called_once_with(OLD, TASK)
    mock_add.assert_called_once_with(NEW, TASK)


def test_main_idempotent_when_already_under_target() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=NEW),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


def test_main_links_when_task_has_no_parent() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_remove.assert_not_called()
    mock_add.assert_called_once_with(NEW, TASK)


def test_main_rejects_non_epic_target() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", side_effect=ValueError("not an epic")),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "#999"])
    assert rc == 1
    mock_add.assert_not_called()


def test_main_accepts_adhoc_epic() -> None:
    """The 'adhoc' sentinel skips the cross-org guard (it resolves in .github)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "adhoc"])
    assert rc == 0
    mock_add.assert_called_once_with(NEW, TASK)


@pytest.mark.parametrize(
    ("task_ref", "task_repo"),
    [
        ("other-org/proj#49", "other-org/proj"),  # different org from cwd
        ("cwd-org/other#49", "cwd-org/other"),  # same org, different repo
    ],
)
def test_main_adhoc_resolves_against_task_repo_not_cwd(task_ref: str, task_repo: str) -> None:
    """'--epic adhoc' targets the task repo's ad-hoc epic, not the cwd repo's (#3409)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/tooling"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW) as mock_resolve,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--task", task_ref, "--epic", "adhoc"])
    assert rc == 0
    mock_resolve.assert_called_once_with("adhoc", repo=task_repo)


@pytest.mark.parametrize(
    ("task_ref", "public", "expected_epic"),
    [
        # Public task: its epics home in <org>/.github.
        ("org/tooling#123", {"org/tooling", "org/.github"}, epics.IssueRef("org", ".github", 407)),
        # Private task with a public .github: its epics home in the repo itself.
        ("org/lab#123", {"org/.github"}, epics.IssueRef("org", "lab", 407)),
    ],
)
def test_main_bare_epic_resolves_against_task_epic_home(
    task_ref: str, public: set[str], expected_epic: epics.IssueRef
) -> None:
    """A bare '--epic #N' reads against the task's epic home, not the cwd repo (#3411).

    The visibility probes behind the epic-home lookup run under the task
    owner's App token, never the cwd org's.
    """
    seen_orgs: list[str | None] = []

    def is_public(nwo: str) -> bool:
        seen_orgs.append(github._target_org.get())
        return nwo in public

    task = epics.parse_issue_ref(task_ref, default_repo="unused/unused")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.is_public", side_effect=is_public),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True) as mock_is_epic,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", task_ref, "--epic", "#407"])
    assert rc == 0
    mock_is_epic.assert_called_once_with(expected_epic)
    mock_add.assert_called_once_with(expected_epic, task)
    assert seen_orgs
    assert set(seen_orgs) == {"org"}


def test_main_bare_epic_with_cross_org_task_is_not_rejected() -> None:
    """A bare epic ref homes in the task's org, so it never trips the cross-org guard."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW) as mock_resolve,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--task", "other-org/proj#49", "--epic", "#407"])
    assert rc == 0
    mock_resolve.assert_called_once_with("#407", repo="other-org/proj")


def test_main_scopes_token_to_task_owner() -> None:
    """The re-parent runs under the task's owner installation, not cwd (#2070)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/repo"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--task", "other-org/repo#42", "--epic", "other-org/repo#200"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_rejects_cross_org_task_and_epic() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "org-a/repo#42", "--epic", "org-b/repo#200"])
    assert rc == 1
    mock_resolve.assert_not_called()
    mock_add.assert_not_called()


def test_main_reports_missing_installation() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(
            f"{_MOD}.epics.add_child",
            side_effect=github.NoInstallationError("org", []),
        ),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 1


@pytest.mark.parametrize("sentinel", ["adhoc", "intake", "planning"])
def test_main_accepts_every_standing_sentinel(sentinel: str) -> None:
    """Every standing sentinel passes the cross-org guard and resolves (#3514)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW) as mock_resolve,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", sentinel])
    assert rc == 0
    mock_resolve.assert_called_once_with(sentinel, repo="org/repo")
    mock_add.assert_called_once_with(NEW, TASK)


def test_main_planning_ensures_in_progress_label_in_task_repo() -> None:
    """Moving a member-repo task into planning ensures 'in-progress' there (#3520)."""
    tool_task = epics.IssueRef("org", "tool", 36)
    planning = epics.IssueRef("org", ".github", 439)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._find_epic_by_title", return_value=planning),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "org/tool#36", "--epic", "planning"])
    assert rc == 0
    mock_ensure.assert_any_call("org/tool", ("in-progress",))
    mock_add.assert_called_once_with(planning, tool_task)


def test_epic_help_names_every_standing_sentinel(capsys: pytest.CaptureFixture[str]) -> None:
    """The --epic help lists adhoc, intake and planning (#3512)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    for sentinel in ("adhoc", "intake", "planning"):
        assert f"'{sentinel}'" in out


# -- Deferred queue: entered and left only through vrg-intake-route (#3673) ---


def test_epic_move_refuses_deferred_sentinel(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
        patch(f"{_MOD}.epics.parent_of", return_value=OLD),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "deferred"])
    assert rc == 1
    assert "vrg-intake-route" in capsys.readouterr().err
    mock_resolve.assert_not_called()  # the deferred epic is never created
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


def test_epic_move_refuses_deferred_epic_number(
    queue_kind: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_kind.side_effect = lambda epic: "deferred" if epic == NEW else None
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=OLD),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "vrg-intake-route" in err
    assert "org/repo#200" in err
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


def test_epic_move_refuses_task_under_deferred_epic(
    queue_kind: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_kind.side_effect = lambda epic: "deferred" if epic == OLD else None
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=OLD),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "vrg-intake-route" in err
    assert "org/repo#100" in err
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


def test_epic_move_moves_task_out_of_intake(queue_kind: MagicMock) -> None:
    """Only the deferred queue is route-only; an intake parent still moves."""
    queue_kind.side_effect = lambda epic: "intake" if epic == OLD else None
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=OLD),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_remove.assert_called_once_with(OLD, TASK)
    mock_add.assert_called_once_with(NEW, TASK)
