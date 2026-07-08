"""Tests for vergil_tooling.lib.epic_audit."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from vergil_tooling.lib import epic_audit, epics, github, roadmap
from vergil_tooling.lib.epic_audit import TaskDrift

if TYPE_CHECKING:
    import pytest


def test_task_drift_flags_open_task_behind_merged_pr() -> None:
    prs = [
        {
            "number": 1948,
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u1948",
            "body": "Ref #1947",
        },
        {  # task closed -> not drift
            "number": 100,
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u100",
            "body": "Ref #99",
        },
        {  # no ref -> skipped
            "number": 101,
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u101",
            "body": "no linkage",
        },
        {"number": 102, "repository": {}, "url": "u102", "body": "Ref #5"},  # no repo -> skipped
        {  # multiple refs -> skipped
            "number": 103,
            "repository": {"nameWithOwner": "o/r"},
            "url": "u103",
            "body": "Ref #1\nRef #2",
        },
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        # issue view: args = ("issue", "view", "<number>", ...)
        state = "open" if args[2] == "1947" else "closed"
        return {"state": state, "title": f"feat: task {args[2]}", "body": "task body"}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        result = epic_audit.task_drift("2026-06-01", org="vergil-project")
    assert result == [TaskDrift("vergil-project/vergil-tooling", 1947, 1948, "u1948")]


def test_task_drift_skips_release_tracking_issue() -> None:
    # A merged release PR Refs its open ``release: X.Y.Z`` tracking issue, which
    # is vrg-release bookkeeping — not a slipped task. It must not be flagged.
    prs = [
        {
            "number": 500,
            "repository": {"nameWithOwner": "vergil-project/vergil-containers"},
            "url": "u500",
            "body": "Ref #373",
        },
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        return {
            "state": "open",
            "title": "release: 2.1.4",
            "body": "<!-- vrg-release:progress -->\n- [x] tag\n",
        }

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        result = epic_audit.task_drift("2026-06-01", org="vergil-project")
    assert result == []


def test_task_drift_resolves_cross_repo_ref() -> None:
    # A cross-repo Ref (owner/repo#N) must be looked up in the ref's repo, not
    # the PR's own repo (issue #2111).
    prs = [
        {
            "number": 7,
            "repository": {"nameWithOwner": "vergil-project/.github"},
            "url": "u7",
            "body": "Ref vergil-project/vergil-tooling#42",
        },
    ]
    looked_up: dict[str, str] = {}

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        looked_up["repo"] = args[args.index("--repo") + 1]
        return {"state": "open", "title": "feat: t", "body": "b"}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        result = epic_audit.task_drift("2026-06-01", org="vergil-project")
    assert looked_up["repo"] == "vergil-project/vergil-tooling"
    assert result == [TaskDrift("vergil-project/vergil-tooling", 42, 7, "u7")]


def test_task_drift_skips_unresolvable_task(capsys: pytest.CaptureFixture[str]) -> None:
    # A ref to a task this run can't see (cross-org, private, deleted) must be
    # skipped with a warning, not crash the sweep (issue #2111).
    prs = [
        {
            "number": 8,
            "repository": {"nameWithOwner": "logical-minds-foundry/.github"},
            "url": "u8",
            "body": "Ref vergil-project/vergil-tooling#2105",
        },
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        raise github.GitHubAPIError(1, ["gh"], "", "GraphQL: Could not resolve to an issue")

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        result = epic_audit.task_drift("2026-06-01", org="logical-minds-foundry")
    assert result == []
    assert "skipping vergil-project/vergil-tooling#2105" in capsys.readouterr().err


def test_task_drift_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"x": 1}):
        assert epic_audit.task_drift("2026-06-01", org="vergil-project") == []


def test_task_drift_skips_when_issue_view_is_not_an_object() -> None:
    # Defensive: gh issue view --json returns a JSON object, but if the response
    # is ever shaped unexpectedly (a list), skip the entry rather than crash.
    prs = [
        {
            "number": 1948,
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u1948",
            "body": "Ref #1947",
        },
    ]

    def fake_read_json(*args: str) -> object:
        return prs if args[0] == "search" else ["unexpected"]

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        assert epic_audit.task_drift("2026-06-01", org="vergil-project") == []


def test_epic_drift_flags_all_done_open_epics() -> None:
    summaries = [
        roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40"),  # all done
        roadmap.EpicSummary(7, "WIP", "2026-06-01", None, (), 3, 1, "u7"),  # not all done
        roadmap.EpicSummary(8, "Empty", "2026-06-01", None, (), 0, 0, "u8"),  # no children
    ]
    with patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=summaries):
        result = epic_audit.epic_drift()
    assert [e.number for e in result] == [40]


def test_epic_outside_dotgithub_flags_non_dotgithub_epics() -> None:
    issues = [
        {"number": 99, "repository": {"nameWithOwner": "vergil-project/vergil-tooling"}},
        {"number": 40, "repository": {"nameWithOwner": "vergil-project/.github"}},  # ok
        {"number": 5, "repository": {}},  # no repo -> skipped
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=issues) as mock_search:
        result = epic_audit.epic_outside_dotgithub("vergil-project")
    assert result == ["vergil-project/vergil-tooling#99"]
    args = mock_search.call_args.args
    assert "search" in args and "issues" in args and "epic" in args


def test_stray_dotgithub_issue_flags_unlinked_non_epic_non_intake() -> None:
    issues = [
        {"number": 40, "labels": [{"name": "epic"}]},  # epic -> ok
        {"number": 50, "labels": [{"name": "idea"}]},  # intake -> ok
        {"number": 86, "labels": [{"name": "documentation"}]},  # managed task -> ok
        {"number": 7, "labels": []},  # unlinked, non-epic, non-intake -> STRAY
    ]

    def fake_parent_of(ref: epics.IssueRef) -> epics.IssueRef | None:
        # #86 is a managed task under an epic; #7 has no parent.
        if ref.number == 86:
            return epics.IssueRef("vergil-project", ".github", 85)
        return None

    with (
        patch("vergil_tooling.lib.github.read_json", return_value=issues),
        patch("vergil_tooling.lib.epics.parent_of", side_effect=fake_parent_of),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
    ):
        result = epic_audit.stray_dotgithub_issue("vergil-project")
    assert result == ["vergil-project/.github#7"]


def test_render_shows_invariant_violations() -> None:
    out = epic_audit.render(
        [],
        [],
        org="vergil-project",
        window_days=30,
        epics_outside=["vergil-project/vergil-tooling#99"],
        stray=["vergil-project/.github#7"],
    )
    assert "Invariant violations" in out
    assert "vergil-project/vergil-tooling#99" in out
    assert "vergil-project/.github#7" in out


def test_render_invariant_only_epics_outside() -> None:
    out = epic_audit.render(
        [],
        [],
        org="vergil-project",
        window_days=30,
        epics_outside=["vergil-project/vergil-tooling#99"],
    )
    assert "Epics outside" in out
    assert "vergil-project/vergil-tooling#99" in out
    assert "Stray" not in out


def test_render_invariant_only_stray() -> None:
    out = epic_audit.render(
        [],
        [],
        org="vergil-project",
        window_days=30,
        stray=["vergil-project/.github#7"],
    )
    assert "Stray" in out
    assert "vergil-project/.github#7" in out
    assert "Epics outside" not in out


def test_render_clean() -> None:
    out = epic_audit.render([], [], org="vergil-project", window_days=30)
    assert "No drift" in out


def test_render_banner_states_scope_and_read_only() -> None:
    out = epic_audit.render([], [], org="acme-co", window_days=14)
    assert "Read-only audit" in out
    assert "**acme-co**" in out
    assert "last 14 days" in out
    assert "changes nothing" in out


def test_render_task_drift_only() -> None:
    out = epic_audit.render(
        [TaskDrift("o/r", 1947, 1948, "u1948")], [], org="vergil-project", window_days=30
    )
    assert "o/r#1947 — open; PR [#1948](u1948) merged" in out
    assert "## Epic drift" in out
    assert out.count("_none_") == 1  # epic section is empty


def test_render_epic_drift_only() -> None:
    epic = roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40")
    out = epic_audit.render([], [epic], org="vergil-project", window_days=30)
    assert "[#40](u40) Convention — 9/9 done" in out
    assert "## Task drift" in out
    assert out.count("_none_") == 1  # task section is empty


def test_close_drift_closes_tasks_in_repo_and_epics_in_dot_github() -> None:
    tasks = [TaskDrift("vergil-project/vergil-tooling", 1947, 1948, "u1948")]
    epics = [roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40")]
    run = MagicMock()
    with patch("vergil_tooling.lib.github.run", run):
        closed = epic_audit.close_drift(tasks, epics, org="vergil-project")
    assert closed == ["vergil-project/vergil-tooling#1947", "vergil-project/.github#40"]
    task_call = run.call_args_list[0]
    assert task_call.args[:5] == (
        "issue",
        "close",
        "1947",
        "--repo",
        "vergil-project/vergil-tooling",
    )
    assert "PR #1948 merged" in task_call.args[-1]
    epic_call = run.call_args_list[1]
    assert epic_call.args[:5] == ("issue", "close", "40", "--repo", "vergil-project/.github")
    assert "all 9 child tasks" in epic_call.args[-1]


def test_close_drift_empty_is_noop() -> None:
    run = MagicMock()
    with patch("vergil_tooling.lib.github.run", run):
        assert epic_audit.close_drift([], [], org="vergil-project") == []
    run.assert_not_called()


def test_render_closed_lists_what_closed() -> None:
    out = epic_audit.render_closed(
        ["vergil-project/vergil-tooling#1947", "vergil-project/.github#40"],
        org="vergil-project",
        window_days=30,
    )
    assert "— closed" in out
    assert "vergil-project/vergil-tooling#1947" in out
    assert "vergil-project/.github#40" in out


def test_render_closed_empty_says_nothing_to_close() -> None:
    out = epic_audit.render_closed([], org="vergil-project", window_days=30)
    assert "nothing to close" in out


# -- validation-aware rollup/audit (epic vergil-project/.github#115) ----------


def test_validation_status_classifies_runnable_vs_blocked() -> None:
    epic = epics.IssueRef("org", ".github", 115)
    val_runnable = epics.IssueRef("org", "repo", 7)
    val_blocked = epics.IssueRef("org", "repo", 8)
    children = [
        epics.ChildState(val_runnable, "OPEN"),
        epics.ChildState(val_blocked, "OPEN"),
        epics.ChildState(epics.IssueRef("org", "repo", 5), "OPEN"),  # not a validation task
        epics.ChildState(epics.IssueRef("org", "repo", 9), "CLOSED"),  # closed -> ignored
    ]

    def is_validation(ref: epics.IssueRef) -> bool:
        return ref.number in (7, 8, 9)

    def all_blockers_closed(ref: epics.IssueRef) -> bool:
        return ref.number == 7  # #7 runnable, #8 still blocked

    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
        patch("vergil_tooling.lib.epics.is_validation", side_effect=is_validation),
        patch("vergil_tooling.lib.epics.all_blockers_closed", side_effect=all_blockers_closed),
    ):
        status = epic_audit.validation_status(epic)
    assert status.runnable == (val_runnable,)
    assert status.blocked == (val_blocked,)
    assert status.pending == (val_runnable, val_blocked)


def test_validation_pending_collects_only_epics_with_open_validations() -> None:
    val = epics.IssueRef("org", "repo", 7)

    def fake_status(epic: epics.IssueRef) -> epic_audit.ValidationStatus:
        if epic.number == 115:
            return epic_audit.ValidationStatus(epic, (val,), ())
        return epic_audit.ValidationStatus(epic, (), ())  # nothing pending

    with (
        patch(
            "vergil_tooling.lib.epic_audit.roadmap.gather",
            return_value=[MagicMock(number=115), MagicMock(number=200)],
        ),
        patch("vergil_tooling.lib.epic_audit.validation_status", side_effect=fake_status),
    ):
        pending = epic_audit.validation_pending("org")
    assert [s.epic.number for s in pending] == [115]


def test_closed_validation_without_pass_flags_missing_pass() -> None:
    search = [
        {"number": 120, "repository": {"nameWithOwner": "org/.github"}},  # has PASS -> ok
        {"number": 55, "repository": {"nameWithOwner": "org/repo"}},  # no PASS -> flagged
        {"number": 77, "repository": {}},  # no repo -> skipped
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return search
        number = args[2]
        if number == "120":
            return {"comments": [{"body": "ran it\n- Outcome: PASS"}]}
        return {"comments": [{"body": "closed early; no result recorded"}]}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        result = epic_audit.closed_validation_without_pass("org")
    assert result == ["org/repo#55"]


def test_closed_validation_pass_marker_excludes_unresolved_template() -> None:
    # The scaffold's unresolved "Outcome: PASS / FAIL" line must NOT read as a pass.
    search = [{"number": 1, "repository": {"nameWithOwner": "org/repo"}}]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return search
        return {"comments": [{"body": "- Outcome: PASS / FAIL"}]}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        assert epic_audit.closed_validation_without_pass("org") == ["org/repo#1"]


def test_render_includes_validation_pending_section() -> None:
    status = epic_audit.ValidationStatus(
        epics.IssueRef("org", ".github", 115),
        (epics.IssueRef("org", "repo", 7),),
        (epics.IssueRef("org", "repo", 8),),
    )
    out = epic_audit.render([], [], org="org", window_days=30, pending_validation=[status])
    assert "Validation pending" in out
    assert "org/repo#7" in out  # runnable
    assert "org/repo#8" in out  # blocked


def test_render_flags_closed_validation_without_pass() -> None:
    out = epic_audit.render(
        [], [], org="org", window_days=30, closed_validation_no_pass=["org/repo#55"]
    )
    assert "PASS comment" in out
    assert "org/repo#55" in out
