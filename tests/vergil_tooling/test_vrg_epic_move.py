"""Tests for vergil_tooling.bin.vrg_epic_move.

The sub-issue graph is the in-memory :class:`FakeSubIssueGraph`: the move goes
through the chain layer (:mod:`vergil_tooling.lib.epic_chain`) for real, and
each test asserts the graph's end state (which container holds the task, and
that it has one parent) and the links written (``graph.mutations``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.bin.vrg_epic_move import main, parse_args
from vergil_tooling.lib import epic_chain, epics, github, trust
from vergil_tooling.lib.release_groups import record
from vergil_tooling.lib.trust import CommentStream, Role, StreamComment

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_epic_move"

OLD = epics.IssueRef("org", "repo", 100)
NEW = epics.IssueRef("org", "repo", 200)
TASK = epics.IssueRef("org", "repo", 42)

_LINK_MUTATIONS = (epics._ADD_SUBISSUE, epics._REPARENT_SUBISSUE, epics._REMOVE_SUBISSUE)

# TASK is a live member of G1 of OLD (epic vergil-project/.github#1672, spec §5.1).
GROUP = record.Group(
    epic=OLD, group="G1", rev=1, members=(TASK,), dissolved=False, comment_id="901"
)
_GROUPS = "vergil_tooling.lib.release_groups.record.groups_of"


@pytest.fixture(autouse=True)
def queue_kind() -> Iterator[MagicMock]:
    """Every epic is a non-deferred epic unless a test says otherwise (#3673)."""
    with patch(f"{_MOD}.epics.queue_kind", return_value=None) as mock_kind:
        yield mock_kind


@pytest.fixture(autouse=True)
def no_release_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK is in no epic, so its release-group read finds no group; a test may stub it."""
    monkeypatch.setattr(
        epics, "task_epic", lambda task: epics.TaskEpic(task, None, epics.NO_PARENT_KIND, None)
    )


@pytest.fixture(autouse=True)
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    """The sub-issue graph: the epics OLD and NEW (both open) and the unlinked TASK."""
    fake = FakeSubIssueGraph().install(monkeypatch)
    fake.add_issue(OLD, title="Epic: old", labels=["epic"])
    fake.add_issue(NEW, title="Epic: new", labels=["epic"])
    fake.add_issue(TASK, title="a task")
    return fake


def _under(graph: FakeSubIssueGraph, parent: epics.IssueRef, task: epics.IssueRef) -> None:
    """Link *task* under *parent* as the starting state, leaving the write log empty."""
    graph.link(parent, task)
    graph.clear_log()


def _fill(graph: FakeSubIssueGraph, head: epics.IssueRef, count: int) -> None:
    """Link *count* plain items under *head*, numbered from 1000."""
    for number in range(1000, 1000 + count):
        graph.link(head, graph.add_issue(epics.IssueRef("org", "repo", number), title="item"))


def _fail_links(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> list[str]:
    """Make every link mutation raise *exc*; returns the mutation queries attempted."""
    attempted: list[str] = []

    def graphql(query: str, **variables: Any) -> dict[str, Any]:
        if query in _LINK_MUTATIONS:
            attempted.append(query)
            raise exc
        return graph.graphql(query, **variables)

    monkeypatch.setattr(github, "graphql", graphql)
    return attempted


def test_parse_args_requires_task_and_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "adhoc"])  # missing --task


def test_main_reparents_in_one_call(graph: FakeSubIssueGraph) -> None:
    _under(graph, OLD, TASK)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    assert graph.mutations == [("reparent", NEW, TASK)]
    assert graph.parent_of(TASK) == NEW
    assert TASK not in graph.children_of(OLD)


# -- Atomic move and closed target, at the GraphQL layer (epic .github#1609) --


def _run_move() -> int:
    """Move TASK to NEW with only the target's resolution faked."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        return main(["--task", "#42", "--epic", "org/repo#200"])


def test_move_uses_one_reparent(graph: FakeSubIssueGraph) -> None:
    """A task under A moved to B is one addSubIssue with replaceParent, no remove."""
    _under(graph, OLD, TASK)
    rc = _run_move()
    assert rc == 0
    assert graph.mutations == [("reparent", NEW, TASK)]
    assert graph.parent_of(TASK) == NEW
    assert graph.children_of(OLD) == []
    assert graph.children_of(NEW) == [TASK]


def test_failed_move_leaves_task_in_place(
    graph: FakeSubIssueGraph,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed re-parent exits 1 with the error, and nothing was unlinked first."""
    _under(graph, OLD, TASK)
    attempted = _fail_links(graph, monkeypatch, RuntimeError("GraphQL errors: boom"))
    rc = _run_move()
    assert rc == 1
    assert capsys.readouterr().err == (
        "vrg-epic-move: moving org/repo#42 under epic org/repo#200 failed: GraphQL errors: boom\n"
    )
    assert attempted == [epics._REPARENT_SUBISSUE]
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD
    assert graph.children_of(OLD) == [TASK]


def test_failed_move_missing_token_keeps_cli_message(
    graph: FakeSubIssueGraph,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing token during the move gets cli_main's configuration message."""
    _under(graph, OLD, TASK)
    exc = github.MissingGitHubTokenError(1, ["gh"], stderr="GH_TOKEN environment variable")
    _fail_links(graph, monkeypatch, exc)
    with pytest.raises(SystemExit) as excinfo:
        _run_move()
    assert excinfo.value.code == 1
    assert capsys.readouterr().err.startswith("vrg-epic-move: no GitHub token available.")
    assert graph.parent_of(TASK) == OLD


def test_move_unparented_task_links_it(graph: FakeSubIssueGraph) -> None:
    """A task with no parent is linked with a plain addSubIssue, no replaceParent."""
    rc = _run_move()
    assert rc == 0
    assert graph.mutations == [("add", NEW, TASK)]
    assert graph.parent_of(TASK) == NEW


def test_move_into_closed_epic_refused(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    """A closed target is refused by name before any write; the task stays under A."""
    _under(graph, OLD, TASK)
    graph.close(NEW)
    # ClosedEpicError reaches cli_main, which prints one line and exits 1.
    with pytest.raises(SystemExit) as excinfo:
        _run_move()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err == f"vrg-epic-move: {epics.ClosedEpicError(NEW)}\n"
    assert "org/repo#200" in err
    assert "Traceback" not in err
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD


def test_main_idempotent_when_already_under_target(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _under(graph, NEW, TASK)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    assert capsys.readouterr().out == (
        "org/repo#42 is already under epic org/repo#200; nothing to do.\n"
    )
    assert graph.mutations == []
    assert graph.parent_of(TASK) == NEW


def test_main_links_when_task_has_no_parent(graph: FakeSubIssueGraph) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    assert graph.mutations == [("add", NEW, TASK)]
    assert graph.parent_of(TASK) == NEW


def test_main_rejects_non_epic_target(graph: FakeSubIssueGraph) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", side_effect=ValueError("not an epic")),
    ):
        rc = main(["--task", "#42", "--epic", "#999"])
    assert rc == 1
    assert graph.mutations == []


def test_main_unconfirmed_standing_epic_exits_1_naming_it(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    # A standing epic created but not confirmed is a clean refusal, not a traceback.
    message = "created ad-hoc epic org/.github#10 but org/.github also has org/.github#9"
    exc = epics.StandingEpicDuplicateError(
        message, epics.IssueRef("org", ".github", 10), [epics.IssueRef("org", ".github", 9)]
    )
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", side_effect=exc),
    ):
        rc = main(["--task", "#42", "--epic", "adhoc"])
    assert rc == 1
    assert capsys.readouterr().err == f"vrg-epic-move: {message}\n"
    assert graph.mutations == []


def test_main_accepts_adhoc_epic(graph: FakeSubIssueGraph) -> None:
    """The 'adhoc' sentinel skips the cross-org guard (it resolves in .github)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "adhoc"])
    assert rc == 0
    assert graph.mutations == [("add", NEW, TASK)]


@pytest.mark.parametrize(
    ("task_ref", "task_repo"),
    [
        ("other-org/proj#49", "other-org/proj"),  # different org from cwd
        ("cwd-org/other#49", "cwd-org/other"),  # same org, different repo
    ],
)
def test_main_adhoc_resolves_against_task_repo_not_cwd(
    graph: FakeSubIssueGraph, task_ref: str, task_repo: str
) -> None:
    """'--epic adhoc' targets the task repo's ad-hoc epic, not the cwd repo's (#3409)."""
    task = graph.add_issue(epics.parse_issue_ref(task_ref, default_repo=task_repo), title="t")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/tooling"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW) as mock_resolve,
    ):
        rc = main(["--task", task_ref, "--epic", "adhoc"])
    assert rc == 0
    mock_resolve.assert_called_once_with("adhoc", repo=task_repo)
    assert graph.parent_of(task) == NEW


def test_main_bare_epic_resolves_against_task_epic_home(graph: FakeSubIssueGraph) -> None:
    """A bare '--epic #N' reads against the task's epic home, not the cwd repo (#3411).

    The existence check behind the epic-home lookup runs under the task
    owner's App token, never the cwd org's.
    """
    seen: list[tuple[str, str, str | None]] = []
    expected_epic = graph.add_issue(
        epics.IssueRef("org", ".github", 407), title="Epic: home", labels=["epic"]
    )
    task = graph.add_issue(epics.IssueRef("org", "lab", 123), title="a lab task")

    def exists(owner: str, name: str) -> bool:
        seen.append((owner, name, github._target_org.get()))
        return True

    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch("vergil_tooling.lib.github.repo_exists", side_effect=exists),
        patch("vergil_tooling.lib.epics._labels", wraps=epics._labels) as mock_labels,
    ):
        rc = main(["--task", "org/lab#123", "--epic", "#407"])
    assert rc == 0
    mock_labels.assert_any_call(expected_epic)
    assert graph.mutations == [("add", expected_epic, task)]
    assert graph.parent_of(task) == expected_epic
    assert seen == [("org", ".github", "org")]


def test_main_bare_epic_with_cross_org_task_is_not_rejected(graph: FakeSubIssueGraph) -> None:
    """A bare epic ref homes in the task's org, so it never trips the cross-org guard."""
    graph.add_issue(epics.IssueRef("other-org", "proj", 49), title="t")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/elsewhere"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW) as mock_resolve,
    ):
        rc = main(["--task", "other-org/proj#49", "--epic", "#407"])
    assert rc == 0
    mock_resolve.assert_called_once_with("#407", repo="other-org/proj")


def test_main_scopes_token_to_task_owner(graph: FakeSubIssueGraph) -> None:
    """The re-parent runs under the task's owner installation, not cwd (#2070)."""
    task = graph.add_issue(epics.IssueRef("other-org", "repo", 42), title="t")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/repo"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "other-org/repo#42", "--epic", "other-org/repo#200"])
    assert rc == 0
    # The release-group read (#5344) also runs under the task's owner: every scope is that owner.
    assert mock_scope.call_count >= 1
    assert {c.args[0] for c in mock_scope.call_args_list} == {"other-org"}
    assert graph.parent_of(task) == NEW


def test_main_rejects_cross_org_task_and_epic(graph: FakeSubIssueGraph) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
    ):
        rc = main(["--task", "org-a/repo#42", "--epic", "org-b/repo#200"])
    assert rc == 1
    mock_resolve.assert_not_called()
    assert graph.mutations == []


