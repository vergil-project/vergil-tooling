"""Tests for vergil_tooling.lib.epic_audit."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import epic_audit, epics, github, roadmap
from vergil_tooling.lib.epic_audit import TaskDrift


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

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
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

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
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


def test_task_drift_skips_epic_ref() -> None:
    # A merged PR may legitimately ``Ref`` an epic; that is not a slipped task.
    # Closing the epic here would orphan its open children (issue #2259, Fix A).
    prs = [
        {
            "number": 22,
            "repository": {"nameWithOwner": "logical-minds-foundry/.github"},
            "url": "u22",
            "body": "Ref #19",
        },
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        return {
            "state": "open",
            "title": "Epic: MQ Configuration Guides",
            "body": "epic body",
            "labels": [{"name": "epic"}],
        }

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        assert epic_audit.task_drift("2026-06-01", org="logical-minds-foundry") == []


def test_task_drift_skips_operational_refs() -> None:
    # An operational task (closes on an Outcome comment) is never a PR-tracked
    # task — a merged PR Ref'ing it is not slipped-task drift. (Queue items are
    # skipped by their parent: see test_task_drift_skips_pr_ref_to_queue_item.)
    prs = [
        {
            "number": 30,
            "repository": {"nameWithOwner": "org/repo"},
            "url": "u30",
            "body": "Ref #7",
        },
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        return {"state": "open", "title": "t", "body": "b", "labels": [{"name": "validation"}]}

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.epics.parent_of") as parent,
    ):
        assert epic_audit.task_drift("2026-06-01", org="org") == []
    parent.assert_not_called()


def test_task_drift_still_flags_labelled_plain_task() -> None:
    # The label guard must not over-skip: an ordinary task carrying a non-special
    # label (e.g. ``bug``) is still drift when its PR merged and it stays open.
    prs = [
        {
            "number": 40,
            "repository": {"nameWithOwner": "org/repo"},
            "url": "u40",
            "body": "Ref #41",
        },
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        return {"state": "open", "title": "fix: t", "body": "b", "labels": [{"name": "bug"}]}

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        result = epic_audit.task_drift("2026-06-01", org="org")
    assert result == [TaskDrift("org/repo", 41, 40, "u40")]


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


def test_epic_outside_dotgithub_flags_public_non_dotgithub_epics() -> None:
    issues = [
        {"number": 99, "repository": {"nameWithOwner": "vergil-project/vergil-tooling"}},
        {"number": 40, "repository": {"nameWithOwner": "vergil-project/.github"}},  # ok
        {"number": 5, "repository": {}},  # no repo -> skipped
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=issues) as mock_search,
        patch("vergil_tooling.lib.github.is_public", return_value=True),
    ):
        result = epic_audit.epic_outside_dotgithub("vergil-project")
    assert result == ["vergil-project/vergil-tooling#99"]
    args = mock_search.call_args.args
    assert "search" in args and "issues" in args and "epic" in args


def test_epic_outside_dotgithub_ignores_private_repo_epic() -> None:
    # A private repo legitimately self-homes its epics -> not a violation.
    issues = [{"number": 4, "repository": {"nameWithOwner": "vergil-project/lab"}}]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=issues),
        patch("vergil_tooling.lib.github.is_public", return_value=False),
    ):
        assert epic_audit.epic_outside_dotgithub("vergil-project") == []


def test_epic_outside_dotgithub_fails_loud_on_probe_error() -> None:
    # A visibility probe that errors must raise, never silently skip (a genuine
    # leaked-out public epic would otherwise be masked).
    issues = [{"number": 4, "repository": {"nameWithOwner": "vergil-project/lab"}}]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=issues),
        patch(
            "vergil_tooling.lib.github.is_public",
            side_effect=github.GitHubAPIError(1, "cmd", "boom"),
        ),
        pytest.raises(github.GitHubAPIError),
    ):
        epic_audit.epic_outside_dotgithub("vergil-project")


def test_stray_dotgithub_issue_flags_unlinked_non_epic() -> None:
    issues = [
        {"number": 40, "labels": [{"name": "epic"}]},  # epic -> ok
        {"number": 50, "labels": []},  # queue item under the intake epic -> ok
        {"number": 86, "labels": [{"name": "documentation"}]},  # managed task -> ok
        {"number": 7, "labels": []},  # unlinked, non-epic -> STRAY
    ]

    def fake_parent_of(ref: epics.IssueRef) -> epics.IssueRef | None:
        # #86 is a managed task under an epic, #50 sits under the intake epic;
        # #7 has no parent.
        if ref.number == 86:
            return epics.IssueRef("vergil-project", ".github", 85)
        if ref.number == 50:
            return epics.IssueRef("vergil-project", ".github", 100)
        return None

    with (
        patch("vergil_tooling.lib.github.read_json", return_value=issues),
        patch("vergil_tooling.lib.epics.parent_of", side_effect=fake_parent_of),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
    ):
        result = epic_audit.stray_dotgithub_issue("vergil-project")
    assert result == ["vergil-project/.github#7"]


def test_stray_skips_open_archive() -> None:
    # An open, top-level 'archive'-labelled per-quarter bucket has no parent and
    # is not epic-labelled, but it is a legitimate home issue, not a stray.
    rows = [
        {"number": 88, "labels": [{"name": "archive"}, {"name": "ad-hoc"}]},
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        strays = epic_audit.stray_dotgithub_issue("org", home="org/.github")
    assert strays == []  # an archive is not a stray


# -- closed-epic-with-open-child invariant + reopen remediation (issue #2259) --


def test_closed_epic_open_child_flags_closed_epic_with_open_child() -> None:
    listing = [
        {"number": 19, "title": "MQ Guides", "labels": []},  # has an open child -> violation
        {"number": 15, "title": "Cockpit", "labels": []},  # all children closed -> ok
        # perpetual -> skipped
        {"number": 99, "title": "Ad hoc", "labels": [{"name": "epic"}, {"name": "ad-hoc"}]},
    ]

    def fake_child_states(epic: epics.IssueRef) -> list[epics.ChildState]:
        if epic.number == 19:
            return [
                epics.ChildState(epics.IssueRef("org", "lab", 462), "OPEN"),
                epics.ChildState(epics.IssueRef("org", "lab", 463), "CLOSED"),
            ]
        return [epics.ChildState(epics.IssueRef("org", "lab", 1), "CLOSED")]

    with (
        patch("vergil_tooling.lib.github.read_json", return_value=listing),
        patch("vergil_tooling.lib.epics.child_states", side_effect=fake_child_states),
    ):
        result = epic_audit.closed_epic_open_child("org")
    assert result == [(epics.IssueRef("org", ".github", 19), (epics.IssueRef("org", "lab", 462),))]


@pytest.mark.parametrize("kind_label", ["ad-hoc", "intake", "planning"])
def test_closed_epic_open_child_skips_perpetual_before_fetching_children(
    kind_label: str,
) -> None:
    # A perpetual standing epic (ad-hoc, intake, planning) is skipped without a
    # children lookup.
    labels = [{"name": "epic"}, {"name": kind_label}]
    listing = [{"number": 99, "title": "Standing", "labels": labels}]
    child_states = MagicMock()
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=listing),
        patch("vergil_tooling.lib.epics.child_states", child_states),
    ):
        assert epic_audit.closed_epic_open_child("org") == []
    child_states.assert_not_called()


def test_closed_epic_open_child_scopes_to_home() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[]) as mock_list,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        epic_audit.closed_epic_open_child("org", home="org/lab")
    assert "org/lab" in mock_list.call_args.args  # lists the home, not <org>/.github
    assert "closed" in mock_list.call_args.args  # only closed epics


def test_reopen_epics_with_open_children_reopens_and_comments() -> None:
    violations: list[epic_audit.EpicOpenChildViolation] = [
        (epics.IssueRef("org", ".github", 19), (epics.IssueRef("org", "lab", 462),))
    ]
    run = MagicMock()
    with patch("vergil_tooling.lib.github.run", run):
        reopened = epic_audit.reopen_epics_with_open_children(violations)
    assert reopened == ["org/.github#19"]
    assert run.call_args.args[:5] == ("issue", "reopen", "19", "--repo", "org/.github")
    assert "org/lab#462" in run.call_args.args[-1]


def test_reopen_epics_with_open_children_empty_is_noop() -> None:
    run = MagicMock()
    with patch("vergil_tooling.lib.github.run", run):
        assert epic_audit.reopen_epics_with_open_children([]) == []
    run.assert_not_called()


def test_render_flags_closed_epic_with_open_child() -> None:
    out = epic_audit.render(
        [],
        [],
        org="org",
        window_days=30,
        closed_epic_open_children=[
            (epics.IssueRef("org", ".github", 19), (epics.IssueRef("org", "lab", 462),))
        ],
    )
    assert "Invariant violations" in out
    assert "Closed epics with open children" in out
    assert "org/.github#19" in out
    assert "org/lab#462" in out


def test_render_closed_lists_reopened_epics() -> None:
    out = epic_audit.render_closed([], org="org", window_days=30, reopened=["org/.github#19"])
    assert "Reopened" in out
    assert "org/.github#19" in out


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


def test_operational_status_classifies_runnable_vs_blocked() -> None:
    epic = epics.IssueRef("org", ".github", 115)
    val_runnable = epics.IssueRef("org", "repo", 7)
    val_blocked = epics.IssueRef("org", "repo", 8)
    children = [
        epics.ChildState(val_runnable, "OPEN"),
        epics.ChildState(val_blocked, "OPEN"),
        epics.ChildState(epics.IssueRef("org", "repo", 5), "OPEN"),  # not an operational task
        epics.ChildState(epics.IssueRef("org", "repo", 9), "CLOSED"),  # closed -> ignored
    ]

    def kind(ref: epics.IssueRef) -> str | None:
        return "validation" if ref.number in (7, 8, 9) else None

    def all_blockers_closed(ref: epics.IssueRef) -> bool:
        return ref.number == 7  # #7 runnable, #8 still blocked

    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
        patch("vergil_tooling.lib.epics.operational_kind", side_effect=kind),
        patch("vergil_tooling.lib.epics.all_blockers_closed", side_effect=all_blockers_closed),
    ):
        status = epic_audit.operational_status(epic)
    assert status.runnable == (val_runnable,)
    assert status.blocked == (val_blocked,)
    assert status.pending == (val_runnable, val_blocked)


def test_operational_status_tags_kind() -> None:
    epic = epics.IssueRef("org", ".github", 124)
    val = epics.IssueRef("org", "repo", 7)
    dep = epics.IssueRef("org", "repo", 8)
    children = [epics.ChildState(val, "OPEN"), epics.ChildState(dep, "OPEN")]

    def kind(ref: epics.IssueRef) -> str | None:
        return "validation" if ref.number == 7 else "deployment"

    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
        patch("vergil_tooling.lib.epics.operational_kind", side_effect=kind),
        patch("vergil_tooling.lib.epics.all_blockers_closed", return_value=True),
    ):
        status = epic_audit.operational_status(epic)
    # keyed by IssueRef (cross-repo safe), not bare number
    assert status.by_kind[val] == "validation"
    assert status.by_kind[dep] == "deployment"


def test_child_status_classifies_every_open_child() -> None:
    epic = epics.IssueRef("org", ".github", 99)
    plain_runnable = epics.IssueRef("org", "repo", 10)
    plain_blocked = epics.IssueRef("org", "repo", 11)
    op_runnable = epics.IssueRef("org", "repo", 12)  # operational, but not special-cased here
    children = [
        epics.ChildState(plain_runnable, "OPEN"),
        epics.ChildState(plain_blocked, "OPEN"),
        epics.ChildState(op_runnable, "OPEN"),
        epics.ChildState(epics.IssueRef("org", "repo", 13), "CLOSED"),  # closed -> ignored
    ]

    def all_blockers_closed(ref: epics.IssueRef) -> bool:
        return ref.number != 11  # only #11 is still blocked

    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
        patch("vergil_tooling.lib.epics.all_blockers_closed", side_effect=all_blockers_closed),
    ):
        status = epic_audit.child_status(epic)
    # Every open child is classified, plain tasks included — no kind gating.
    assert status.runnable == (plain_runnable, op_runnable)
    assert status.blocked == (plain_blocked,)


def test_child_status_empty_when_no_open_children() -> None:
    epic = epics.IssueRef("org", ".github", 99)
    children = [epics.ChildState(epics.IssueRef("org", "repo", 1), "CLOSED")]
    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
        patch("vergil_tooling.lib.epics.all_blockers_closed", return_value=True) as mock_blockers,
    ):
        status = epic_audit.child_status(epic)
    assert status.runnable == ()
    assert status.blocked == ()
    mock_blockers.assert_not_called()  # closed children are never queried


def test_operational_pending_collects_only_epics_with_open_validations() -> None:
    val = epics.IssueRef("org", "repo", 7)

    def fake_status(epic: epics.IssueRef) -> epic_audit.OperationalStatus:
        if epic.number == 115:
            return epic_audit.OperationalStatus(epic, (val,), (), {val: "validation"})
        return epic_audit.OperationalStatus(epic, (), (), {})  # nothing pending

    with (
        patch(
            "vergil_tooling.lib.epic_audit.roadmap.gather",
            return_value=[MagicMock(number=115), MagicMock(number=200)],
        ),
        patch("vergil_tooling.lib.epic_audit.operational_status", side_effect=fake_status),
    ):
        pending = epic_audit.operational_pending("org")
    assert [s.epic.number for s in pending] == [115]


def test_closed_operational_without_success_flags_missing_pass() -> None:
    search = [
        {"number": 120, "repository": {"nameWithOwner": "org/.github"}},  # has PASS -> ok
        {"number": 55, "repository": {"nameWithOwner": "org/repo"}},  # no PASS -> flagged
        {"number": 77, "repository": {}},  # no repo -> skipped
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            # invariant loops each operational label; return the fixture once
            return search if args[5] == "validation" else []
        number = args[2]
        if number == "120":
            return {"comments": [{"body": "ran it\n- Outcome: PASS"}]}
        return {"comments": [{"body": "closed early; no result recorded"}]}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        result = epic_audit.closed_operational_without_success("org")
    assert result == ["org/repo#55"]


def test_closed_validation_pass_marker_excludes_unresolved_template() -> None:
    # The scaffold's unresolved "Outcome: PASS / FAIL" line must NOT read as a pass.
    search = [{"number": 1, "repository": {"nameWithOwner": "org/repo"}}]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            # invariant loops each operational label; return the fixture once
            return search if args[5] == "validation" else []
        return {"comments": [{"body": "- Outcome: PASS / FAIL"}]}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        assert epic_audit.closed_operational_without_success("org") == ["org/repo#1"]


def test_success_marker_accepts_success_and_legacy_pass() -> None:
    search = [
        {"number": 1, "repository": {"nameWithOwner": "org/repo"}},  # SUCCESS -> ok
        {"number": 2, "repository": {"nameWithOwner": "org/repo"}},  # legacy PASS -> ok
        {"number": 3, "repository": {"nameWithOwner": "org/repo"}},  # neither -> flagged
    ]
    bodies = {"1": "- Outcome: SUCCESS", "2": "- Outcome: PASS", "3": "closed early"}

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            # invariant loops each operational label; return the fixture once
            return search if args[5] == "validation" else []
        return {"comments": [{"body": bodies[args[2]]}]}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        assert epic_audit.closed_operational_without_success("org") == ["org/repo#3"]


def test_render_includes_operational_pending_section() -> None:
    val = epics.IssueRef("org", "repo", 7)
    dep = epics.IssueRef("org", "repo", 8)
    status = epic_audit.OperationalStatus(
        epics.IssueRef("org", ".github", 115),
        (val,),
        (dep,),
        {val: "validation", dep: "deployment"},
    )
    out = epic_audit.render([], [], org="org", window_days=30, pending_operational=[status])
    assert "Operational tasks pending" in out
    assert "org/repo#7 (validation)" in out  # runnable, kind-tagged
    assert "org/repo#8 (deployment)" in out  # blocked, kind-tagged


def test_render_flags_closed_operational_without_success() -> None:
    out = epic_audit.render(
        [], [], org="org", window_days=30, closed_operational_no_success=["org/repo#55"]
    )
    assert "PASS comment" in out
    assert "org/repo#55" in out


# -- self-contained --repo audit (epic #130, home-scoped checks) --------------
def test_stray_dotgithub_issue_scopes_to_home() -> None:
    issues = [{"number": 7, "labels": []}]  # unlinked, non-epic, non-intake -> stray
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=issues) as mock_list,
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        result = epic_audit.stray_dotgithub_issue("org", home="org/lab")
    assert result == ["org/lab#7"]
    assert "org/lab" in mock_list.call_args.args  # read the home, not <org>/.github


def test_operational_pending_scopes_to_home() -> None:
    captured: list[epics.IssueRef] = []

    def fake_status(epic: epics.IssueRef) -> epic_audit.OperationalStatus:
        captured.append(epic)
        return epic_audit.OperationalStatus(epic, (), (), {})

    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=[MagicMock(number=5)]),
        patch("vergil_tooling.lib.epic_audit.operational_status", side_effect=fake_status),
    ):
        epic_audit.operational_pending("org", home="org/lab")
    assert (captured[0].owner, captured[0].repo) == ("org", "lab")


def test_close_drift_closes_epics_in_home() -> None:
    epic_summaries = [roadmap.EpicSummary(5, "Lab", "2026-07-09", None, (), 1, 1, "u5")]
    run = MagicMock()
    with patch("vergil_tooling.lib.github.run", run):
        closed = epic_audit.close_drift([], epic_summaries, org="org", home="org/lab")
    assert closed == ["org/lab#5"]
    assert run.call_args.args[:5] == ("issue", "close", "5", "--repo", "org/lab")


def test_epic_drift_scopes_to_home() -> None:
    with patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=[]) as gather:
        epic_audit.epic_drift("org", home="org/lab")
    assert gather.call_args.kwargs["home"] == "org/lab"


def test_render_banner_names_home_when_repo_scoped() -> None:
    out = epic_audit.render([], [], org="org", window_days=30, home="org/lab")
    assert "**org/lab**" in out  # banner names the scoped home, not the org


# -- render_epic_children (machine-readable enumeration, issue #2538) ---------


def test_render_epic_children_lists_children_with_states() -> None:
    epic = epics.IssueRef("vergil-project", ".github", 201)
    children = [
        epics.ChildState(
            epics.IssueRef("vergil-project", "vergil-tooling", 2538), "OPEN", "Harden enumeration"
        ),
        epics.ChildState(
            epics.IssueRef("vergil-project", "vergil-tooling", 2500), "CLOSED", "Earlier task"
        ),
    ]
    out = json.loads(epic_audit.render_epic_children(epic, children))
    assert out["epic"] == "vergil-project/.github#201"
    # Deterministic order (owner, repo, number): 2500 sorts before 2538.
    assert out["children"] == [
        {
            "repo": "vergil-project/vergil-tooling",
            "number": 2500,
            "title": "Earlier task",
            "state": "CLOSED",
        },
        {
            "repo": "vergil-project/vergil-tooling",
            "number": 2538,
            "title": "Harden enumeration",
            "state": "OPEN",
        },
    ]
    # Open children are trivially selectable from a proper array of objects — the
    # brittle-jq failure mode this replaces (issue #2538).
    open_children = [c for c in out["children"] if c["state"] == "OPEN"]
    assert [c["number"] for c in open_children] == [2538]


def test_render_epic_children_empty_when_no_children() -> None:
    epic = epics.IssueRef("vergil-project", ".github", 201)
    out = json.loads(epic_audit.render_epic_children(epic, []))
    assert out == {"epic": "vergil-project/.github#201", "children": []}


# -- orphan pass, label retirement, queue visibility (epic .github#176, Task 5) --


def _issue(
    repo: str,
    number: int,
    *,
    labels: tuple[str, ...] = (),
    title: str = "feat: a task",
    body: str = "",
    native_parent: epics.IssueRef | None = None,
    public: bool = True,
) -> epic_audit.OpenIssue:
    owner, name = repo.split("/", 1)
    return epic_audit.OpenIssue(
        ref=epics.IssueRef(owner, name, number),
        title=title,
        body=body,
        labels=frozenset(labels),
        native_parent=native_parent,
        public=public,
    )


def _gql_page(
    nodes: list[dict[str, object]], *, has_next: bool = False, cursor: str | None = None
) -> dict[str, object]:
    return {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


def _gql_node(
    number: int,
    *,
    labels: tuple[str, ...] = (),
    parent: tuple[str, str, int] | None = None,
    body: str | None = "",
) -> dict[str, object]:
    node: dict[str, object] = {
        "number": number,
        "title": f"t{number}",
        "body": body,
        "labels": {"totalCount": len(labels), "nodes": [{"name": n} for n in labels]},
        "parent": None,
    }
    if parent is not None:
        node["parent"] = {
            "number": parent[2],
            "repository": {"name": parent[1], "owner": {"login": parent[0]}},
        }
    return node


def test_open_issues_enumerates_managed_repos_with_paging() -> None:
    pages: dict[tuple[str, str | None], dict[str, object]] = {
        ("tool", None): _gql_page([_gql_node(5, labels=("idea",))], has_next=True, cursor="c1"),
        ("tool", "c1"): _gql_page([_gql_node(6, parent=("org", ".github", 9), body=None)]),
        ("secret", None): _gql_page([_gql_node(3)]),
    }
    calls: list[tuple[str, str | None]] = []

    def fake_graphql(_query: str, **variables: object) -> object:
        after = variables.get("after")
        key = (str(variables["name"]), None if after is None else str(after))
        calls.append(key)
        return pages[key]

    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool", "fork", "secret"]),
        patch(
            "vergil_tooling.lib.epics.is_managed_repo",
            side_effect=lambda repo: repo != "org/fork",
        ),
        patch("vergil_tooling.lib.github.is_public", side_effect=lambda repo: repo == "org/tool"),
        patch("vergil_tooling.lib.github.graphql", side_effect=fake_graphql),
    ):
        issues = epic_audit.open_issues("org")
    assert calls == [("tool", None), ("tool", "c1"), ("secret", None)]  # fork never read
    assert [i.ref.slug for i in issues] == ["org/tool#5", "org/tool#6", "org/secret#3"]
    assert issues[0].labels == frozenset({"idea"})
    assert issues[0].native_parent is None
    assert issues[0].public
    assert issues[1].native_parent == epics.IssueRef("org", ".github", 9)
    assert issues[1].body == ""
    assert not issues[2].public


def test_open_issues_fails_loud_on_label_overflow() -> None:
    node = _gql_node(5)
    node["labels"] = {"totalCount": 101, "nodes": []}
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool"]),
        patch("vergil_tooling.lib.epics.is_managed_repo", return_value=True),
        patch("vergil_tooling.lib.github.is_public", return_value=True),
        patch("vergil_tooling.lib.github.graphql", return_value=_gql_page([node])),
        pytest.raises(RuntimeError, match="org/tool#5"),
    ):
        epic_audit.open_issues("org")


def test_open_issues_tolerates_empty_repository_payload() -> None:
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool"]),
        patch("vergil_tooling.lib.epics.is_managed_repo", return_value=True),
        patch("vergil_tooling.lib.github.is_public", return_value=True),
        patch("vergil_tooling.lib.github.graphql", return_value={}),
    ):
        assert epic_audit.open_issues("org") == []


@pytest.mark.parametrize(
    "check",
    [
        epic_audit.orphan_drift,
        epic_audit.retired_label_drift,
        epic_audit.body_only_parents,
        epic_audit.public_self_homed_queues,
        epic_audit.private_under_public_epic,
    ],
)
def test_orphan_pass_checks_enumerate_when_not_handed_issues(check: Any) -> None:
    with patch("vergil_tooling.lib.epic_audit.open_issues", return_value=[]) as enum:
        assert check("org") == []
    enum.assert_called_once_with("org")


def _homes(repo: str) -> tuple[str, str]:
    # Public repos share the org's .github queues; "secret" self-homes.
    owner, bare = repo.split("/", 1)
    return (repo, bare) if bare == "secret" else (f"{owner}/.github", owner)


def test_orphan_drift_targets_intake_epic_of_home() -> None:
    issues = [_issue("org/tool", 5), _issue("org/secret", 3, public=False)]
    with patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes):
        orphans = epic_audit.orphan_drift("org", issues=issues)
    assert orphans == [
        epic_audit.Orphan(epics.IssueRef("org", "tool", 5), home="org/.github", name="org"),
        epic_audit.Orphan(epics.IssueRef("org", "secret", 3), home="org/secret", name="secret"),
    ]


def test_orphan_drift_skips_release_tracking_issue() -> None:
    issues = [
        _issue(
            "org/tool",
            9,
            title="release: 2.1.258",
            body="<!-- vrg-release:progress -->\n- [ ] tag\n",
        )
    ]
    with patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes) as qh:
        assert epic_audit.orphan_drift("org", issues=issues) == []
    qh.assert_not_called()


def test_orphan_drift_skips_archives_and_epics() -> None:
    issues = [
        _issue("org/.github", 1, labels=("epic",)),
        _issue("org/.github", 2, labels=("archive", "intake")),
        _issue("org/.github", 3, labels=("epic", "intake")),
    ]
    assert epic_audit.orphan_drift("org", issues=issues) == []


def test_orphan_drift_skips_unmanaged_repos() -> None:
    # The shared enumeration never reads an unmanaged repo, so nothing from it
    # can become an orphan; orphan_drift enumerates when not handed issues.
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["fork"]),
        patch("vergil_tooling.lib.epics.is_managed_repo", return_value=False),
        patch("vergil_tooling.lib.github.graphql") as gql,
    ):
        assert epic_audit.orphan_drift("org") == []
    gql.assert_not_called()


def test_orphan_drift_treats_body_parent_as_parented() -> None:
    issues = [
        _issue("org/tool", 5, body="Parent: org/.github#40\n"),
        _issue("org/tool", 6, native_parent=epics.IssueRef("org", ".github", 40)),
    ]
    with patch("vergil_tooling.lib.epics.is_epic", return_value=True):
        assert epic_audit.orphan_drift("org", issues=issues) == []


def test_orphan_drift_attaches_body_parent_that_is_not_an_epic() -> None:
    # A body Parent: naming a non-epic is not epic-parented: it is an orphan.
    issues = [_issue("org/tool", 5, body="Parent: org/tool#4\n")]
    with (
        patch("vergil_tooling.lib.epics.is_epic", return_value=False),
        patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes),
    ):
        orphans = epic_audit.orphan_drift("org", issues=issues)
    assert [o.ref.number for o in orphans] == [5]


def _intake_epic(home: str) -> epics.IssueRef:
    owner, repo = home.split("/", 1)
    return epics.IssueRef(owner, repo, 100 if repo == ".github" else 7)


def test_attach_orphans_attaches_each_under_its_intake_epic() -> None:
    orphans = [
        epic_audit.Orphan(epics.IssueRef("org", "tool", 5), home="org/.github", name="org"),
        epic_audit.Orphan(epics.IssueRef("org", "tool", 6), home="org/.github", name="org"),
        epic_audit.Orphan(epics.IssueRef("org", "secret", 3), home="org/secret", name="secret"),
    ]
    with (
        patch(
            "vergil_tooling.lib.epics.ensure_standing_epic",
            side_effect=lambda _kind, home, _name: _intake_epic(home),
        ) as ensure,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]) as cs,
        patch("vergil_tooling.lib.epics.add_child") as add,
    ):
        assert epic_audit.attach_orphans(orphans) == 3
    assert ensure.call_count == 2  # resolved once per home
    assert ensure.call_args_list[0].args == (epics.INTAKE, "org/.github", "org")
    assert cs.call_count == 2  # open-children count fetched once per epic
    assert [c.args for c in add.call_args_list] == [
        (epics.IssueRef("org", ".github", 100), epics.IssueRef("org", "tool", 5)),
        (epics.IssueRef("org", ".github", 100), epics.IssueRef("org", "tool", 6)),
        (epics.IssueRef("org", "secret", 7), epics.IssueRef("org", "secret", 3)),
    ]


def test_attach_orphans_is_idempotent() -> None:
    # After the attach, each issue has a native parent, so a re-run finds nothing.
    intake = epics.IssueRef("org", ".github", 100)
    before = [_issue("org/tool", 5)]
    with (
        patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes),
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=intake),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
        patch("vergil_tooling.lib.epics.add_child"),
    ):
        assert epic_audit.attach_orphans(epic_audit.orphan_drift("org", issues=before)) == 1
    after = [_issue("org/tool", 5, native_parent=intake)]
    assert epic_audit.orphan_drift("org", issues=after) == []


def test_attach_orphans_stops_at_cap() -> None:
    intake = epics.IssueRef("org", ".github", 100)
    full = [
        epics.ChildState(epics.IssueRef("org", "tool", 1000 + i), "OPEN", "c")
        for i in range(epics._ADHOC_ARCHIVE_SEGMENT_CAP)
    ] + [epics.ChildState(epics.IssueRef("org", "tool", 1), "CLOSED", "done")]
    orphans = [
        epic_audit.Orphan(epics.IssueRef("org", "tool", n), home="org/.github", name="org")
        for n in (5, 6, 7)
    ]
    with (
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=intake),
        patch("vergil_tooling.lib.epics.child_states", return_value=full),
        patch("vergil_tooling.lib.epics.add_child") as add,
        pytest.raises(epic_audit.StandingEpicFull) as excinfo,
    ):
        epic_audit.attach_orphans(orphans)
    add.assert_not_called()
    exc = excinfo.value
    assert exc.epic == intake
    assert exc.open_children == 95
    assert exc.pending == [o.ref for o in orphans]
    assert str(exc) == (
        "intake epic org/.github#100 is full (95 open); 3 orphan(s) not attached: "
        "org/tool#5, org/tool#6, org/tool#7. Run intake-review."
    )


def test_attach_orphans_stops_when_attaching_reaches_cap() -> None:
    intake = epics.IssueRef("org", ".github", 100)
    near = [
        epics.ChildState(epics.IssueRef("org", "tool", 1000 + i), "OPEN", "c")
        for i in range(epics._ADHOC_ARCHIVE_SEGMENT_CAP - 1)
    ]
    orphans = [
        epic_audit.Orphan(epics.IssueRef("org", "tool", n), home="org/.github", name="org")
        for n in (5, 6)
    ]
    with (
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=intake),
        patch("vergil_tooling.lib.epics.child_states", return_value=near),
        patch("vergil_tooling.lib.epics.add_child") as add,
        pytest.raises(epic_audit.StandingEpicFull) as excinfo,
    ):
        epic_audit.attach_orphans(orphans)
    assert add.call_count == 1  # the first fits; the second would exceed the cap
    assert excinfo.value.pending == [epics.IssueRef("org", "tool", 6)]


def test_retired_label_drift_lists_kind_labelled_issues() -> None:
    issues = [
        _issue("org/.github", 428, labels=("idea",), native_parent=epics.IssueRef("o", "g", 1)),
        _issue("org/tool", 5, labels=("triage", "research", "bug")),
        _issue("org/tool", 6, labels=("bug",)),
    ]
    drift = epic_audit.retired_label_drift("org", issues=issues)
    assert drift == [
        epic_audit.RetiredLabels(epics.IssueRef("org", ".github", 428), ("idea",)),
        epic_audit.RetiredLabels(epics.IssueRef("org", "tool", 5), ("research", "triage")),
    ]


def test_strip_retired_labels_keeps_existing_parent() -> None:
    # .github#428 is already routed under #139 and carries "idea": the strip
    # only removes the label — it never re-parents.
    drift = [epic_audit.RetiredLabels(epics.IssueRef("vergil-project", ".github", 428), ("idea",))]
    with (
        patch("vergil_tooling.lib.github.run") as run,
        patch("vergil_tooling.lib.epics.reparent_child") as reparent,
        patch("vergil_tooling.lib.epics.add_child") as add,
    ):
        assert epic_audit.strip_retired_labels(drift) == 1
    run.assert_called_once_with(
        "issue", "edit", "428", "--repo", "vergil-project/.github", "--remove-label", "idea"
    )
    reparent.assert_not_called()
    add.assert_not_called()


def test_strip_retired_labels_removes_every_retired_label() -> None:
    drift = [epic_audit.RetiredLabels(epics.IssueRef("o", "r", 5), ("research", "triage"))]
    with patch("vergil_tooling.lib.github.run") as run:
        epic_audit.strip_retired_labels(drift)
    run.assert_called_once_with(
        "issue", "edit", "5", "--repo", "o/r", "--remove-label", "research,triage"
    )


def test_body_only_parents_pairs_task_with_body_epic() -> None:
    issues = [
        _issue("org/tool", 5, body="Parent: org/.github#40\n"),  # body-only, epic -> pair
        _issue("org/tool", 6, body="Parent: org/tool#4\n"),  # body-only, not an epic
        _issue(
            "org/tool",
            7,
            body="Parent: org/.github#40\n",
            native_parent=epics.IssueRef("org", ".github", 40),
        ),  # already native
        _issue("org/tool", 8),  # no parent at all
    ]
    with patch("vergil_tooling.lib.epics.is_epic", side_effect=lambda ref: ref.repo == ".github"):
        pairs = epic_audit.body_only_parents("org", issues=issues)
    assert pairs == [(epics.IssueRef("org", "tool", 5), epics.IssueRef("org", ".github", 40))]


def test_nativize_parents_adds_native_link() -> None:
    task = epics.IssueRef("org", "tool", 5)
    epic = epics.IssueRef("org", ".github", 40)
    with patch("vergil_tooling.lib.epics.add_child") as add:
        assert epic_audit.nativize_parents([(task, epic)]) == 1
    add.assert_called_once_with(epic, task)


def test_public_self_homed_queues_lists_public_repo_queue_epics() -> None:
    issues = [
        _issue("org/lab", 2, labels=("epic", "intake")),  # public repo queue -> move
        _issue("org/lab", 3, labels=("epic", "planning")),  # public repo queue -> move
        _issue("org/lab", 4, labels=("epic", "ad-hoc")),  # ad-hoc, not a queue
        _issue("org/secret", 2, labels=("epic", "intake"), public=False),  # still private
        _issue("org/.github", 9, labels=("epic", "intake")),  # the org queue itself
        _issue("org/lab", 5, labels=("archive", "intake")),  # an archive, not live
    ]
    assert epic_audit.public_self_homed_queues("org", issues=issues) == [
        epics.IssueRef("org", "lab", 2),
        epics.IssueRef("org", "lab", 3),
    ]


def test_move_public_queues_reparents_then_closes() -> None:
    old = epics.IssueRef("org", "lab", 2)
    new = epics.IssueRef("org", ".github", 100)
    kids = [
        epics.ChildState(epics.IssueRef("org", "lab", 10), "OPEN", "a"),
        epics.ChildState(epics.IssueRef("org", "lab", 11), "CLOSED", "b"),
        epics.ChildState(epics.IssueRef("org", "lab", 12), "OPEN", "c"),
    ]
    order: list[str] = []

    def fake_children(epic: epics.IssueRef) -> list[epics.ChildState]:
        return kids if epic == old else []

    with (
        patch("vergil_tooling.lib.epics.queue_kind", return_value="intake"),
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=new) as ensure,
        patch("vergil_tooling.lib.epics.child_states", side_effect=fake_children),
        patch(
            "vergil_tooling.lib.epics.reparent_child",
            side_effect=lambda _p, t: order.append(f"move {t.number}"),
        ) as reparent,
        patch(
            "vergil_tooling.lib.github.run", side_effect=lambda *a: order.append(f"close {a[2]}")
        ) as run,
    ):
        assert epic_audit.move_public_queues([old], org="org") == 2
    ensure.assert_called_once_with(epics.INTAKE, "org/.github", "org")
    assert [c.args for c in reparent.call_args_list] == [
        (new, epics.IssueRef("org", "lab", 10)),
        (new, epics.IssueRef("org", "lab", 12)),
    ]
    run.assert_called_once_with(
        "issue",
        "close",
        "2",
        "--repo",
        "org/lab",
        "--comment",
        "Repo is now public; queue items moved to org/.github#100.",
    )
    assert order == ["move 10", "move 12", "close 2"]


def test_move_public_queues_targets_planning_for_planning_epic() -> None:
    old = epics.IssueRef("org", "lab", 3)
    with (
        patch("vergil_tooling.lib.epics.queue_kind", return_value="planning"),
        patch(
            "vergil_tooling.lib.epics.ensure_standing_epic",
            return_value=epics.IssueRef("org", ".github", 101),
        ) as ensure,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
        patch("vergil_tooling.lib.github.run"),
    ):
        assert epic_audit.move_public_queues([old], org="org") == 0
    assert ensure.call_args.args[0] is epics.PLANNING


def test_move_public_queues_rejects_non_queue_epic() -> None:
    with (
        patch("vergil_tooling.lib.epics.queue_kind", return_value=None),
        pytest.raises(ValueError, match="org/lab#4 is not a queue epic"),
    ):
        epic_audit.move_public_queues([epics.IssueRef("org", "lab", 4)], org="org")


def test_move_public_queues_stops_at_cap_without_closing() -> None:
    old = epics.IssueRef("org", "lab", 3)
    new = epics.IssueRef("org", ".github", 101)
    full = [
        epics.ChildState(epics.IssueRef("org", "x", i), "OPEN", "c")
        for i in range(epics._ADHOC_ARCHIVE_SEGMENT_CAP)
    ]
    mine = [epics.ChildState(epics.IssueRef("org", "lab", 10), "OPEN", "a")]
    with (
        patch("vergil_tooling.lib.epics.queue_kind", return_value="planning"),
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=new),
        patch(
            "vergil_tooling.lib.epics.child_states",
            side_effect=lambda epic: mine if epic == old else full,
        ),
        patch("vergil_tooling.lib.epics.reparent_child") as reparent,
        patch("vergil_tooling.lib.github.run") as run,
        pytest.raises(epic_audit.StandingEpicFull) as excinfo,
    ):
        epic_audit.move_public_queues([old], org="org")
    reparent.assert_not_called()
    run.assert_not_called()  # the old epic stays open: nothing half-moved
    assert str(excinfo.value) == (
        "planning epic org/.github#101 is full (95 open); 1 queue item(s) not moved: "
        "org/lab#10. Work the planning queue (epic-create)."
    )


def test_private_under_public_epic_reports_only() -> None:
    issues = [
        _issue(
            "org/secret",
            3,
            public=False,
            native_parent=epics.IssueRef("org", ".github", 40),
        ),  # private work under a public .github epic -> report
        _issue("org/secret", 4, public=False, body="Parent: org/.github#41\n"),  # body link
        _issue(
            "org/secret", 5, public=False, native_parent=epics.IssueRef("org", "secret", 2)
        ),  # self-homed -> fine
        _issue("org/secret", 6, public=False),  # unparented -> not this check
        _issue("org/tool", 7, native_parent=epics.IssueRef("org", ".github", 40)),  # public
    ]
    with (
        patch("vergil_tooling.lib.github.is_public", return_value=True),
        patch("vergil_tooling.lib.github.run") as run,
        patch("vergil_tooling.lib.epics.reparent_child") as reparent,
    ):
        slugs = epic_audit.private_under_public_epic("org", issues=issues)
    assert slugs == ["org/secret#3", "org/secret#4"]
    run.assert_not_called()
    reparent.assert_not_called()


def test_private_under_public_epic_silent_when_dotgithub_private() -> None:
    # A private .github means the whole org is private: everything homes there.
    issues = [
        _issue("org/secret", 3, public=False, native_parent=epics.IssueRef("org", ".github", 40))
    ]
    with patch("vergil_tooling.lib.github.is_public", return_value=False):
        assert epic_audit.private_under_public_epic("org", issues=issues) == []


def _one_pr_reffing_8() -> list[dict[str, object]]:
    return [
        {"number": 31, "repository": {"nameWithOwner": "org/repo"}, "url": "u", "body": "Ref #8"}
    ]


def test_task_drift_skips_pr_ref_to_queue_item() -> None:
    # A merged PR Ref'ing an intake/planning queue item is not slipped-task
    # drift: the queue parent (not a kind label) marks it.
    prs = _one_pr_reffing_8()

    def fake_read_json(*args: str) -> object:
        return prs if args[0] == "search" else {"state": "open", "title": "t", "body": "b"}

    intake = epics.IssueRef("org", ".github", 100)
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.epics.parent_of", return_value=intake) as parent,
        patch("vergil_tooling.lib.epics.queue_kind", return_value="intake"),
    ):
        assert epic_audit.task_drift("2026-06-01", org="org") == []
    parent.assert_called_once_with(epics.IssueRef("org", "repo", 8))


def test_task_drift_flags_task_under_finite_epic() -> None:
    prs = _one_pr_reffing_8()

    def fake_read_json(*args: str) -> object:
        return prs if args[0] == "search" else {"state": "open", "title": "t", "body": "b"}

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch(
            "vergil_tooling.lib.epics.parent_of",
            return_value=epics.IssueRef("org", ".github", 40),
        ),
        patch("vergil_tooling.lib.epics.queue_kind", return_value=None),
    ):
        assert epic_audit.task_drift("2026-06-01", org="org") == [TaskDrift("org/repo", 8, 31, "u")]


def test_stray_check_allows_queue_items_without_kind_labels() -> None:
    # A queue item is parented under the (epic-labelled) intake epic, so it
    # passes the parent check with no kind label; a kind label alone no longer
    # exempts an unparented issue.
    rows = [
        {"number": 50, "labels": []},  # under intake -> ok
        {"number": 51, "labels": [{"name": "idea"}]},  # kind label, no parent -> stray
    ]
    intake = epics.IssueRef("org", ".github", 100)
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch(
            "vergil_tooling.lib.epics.parent_of",
            side_effect=lambda ref: intake if ref.number == 50 else None,
        ),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
    ):
        assert epic_audit.stray_dotgithub_issue("org") == ["org/.github#51"]


def test_render_lists_orphan_pass_preview() -> None:
    out = epic_audit.render(
        [],
        [],
        org="org",
        window_days=30,
        orphans=[
            epic_audit.Orphan(epics.IssueRef("org", "tool", 5), home="org/.github", name="org")
        ],
        retired_labels=[epic_audit.RetiredLabels(epics.IssueRef("org", "tool", 6), ("idea",))],
        body_only=[(epics.IssueRef("org", "tool", 7), epics.IssueRef("org", ".github", 40))],
        public_queues=[epics.IssueRef("org", "lab", 2)],
        private_under_public=["org/secret#3"],
    )
    assert "No drift" not in out
    assert "- org/tool#5 → Epic (intake): org in org/.github" in out
    assert "- org/tool#6 — idea" in out
    assert "- org/tool#7 → org/.github#40" in out
    assert "- org/lab#2" in out
    assert (
        "**Private repo work under public .github epics** — an unsupported public → "
        "private flip? Resolve by hand (spec: Private repos and visibility):"
    ) in out
    assert "- org/secret#3" in out


def test_render_orphan_pass_sections_only_when_present() -> None:
    out = epic_audit.render(
        [], [], org="org", window_days=30, private_under_public=["org/secret#3"]
    )
    assert "Invariant violations" in out
    assert "Orphans" not in out
    assert "Retired" not in out


def test_render_closed_lists_private_under_public() -> None:
    out = epic_audit.render_closed(
        [], org="org", window_days=30, private_under_public=["org/secret#3"]
    )
    assert "No drift" not in out
    assert "Private repo work under public .github epics" in out
    assert "- org/secret#3" in out


# -- Deferred queue kind (epic .github#508) ------------------------------------


def test_public_self_homed_queues_includes_deferred() -> None:
    issues = [
        _issue("org/lab", 6, labels=("epic", "deferred")),  # public repo queue -> move
        _issue("org/secret", 6, labels=("epic", "deferred"), public=False),  # still private
        _issue("org/.github", 10, labels=("epic", "deferred")),  # the org queue itself
        _issue("org/lab", 7, labels=("archive", "deferred")),  # an archive, not live
    ]
    assert epic_audit.public_self_homed_queues("org", issues=issues) == [
        epics.IssueRef("org", "lab", 6),
    ]


def test_move_public_queues_moves_deferred_to_dotgithub_deferred() -> None:
    old = epics.IssueRef("org", "lab", 6)
    new = epics.IssueRef("org", ".github", 10)
    kid = epics.IssueRef("org", "lab", 20)
    with (
        patch("vergil_tooling.lib.epics.queue_kind", return_value="deferred"),
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=new) as ensure,
        patch(
            "vergil_tooling.lib.epics.child_states",
            side_effect=lambda e: [epics.ChildState(kid, "OPEN", "a")] if e == old else [],
        ),
        patch("vergil_tooling.lib.epics.reparent_child") as reparent,
        patch("vergil_tooling.lib.github.run"),
    ):
        assert epic_audit.move_public_queues([old], org="org") == 1
    ensure.assert_called_once_with(epics.DEFERRED, "org/.github", "org")
    reparent.assert_called_once_with(new, kid)


def test_queue_full_hint_deferred() -> None:
    assert epic_audit._QUEUE_FULL_HINT["deferred"] == "Run deferred-review."
    # Every queue kind has a hint, so StandingEpicFull never KeyErrors.
    assert set(epic_audit._QUEUE_FULL_HINT) == set(epics.QUEUE_KIND_BY_KEY)
