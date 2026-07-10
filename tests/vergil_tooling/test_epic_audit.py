"""Tests for vergil_tooling.lib.epic_audit."""

from __future__ import annotations

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


def test_task_drift_skips_operational_and_intake_refs() -> None:
    # An operational task (closes on an Outcome comment) and an intake issue
    # (triage/idea/research) are never PR-tracked tasks — a merged PR Ref'ing
    # either is not slipped-task drift.
    prs = [
        {
            "number": 30,
            "repository": {"nameWithOwner": "org/repo"},
            "url": "u30",
            "body": "Ref #7",
        },
        {
            "number": 31,
            "repository": {"nameWithOwner": "org/repo"},
            "url": "u31",
            "body": "Ref #8",
        },
    ]

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        label = "validation" if args[2] == "7" else "idea"
        return {"state": "open", "title": "t", "body": "b", "labels": [{"name": label}]}

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
        assert epic_audit.task_drift("2026-06-01", org="org") == []


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

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json):
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


# -- closed-epic-with-open-child invariant + reopen remediation (issue #2259) --


def test_closed_epic_open_child_flags_closed_epic_with_open_child() -> None:
    listing = [
        {"number": 19, "title": "MQ Guides", "labels": []},  # has an open child -> violation
        {"number": 15, "title": "Cockpit", "labels": []},  # all children closed -> ok
        {"number": 99, "title": "Ad hoc", "labels": [{"name": "ad-hoc"}]},  # perpetual -> skipped
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


def test_closed_epic_open_child_skips_perpetual_before_fetching_children() -> None:
    # A perpetual (ad-hoc/standing) epic is skipped without a children lookup.
    listing = [{"number": 99, "title": "Ad hoc", "labels": [{"name": "standing"}]}]
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
