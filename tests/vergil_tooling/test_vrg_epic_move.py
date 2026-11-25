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


@pytest.fixture(autouse=True)
def issue_state() -> Iterator[MagicMock]:
    """Every target epic is open unless a test says otherwise (epic .github#1609)."""
    with patch("vergil_tooling.lib.epics._issue_state", return_value="OPEN") as mock_state:
        yield mock_state


def test_parse_args_requires_task_and_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "adhoc"])  # missing --task


def test_main_reparents_in_one_call() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=OLD),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
        patch(f"{_MOD}.epics.reparent_child") as mock_reparent,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_reparent.assert_called_once_with(NEW, TASK)
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


# -- Atomic move and closed target, at the GraphQL layer (epic .github#1609) --


def _run_move(mock_graphql: MagicMock, *, parent: epics.IssueRef | None) -> int:
    """Move TASK to NEW with only the GraphQL call (and node-id reads) faked."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=parent),
        patch("vergil_tooling.lib.epics._node_id", side_effect=lambda ref: f"node-{ref.number}"),
        patch("vergil_tooling.lib.epics.github.graphql", mock_graphql),
    ):
        return main(["--task", "#42", "--epic", "org/repo#200"])


def test_move_uses_one_reparent() -> None:
    """A task under A moved to B is one addSubIssue with replaceParent, no remove."""
    mock_graphql = MagicMock()
    rc = _run_move(mock_graphql, parent=OLD)
    assert rc == 0
    assert mock_graphql.call_count == 1
    query = mock_graphql.call_args.args[0]
    assert "addSubIssue" in query
    assert "replaceParent: true" in query
    assert "removeSubIssue" not in query
    assert mock_graphql.call_args.kwargs == {"parent": "node-200", "child": "node-42"}


def test_failed_move_leaves_task_in_place(capsys: pytest.CaptureFixture[str]) -> None:
    """A failed re-parent exits 1 with the error, and nothing was unlinked first."""
    mock_graphql = MagicMock(side_effect=RuntimeError("GraphQL errors: boom"))
    rc = _run_move(mock_graphql, parent=OLD)
    assert rc == 1
    assert capsys.readouterr().err == (
        "vrg-epic-move: moving org/repo#42 under epic org/repo#200 failed: GraphQL errors: boom\n"
    )
    assert mock_graphql.call_count == 1
    assert all("removeSubIssue" not in call.args[0] for call in mock_graphql.call_args_list)


def test_failed_move_missing_token_keeps_cli_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing token during the move gets cli_main's configuration message."""
    exc = github.MissingGitHubTokenError(1, ["gh"], stderr="GH_TOKEN environment variable")
    with pytest.raises(SystemExit) as excinfo:
        _run_move(MagicMock(side_effect=exc), parent=OLD)
    assert excinfo.value.code == 1
    assert capsys.readouterr().err.startswith("vrg-epic-move: no GitHub token available.")


def test_move_unparented_task_links_it() -> None:
    """A task with no parent is linked with add_child (a plain addSubIssue)."""
    mock_graphql = MagicMock()
    with patch(f"{_MOD}.epics.add_child", wraps=epics.add_child) as mock_add:
        rc = _run_move(mock_graphql, parent=None)
    assert rc == 0
    mock_add.assert_called_once_with(NEW, TASK)
    assert mock_graphql.call_count == 1
    query = mock_graphql.call_args.args[0]
    assert "addSubIssue" in query
    assert "replaceParent" not in query


def test_move_into_closed_epic_refused(
    issue_state: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """A closed target is refused by name before any write; the task stays under A."""
    issue_state.side_effect = lambda ref: "CLOSED" if ref == NEW else "OPEN"
    mock_graphql = MagicMock()
    # ClosedEpicError reaches cli_main, which prints one line and exits 1.
    with pytest.raises(SystemExit) as excinfo:
        _run_move(mock_graphql, parent=OLD)
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err == f"vrg-epic-move: {epics.ClosedEpicError(NEW)}\n"
    assert "org/repo#200" in err
    assert "Traceback" not in err
    mock_graphql.assert_not_called()


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


def test_main_unconfirmed_standing_epic_exits_1_naming_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A standing epic created but not confirmed is a clean refusal, not a traceback.
    message = "created ad-hoc epic org/.github#10 but org/.github also has org/.github#9"
    exc = epics.StandingEpicDuplicateError(
        message, epics.IssueRef("org", ".github", 10), [epics.IssueRef("org", ".github", 9)]
    )
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", side_effect=exc),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "adhoc"])
    assert rc == 1
    assert capsys.readouterr().err == f"vrg-epic-move: {message}\n"
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


def test_main_bare_epic_resolves_against_task_epic_home() -> None:
    """A bare '--epic #N' reads against the task's epic home, not the cwd repo (#3411).

    The existence check behind the epic-home lookup runs under the task
    owner's App token, never the cwd org's.
    """
    seen: list[tuple[str, str, str | None]] = []
    expected_epic = epics.IssueRef("org", ".github", 407)

    def exists(owner: str, name: str) -> bool:
        seen.append((owner, name, github._target_org.get()))
        return True

    task = epics.IssueRef("org", "lab", 123)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.repo_exists", side_effect=exists),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic"}) as mock_labels,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "org/lab#123", "--epic", "#407"])
    assert rc == 0
    mock_labels.assert_any_call(expected_epic)
    mock_add.assert_called_once_with(expected_epic, task)
    assert seen == [("org", ".github", "org")]


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


@pytest.mark.parametrize("epic_sentinel", ["adhoc", "intake", "planning", "operations"])
def test_main_accepts_every_standing_sentinel(epic_sentinel: str) -> None:
    """Every standing sentinel passes the cross-org guard and resolves (#3514)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW) as mock_resolve,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", epic_sentinel])
    assert rc == 0
    mock_resolve.assert_called_once_with(epic_sentinel, repo="org/repo")
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
    for epic_sentinel in ("adhoc", "intake", "planning"):
        assert f"'{epic_sentinel}'" in out


def test_epic_help_lists_hoshin(capsys: pytest.CaptureFixture[str]) -> None:
    """The --epic help lists the hoshin sentinel (epic vergil-project/.github#1417)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "'hoshin'" in out


def test_epic_operations_sentinel_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    """--epic operations is listed and resolves the org's operations epic (#1494)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    assert "'operations'" in " ".join(capsys.readouterr().out.split())
    ops = epics.IssueRef("org", ".github", 31)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch("vergil_tooling.lib.epics.ensure_operations_epic", return_value=ops) as mock_ensure,
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "operations"])
    assert rc == 0
    mock_ensure.assert_called_once_with("org/repo")
    mock_add.assert_called_once_with(ops, TASK)


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
        patch(f"{_MOD}.epics.reparent_child") as mock_reparent,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_reparent.assert_called_once_with(NEW, TASK)
    mock_remove.assert_not_called()