def test_main_reports_missing_installation(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_links(graph, monkeypatch, github.NoInstallationError("org", []))
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 1
    assert graph.mutations == []
    assert graph.parent_of(TASK) is None


@pytest.mark.parametrize("epic_sentinel", ["adhoc", "intake", "planning", "operations"])
def test_main_accepts_every_standing_sentinel(graph: FakeSubIssueGraph, epic_sentinel: str) -> None:
    """Every standing sentinel passes the cross-org guard and resolves (#3514)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW) as mock_resolve,
    ):
        rc = main(["--task", "#42", "--epic", epic_sentinel])
    assert rc == 0
    mock_resolve.assert_called_once_with(epic_sentinel, repo="org/repo")
    assert graph.mutations == [("add", NEW, TASK)]


def test_main_planning_ensures_in_progress_label_in_task_repo(graph: FakeSubIssueGraph) -> None:
    """Moving a member-repo task into planning ensures 'in-progress' there (#3520)."""
    tool_task = graph.add_issue(epics.IssueRef("org", "tool", 36), title="a tool task")
    planning = graph.add_issue(
        epics.IssueRef("org", ".github", 439), title="Planning", labels=["epic"]
    )
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._find_epic_by_title", return_value=planning),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
    ):
        rc = main(["--task", "org/tool#36", "--epic", "planning"])
    assert rc == 0
    mock_ensure.assert_any_call("org/tool", ("in-progress",))
    assert graph.mutations == [("add", planning, tool_task)]


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


def test_epic_operations_sentinel_accepted(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    """--epic operations is listed and resolves the org's operations epic (#1494)."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    assert "'operations'" in " ".join(capsys.readouterr().out.split())
    ops = graph.add_issue(epics.IssueRef("org", ".github", 31), title="Ops", labels=["epic"])
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch("vergil_tooling.lib.epics.ensure_operations_epic", return_value=ops) as mock_ensure,
    ):
        rc = main(["--task", "#42", "--epic", "operations"])
    assert rc == 0
    mock_ensure.assert_called_once_with("org/repo")
    assert graph.mutations == [("add", ops, TASK)]


# -- The chain layer: continuations and its refusals (epic .github#1609) ------


def test_epic_move_into_chain(graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]) -> None:
    """A move lands in the target's chain; a task already in a continuation stays put."""
    # NEW is full: 99 items and its continuation, which already holds `linked`.
    _fill(graph, NEW, 99)
    continuation = graph.add_continuation(epics.IssueRef("org", "repo", 300), NEW, NEW)
    linked = graph.add_issue(epics.IssueRef("org", "repo", 43), title="already in the chain")
    graph.link(continuation, linked)
    _under(graph, OLD, TASK)

    # A task under another epic moves into the chain in one re-parent.
    rc = _run_move()
    assert rc == 0
    assert graph.mutations == [("reparent", continuation, TASK)]
    assert graph.parent_of(TASK) == continuation
    assert TASK not in graph.children_of(OLD)
    assert sum(TASK in graph.children_of(ref) for ref in graph.issues) == 1
    assert epic_chain.chain_of(NEW) == [NEW, continuation]
    assert capsys.readouterr().out == (
        "Moved org/repo#42 under epic org/repo#200 (in continuation org/repo#300).\n"
    )

    # A task linked in the target's continuation is already under the epic.
    graph.clear_log()
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(
            f"{_MOD}.epic_chain.chain_contains", wraps=epic_chain.chain_contains
        ) as mock_contains,
    ):
        rc = main(["--task", "#43", "--epic", "org/repo#200"])
    assert rc == 0
    mock_contains.assert_called_once_with(NEW, linked)
    assert capsys.readouterr().out == (
        "org/repo#43 is already under epic org/repo#200; nothing to do.\n"
    )
    assert graph.mutations == []
    assert graph.parent_of(linked) == continuation


def test_epic_move_chain_refusal_leaves_task_in_place(
    graph: FakeSubIssueGraph,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A target at the nesting limit is refused in one line; the task stays under A."""
    monkeypatch.setattr(epic_chain, "MAX_DEPTH", 1)
    _fill(graph, NEW, 3)
    _under(graph, OLD, TASK)
    with pytest.raises(SystemExit) as excinfo:
        _run_move()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    refusal = epic_chain.ChainDepthError(NEW, items=3, containers=1)
    assert err == f"vrg-epic-move: {refusal}\n"
    assert "Traceback" not in err
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD
    assert len(graph.children_of(NEW)) == 3


# -- Deferred queue: entered and left only through vrg-intake-route (#3673) ---


def test_epic_move_refuses_deferred_sentinel(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _under(graph, OLD, TASK)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
    ):
        rc = main(["--task", "#42", "--epic", "deferred"])
    assert rc == 1
    assert "vrg-intake-route" in capsys.readouterr().err
    mock_resolve.assert_not_called()  # the deferred epic is never created
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD


def test_epic_move_refuses_deferred_epic_number(
    graph: FakeSubIssueGraph, queue_kind: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_kind.side_effect = lambda epic: "deferred" if epic == NEW else None
    _under(graph, OLD, TASK)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "vrg-intake-route" in err
    assert "org/repo#200" in err
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD


def test_epic_move_refuses_task_under_deferred_epic(
    graph: FakeSubIssueGraph, queue_kind: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_kind.side_effect = lambda epic: "deferred" if epic == OLD else None
    _under(graph, OLD, TASK)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "vrg-intake-route" in err
    assert "org/repo#100" in err
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD


def test_epic_move_moves_task_out_of_intake(
    graph: FakeSubIssueGraph, queue_kind: MagicMock
) -> None:
    """Only the deferred queue is route-only; an intake parent still moves."""
    queue_kind.side_effect = lambda epic: "intake" if epic == OLD else None
    _under(graph, OLD, TASK)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    assert graph.mutations == [("reparent", NEW, TASK)]
    assert graph.parent_of(TASK) == NEW


# -- A grouped task stays where its group is (epic vergil-project/.github#1672, spec §5.1) --


def test_move_grouped_task_refused(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _under(graph, OLD, TASK)
    with patch(_GROUPS, return_value=[GROUP]):
        rc = _run_move()
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to move org/repo#42" in err
    assert "G1" in err
    assert "org/repo#100" in err
    assert "vrg-release-group set" in err
    assert "dissolve" in err
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD


def test_move_group_read_failure_refused(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _under(graph, OLD, TASK)
    with patch(_GROUPS, side_effect=record.GroupRecordError("org/repo cannot be read")):
        rc = _run_move()
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to move org/repo#42" in err
    assert "org/repo cannot be read" in err
    assert graph.mutations == []
    assert graph.parent_of(TASK) == OLD


def test_move_after_dissolve_allowed(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group recorded at rev 1 and dissolved at rev 2 is not live: the move goes ahead."""
    _under(graph, OLD, TASK)

    def comment(cid: str, body: str) -> StreamComment:
        return StreamComment(
            id=cid,
            login="human",
            body=body,
            role=Role.HUMAN,
            at="2026-11-01T09:30:00Z",
            edited=False,
        )

    stream = CommentStream(
        comments=(
            comment("900", "release-group G1 rev 1: org/repo#42"),
            comment("901", "release-group G1 rev 2: dissolved"),
        ),
        ids=frozenset({"900", "901"}),
        skipped=(),
    )
    monkeypatch.setattr(trust, "read_comment_stream", lambda repo, number, **_: stream)
    monkeypatch.setattr(epics, "epic_kind", lambda epic: (epic, epics.DEVELOPMENT_KIND))
    monkeypatch.setattr(
        epics, "task_epic", lambda task: epics.TaskEpic(task, OLD, epics.DEVELOPMENT_KIND, OLD)
    )
    assert _run_move() == 0
    assert graph.mutations == [("reparent", NEW, TASK)]
    assert graph.parent_of(TASK) == NEW
