"""Tests for vergil_tooling.lib.epic_audit."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.lib import (
    epic_audit,
    epic_chain,
    epic_docs,
    epics,
    github,
    proxies,
    release,
    roadmap,
    trust,
)
from vergil_tooling.lib.epic_audit import TaskDrift
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Mapping

# The day the task_drift tests run on: inside the first week of their 2026-06-01
# window, so the merged-PR search is a single open-ended slice.
_TODAY = date(2026, 6, 1)
_AUDIT = "vergil_tooling.lib.epic_audit"


def _drift(
    prs: Any,
    org: str,
    *,
    stops: trust.BatchStops | None = None,
    items: dict[str, Any] | None = None,
) -> list[TaskDrift]:
    """task_drift over the search's *prs*, each PR body read through the real trust layer.

    Each PR's trusted body is its search body, by the trusted human, unless
    *items* (keyed ``owner/name#N``) gives that PR's item answer.
    """
    answers = {
        (str(pr["repository"]["nameWithOwner"]), int(pr["number"])): trust_stubs.item(
            body=str(pr.get("body") or ""), typename="PullRequest"
        )
        for pr in (prs if isinstance(prs, list) else [])
        if (pr.get("repository") or {}).get("nameWithOwner")
    }
    for key, answer in (items or {}).items():
        repo, _, number = key.rpartition("#")
        answers[(repo, int(number))] = answer
    with patch("vergil_tooling.lib.github.graphql", side_effect=trust_stubs.FakeItems(answers)):
        return epic_audit.task_drift(
            "2026-06-01",
            today=_TODAY,
            org=org,
            stops=trust.BatchStops() if stops is None else stops,
            authors=trust_stubs.AUTHORS,
        )


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
        {  # several linkage lines, none of them Closes -> nothing to check
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
        result = _drift(prs, "vergil-project")
    assert result == [TaskDrift("vergil-project/vergil-tooling", 1947, 1948, "u1948")]


def _drift_with(
    prs: list[dict[str, Any]], open_tasks: set[str]
) -> tuple[list[TaskDrift], list[str]]:
    """Run task_drift over *prs*; *open_tasks* are the ``repo#N`` refs still open."""
    viewed: list[str] = []

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        ref = f"{args[args.index('--repo') + 1]}#{args[2]}"
        viewed.append(ref)
        state = "open" if ref in open_tasks else "closed"
        return {"state": state, "title": f"feat: task {args[2]}", "body": "task body"}

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        result = _drift(prs, "vergil-project")
    return result, viewed


def test_task_drift_checks_every_issue_of_a_batch_pr() -> None:
    # A batch PR closes several tasks, one Closes line each: every one is
    # checked, and each one still open is drift (epic .github#1019).
    repo = "vergil-project/vergil-tooling"
    prs = [
        {
            "number": 50,
            "repository": {"nameWithOwner": repo},
            "url": "u50",
            "body": "## Issue Linkage\n\nCloses #10\nCloses #11\nCloses #12\n",
        },
    ]
    result, viewed = _drift_with(prs, {f"{repo}#10", f"{repo}#12"})
    assert viewed == [f"{repo}#10", f"{repo}#11", f"{repo}#12"]
    assert result == [TaskDrift(repo, 10, 50, "u50"), TaskDrift(repo, 12, 50, "u50")]


def test_task_drift_ignores_ref_lines_in_a_multi_linkage_body() -> None:
    # In a body with more than one linkage line, only the Closes lines name
    # tasks the PR closes; a Ref line is never acted on.
    repo = "vergil-project/vergil-tooling"
    prs = [
        {
            "number": 51,
            "repository": {"nameWithOwner": repo},
            "url": "u51",
            "body": "Closes #20\nRef #21\nCloses vergil-project/.github#22\n",
        },
    ]
    result, viewed = _drift_with(prs, {f"{repo}#20", f"{repo}#21", "vergil-project/.github#22"})
    assert viewed == [f"{repo}#20", "vergil-project/.github#22"]
    assert result == [
        TaskDrift(repo, 20, 51, "u51"),
        TaskDrift("vergil-project/.github", 22, 51, "u51"),
    ]


@pytest.mark.parametrize("keyword", ["Ref", "Closes"])
def test_task_drift_single_issue_unchanged(keyword: str) -> None:
    # Review focus 5: a body with one linkage line is handled as today, Ref
    # or Closes alike.
    repo = "vergil-project/vergil-tooling"
    prs = [
        {
            "number": 60,
            "repository": {"nameWithOwner": repo},
            "url": "u60",
            "body": f"## Issue Linkage\n\n{keyword} #59\n",
        },
    ]
    result, viewed = _drift_with(prs, {f"{repo}#59"})
    assert viewed == [f"{repo}#59"]
    assert result == [TaskDrift(repo, 59, 60, "u60")]


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

    layer = trust_stubs.FakeItems(
        {
            ("vergil-project/vergil-containers", 373): trust_stubs.item(
                body="<!-- vrg-release:progress -->\n- [x] tag\n"
            ),
        },
        strict=True,
    )
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.github.graphql", side_effect=layer),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        result = epic_audit.task_drift(
            "2026-06-01",
            today=_TODAY,
            org="vergil-project",
            stops=trust.BatchStops(),
            authors=trust_stubs.AUTHORS,
        )
    assert result == []
    # The release signals count only once the layer trusts the body.
    assert layer.reads == [("vergil-project/vergil-containers", 373)]


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
        result = _drift(prs, "vergil-project")
    assert looked_up["repo"] == "vergil-project/vergil-tooling"
    assert result == [TaskDrift("vergil-project/vergil-tooling", 42, 7, "u7")]


def test_task_drift_skips_unresolvable_task(capsys: pytest.CaptureFixture[str]) -> None:
    # A ref to a task this run can't see (cross-org or deleted) must be
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
        result = _drift(prs, "logical-minds-foundry")
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
        assert _drift(prs, "logical-minds-foundry") == []


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
        assert _drift(prs, "org") == []
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
        result = _drift(prs, "org")
    assert result == [TaskDrift("org/repo", 41, 40, "u40")]


def test_task_drift_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"x": 1}):
        assert _drift([], "vergil-project") == []


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
        assert _drift(prs, "vergil-project") == []


def _epic_drift(org: str, stops: trust.BatchStops | None = None) -> list[roadmap.EpicSummary]:
    """epic_drift of *org*, judged with the stub authors."""
    return epic_audit.epic_drift(
        org, stops=trust.BatchStops() if stops is None else stops, authors=trust_stubs.AUTHORS
    )


def _closed_epic_open_child(
    org: str, *, stops: trust.BatchStops | None = None, **kwargs: Any
) -> list[epic_audit.EpicOpenChildViolation]:
    """closed_epic_open_child of *org*, each open child's events read through the real layer.

    Unless the test patches ``github.graphql`` itself, every child was last
    closed by the trusted human, so no reopen is judged.
    """
    with patch("vergil_tooling.lib.github.graphql", side_effect=trust_stubs.FakeItems()):
        return epic_audit.closed_epic_open_child(
            org,
            stops=trust.BatchStops() if stops is None else stops,
            authors=trust_stubs.AUTHORS,
            **kwargs,
        )


def test_epic_drift_flags_all_done_open_epics() -> None:
    summaries = [
        roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40"),  # all done
        roadmap.EpicSummary(7, "WIP", "2026-06-01", None, (), 3, 1, "u7"),  # not all done
        roadmap.EpicSummary(8, "Empty", "2026-06-01", None, (), 0, 0, "u8"),  # no children
    ]
    closed = [epics.ChildState(epics.IssueRef("org", "tool", 1), "CLOSED")]
    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=summaries),
        patch("vergil_tooling.lib.epics.child_states", return_value=closed),
        patch("vergil_tooling.lib.github.graphql", side_effect=trust_stubs.FakeItems()),
    ):
        result = _epic_drift("org")
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


def test_invariant1_reports_every_non_dotgithub_epic() -> None:
    # Every epic outside .github is a violation, whatever its repo's visibility,
    # and no visibility field is read (epic vergil-project/.github#1476).
    issues = [{"number": 4, "repository": {"nameWithOwner": "vergil-project/lab"}}]
    with patch("vergil_tooling.lib.github.read_json", return_value=issues) as mock_search:
        assert epic_audit.epic_outside_dotgithub("vergil-project") == ["vergil-project/lab#4"]
    mock_search.assert_called_once()
    assert not any("visibility" in str(arg) for arg in mock_search.call_args.args)


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
        strays = epic_audit.stray_dotgithub_issue("org")
    assert strays == []  # an archive is not a stray


# -- closed-epic-with-open-child invariant + reopen remediation (issue #2259) --


def _record(  # noqa: PLR0913 - one field per EpicRecord key
    number: int,
    *,
    state: str = "CLOSED",
    title: str = "Epic: x",
    labels: tuple[str, ...] = ("epic",),
    body: str | None = "",
    reason: str | None = None,
    created: str = "2026-09-01T00:00:00Z",
) -> epic_docs.EpicRecord:
    return epic_docs.EpicRecord(
        number=number,
        title=title,
        state=state,
        stateReason=reason,
        createdAt=created,
        labels=list(labels),
        body=body,
    )


def test_closed_epic_open_child_flags_closed_epic_with_open_child() -> None:
    listing = [
        _record(19, title="MQ Guides"),  # has an open child -> violation
        _record(15, title="Cockpit"),  # all children closed -> ok
        _record(99, title="Ad hoc", labels=("epic", "ad-hoc")),  # perpetual -> skipped
        _record(20, state="OPEN"),  # open -> not checked
    ]

    def fake_child_states(epic: epics.IssueRef) -> list[epics.ChildState]:
        if epic.number == 19:
            return [
                epics.ChildState(epics.IssueRef("org", "lab", 462), "OPEN"),
                epics.ChildState(epics.IssueRef("org", "lab", 463), "CLOSED"),
            ]
        if epic.number == 20:
            msg = "an open epic is never checked"
            raise AssertionError(msg)
        return [epics.ChildState(epics.IssueRef("org", "lab", 1), "CLOSED")]

    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=listing),
        patch("vergil_tooling.lib.epics.child_states", side_effect=fake_child_states),
    ):
        result = _closed_epic_open_child("org")
    assert result == [(epics.IssueRef("org", ".github", 19), (epics.IssueRef("org", "lab", 462),))]


@pytest.mark.parametrize("kind_label", ["ad-hoc", "intake", "planning"])
def test_closed_epic_open_child_skips_perpetual_before_fetching_children(
    kind_label: str,
) -> None:
    # A perpetual standing epic (ad-hoc, intake, planning) is skipped without a
    # children lookup.
    listing = [_record(99, title="Standing", labels=("epic", kind_label))]
    child_states = MagicMock()
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=listing),
        patch("vergil_tooling.lib.epics.child_states", child_states),
    ):
        assert _closed_epic_open_child("org") == []
    child_states.assert_not_called()


def test_closed_epic_open_child_keeps_archived_epics() -> None:
    # Only its own perpetual-epic skip applies: an [archived] epic is checked.
    listing = [_record(7, title="[archived] Old", body=None)]
    kid = epics.IssueRef("org", "lab", 8)
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=listing),
        patch(
            "vergil_tooling.lib.epics.child_states", return_value=[epics.ChildState(kid, "OPEN")]
        ),
    ):
        assert _closed_epic_open_child("org") == [(epics.IssueRef("org", ".github", 7), (kid,))]


def test_closed_epic_open_child_scopes_to_home() -> None:
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=[]) as mock_list,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        _closed_epic_open_child("org", home="org/lab")
    # Lists the home, not <org>/.github, with no skips applied.
    mock_list.assert_called_once_with("org/lab", apply_skips=False)


def test_closed_epic_open_child_uses_a_given_listing() -> None:
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics") as mock_list,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        assert _closed_epic_open_child("org", listing=[_record(1)]) == []
    mock_list.assert_not_called()


def test_reopen_remediation_is_gone() -> None:
    # Closed is closed (.github#1609): the audit has no reopen fix to call.
    assert not hasattr(epic_audit, "reopen_epics_with_open_children")


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


def test_render_closed_reports_closed_epic_with_open_child_for_a_human() -> None:
    out = epic_audit.render_closed(
        [],
        org="org",
        window_days=30,
        closed_epic_open_children=[
            (epics.IssueRef("org", ".github", 19), (epics.IssueRef("org", "lab", 462),))
        ],
    )
    assert "## Needs a human (report only)" in out
    assert "Closed epics with open children" in out
    assert "- org/.github#19 — open children: org/lab#462" in out
    assert "Reopened" not in out


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


def _close_drift_graph(monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    """The open task vergil-tooling#1947 and the open epic .github#40, its chain the head alone."""
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(epics.IssueRef("vergil-project", "vergil-tooling", 1947), title="task")
    graph.add_issue(
        epics.IssueRef("vergil-project", ".github", 40), title="Convention", labels=["epic"]
    )
    return graph


def test_close_drift_closes_tasks_in_repo_and_epics_in_dot_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _close_drift_graph(monkeypatch)
    task, epic = (
        epics.IssueRef("vergil-project", "vergil-tooling", 1947),
        epics.IssueRef("vergil-project", ".github", 40),
    )
    drift = [TaskDrift("vergil-project/vergil-tooling", 1947, 1948, "u1948")]
    summaries = [roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40")]
    closed = epic_audit.close_drift(drift, summaries, org="vergil-project")
    assert closed == ["vergil-project/vergil-tooling#1947", "vergil-project/.github#40"]
    assert [event[:2] for event in graph.events] == [
        ("close", task),
        ("close", epic),
        ("comment", epic),
    ]
    assert "PR #1948 merged" in graph.comments[task][0]
    assert "all 9 child tasks" in graph.comments[epic][0]


def test_close_drift_closes_whole_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every close closes the chain (spec §4.6 of .github#1609): the head first,
    # then each continuation, and the comment goes on the head.
    graph = _close_drift_graph(monkeypatch)
    head = epics.IssueRef("vergil-project", ".github", 40)
    c2 = graph.add_continuation(epics.IssueRef("vergil-project", ".github", 41), head, head)
    c3 = graph.add_continuation(epics.IssueRef("vergil-project", ".github", 42), head, c2, k=3)
    graph.clear_log()
    summaries = [roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40")]
    closed = epic_audit.close_drift([], summaries, org="vergil-project")
    assert closed == [head.slug, c2.slug, c3.slug]
    assert [event[:2] for event in graph.events] == [
        ("close", head),
        ("close", c2),
        ("close", c3),
        ("comment", head),
    ]
    assert all(graph.issues[ref].state == "CLOSED" for ref in (head, c2, c3))


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


def _blockers(
    table: Mapping[int, list[epics.IssueRef] | Exception],
) -> Any:
    """A ``blockers_of`` fake: each task's blockers, or the exception its read raises."""

    def blockers_of(ref: epics.IssueRef) -> list[epics.IssueRef]:
        found = table.get(ref.number, [])
        if isinstance(found, Exception):
            raise found
        return found

    return blockers_of


# Blocker #100 is still open, #101 closed.
_OPEN_DEP = epics.IssueRef("org", "repo", 100)
_CLOSED_DEP = epics.IssueRef("org", "repo", 101)


def _dep_state(ref: epics.IssueRef) -> str:
    return "OPEN" if ref == _OPEN_DEP else "CLOSED"


# The status readers list children with their native blocked-by lists (#5109).
_LISTING = "vergil_tooling.lib.epics.child_states_with_blockers"


def _listed(children: list[epics.ChildState]) -> list[epics.ChildState]:
    """*children* as the listing gives them: native children with no native blockers."""
    return [replace(child, native_blockers=()) for child in children]


def _native(*blockers: tuple[str, str, int, str]) -> tuple[epics.BlockerState, ...]:
    return tuple(
        epics.BlockerState(epics.IssueRef(owner, repo, number), state)
        for owner, repo, number, state in blockers
    )


def test_child_status_counts_native_blockers() -> None:
    epic = epics.IssueRef("org", ".github", 99)
    runnable = epics.IssueRef("org", "repo", 10)
    native_open = epics.IssueRef("org", "repo", 11)
    cross_org = epics.IssueRef("org", "repo", 12)
    body_open = epics.IssueRef("org", "repo", 13)
    children = [
        epics.ChildState(runnable, "OPEN", native_blockers=_native(("org", "repo", 1, "CLOSED"))),
        epics.ChildState(native_open, "OPEN", native_blockers=_native(("org", "repo", 2, "OPEN"))),
        epics.ChildState(
            cross_org, "OPEN", native_blockers=_native(("other-org", "lib", 3, "OPEN"))
        ),
        # A closed native blocker does not hide an open Blocked-by line.
        epics.ChildState(body_open, "OPEN", native_blockers=_native(("org", "repo", 4, "CLOSED"))),
    ]
    with (
        patch(_LISTING, return_value=children) as listing,
        patch("vergil_tooling.lib.epics.blockers_of", side_effect=_blockers({13: [_OPEN_DEP]})),
        patch("vergil_tooling.lib.epics._issue_state", side_effect=_dep_state),
        patch("vergil_tooling.lib.epics.native_blockers_of") as own_read,
    ):
        status = epic_audit.child_status(epic, stops=trust.BatchStops())
    assert status.runnable == (runnable,)
    assert status.blocked == (native_open, cross_org, body_open)
    own_read.assert_not_called()  # the listing already read every native list
    # Only open children are wanted, so a closed child's list never fails the read.
    wanted = listing.call_args.kwargs["wanted"]
    assert wanted(epics.ChildState(runnable, "OPEN"))
    assert not wanted(epics.ChildState(runnable, "CLOSED"))


def test_child_status_reads_a_reflink_childs_native_blockers() -> None:
    epic = epics.IssueRef("org", ".github", 99)
    blocked = epics.IssueRef("org", "repo", 10)
    runnable = epics.IssueRef("org", "repo", 11)
    children = [epics.ChildState(blocked, "OPEN"), epics.ChildState(runnable, "OPEN")]

    def native_blockers_of(ref: epics.IssueRef) -> tuple[epics.BlockerState, ...]:
        return _native(("org", "repo", 2, "OPEN" if ref == blocked else "CLOSED"))

    with (
        patch(_LISTING, return_value=children),
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]),
        patch("vergil_tooling.lib.epics.native_blockers_of", side_effect=native_blockers_of),
    ):
        status = epic_audit.child_status(epic, stops=trust.BatchStops())
    assert status.runnable == (runnable,)
    assert status.blocked == (blocked,)


def test_child_status_refuses_an_incomplete_native_list() -> None:
    epic = epics.IssueRef("org", ".github", 99)
    failure = epics.NativeBlockersReadError("org/repo#10 has 2 native blockers but only 1")
    with (
        patch(_LISTING, return_value=[epics.ChildState(epics.IssueRef("org", "repo", 10), "OPEN")]),
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]),
        patch("vergil_tooling.lib.epics.native_blockers_of", side_effect=failure),
        pytest.raises(epics.NativeBlockersReadError, match="only 1"),
    ):
        epic_audit.child_status(epic, stops=trust.BatchStops())


def _sub_issue(number: int, state: str, total: int) -> dict[str, Any]:
    """A sub-issue node whose ``blockedBy`` list counts *total* but holds none."""
    return {
        "number": number,
        "state": state,
        "title": f"t{number}",
        "closedAt": None,
        "repository": {"name": "repo", "owner": {"login": "org"}},
        "blockedBy": {"totalCount": total, "nodes": []},
    }


def test_child_status_through_the_listing_skips_only_a_closed_childs_list() -> None:
    # Wired through the real listing: a closed child's incomplete list is not
    # read, an open child's fails the whole status, never "runnable".
    epic = epics.IssueRef("org", ".github", 99)

    def listing(*nodes: dict[str, Any]) -> dict[str, Any]:
        return {"node": {"subIssues": {"nodes": list(nodes)}}}

    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch(
            "vergil_tooling.lib.github.graphql",
            return_value=listing(_sub_issue(1, "CLOSED", 1), _sub_issue(2, "OPEN", 0)),
        ),
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]),
    ):
        status = epic_audit.child_status(epic, stops=trust.BatchStops())
    assert status.runnable == (epics.IssueRef("org", "repo", 2),)
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=listing(_sub_issue(2, "OPEN", 1))),
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]),
        pytest.raises(epics.NativeBlockersReadError),
    ):
        epic_audit.child_status(epic, stops=trust.BatchStops())


def test_operational_status_counts_native_blockers() -> None:
    epic = epics.IssueRef("org", ".github", 115)
    runnable = epics.IssueRef("org", "repo", 7)
    blocked = epics.IssueRef("org", "repo", 8)
    plain = epics.IssueRef("org", "repo", 5)
    children = [
        epics.ChildState(runnable, "OPEN", native_blockers=_native(("org", "repo", 1, "CLOSED"))),
        epics.ChildState(blocked, "OPEN", native_blockers=_native(("other-org", "x", 2, "OPEN"))),
        epics.ChildState(plain, "OPEN", native_blockers=()),
    ]

    def kind(ref: epics.IssueRef) -> str | None:
        return None if ref == plain else "deployment"

    with (
        patch(_LISTING, return_value=children) as listing,
        patch("vergil_tooling.lib.epics.operational_kind", side_effect=kind) as kinds,
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]),
    ):
        status = epic_audit.operational_status(epic, stops=trust.BatchStops())
        wanted = listing.call_args.kwargs["wanted"]
        # Only an open operational child is wanted: no other child's list is read.
        assert wanted(epics.ChildState(runnable, "OPEN"))
        assert not wanted(epics.ChildState(plain, "OPEN"))
        assert not wanted(epics.ChildState(epics.IssueRef("org", "repo", 9), "CLOSED"))
    assert status.runnable == (runnable,)
    assert status.blocked == (blocked,)
    assert status.by_kind == {runnable: "deployment", blocked: "deployment"}
    # Each child's kind is read once, however often the filter asks.
    assert sorted(call.args[0].number for call in kinds.call_args_list) == [5, 7, 8]


def test_operational_status_refuses_an_incomplete_native_list() -> None:
    epic = epics.IssueRef("org", ".github", 115)
    failure = epics.NativeBlockersReadError("unreadable native blocker of org/repo#7")
    with (
        patch(_LISTING, return_value=[epics.ChildState(epics.IssueRef("org", "repo", 7), "OPEN")]),
        patch("vergil_tooling.lib.epics.operational_kind", return_value="validation"),
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]),
        patch("vergil_tooling.lib.epics.native_blockers_of", side_effect=failure),
        pytest.raises(epics.NativeBlockersReadError, match="unreadable"),
    ):
        epic_audit.operational_status(epic, stops=trust.BatchStops())


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

    stops = trust.BatchStops()
    with (
        patch(_LISTING, return_value=_listed(children)),
        patch("vergil_tooling.lib.epics.operational_kind", side_effect=kind),
        patch(
            "vergil_tooling.lib.epics.blockers_of",
            side_effect=_blockers({7: [_CLOSED_DEP], 8: [_CLOSED_DEP, _OPEN_DEP]}),
        ),
        patch("vergil_tooling.lib.epics._issue_state", side_effect=_dep_state),
    ):
        status = epic_audit.operational_status(epic, stops=stops)
    assert status.runnable == (val_runnable,)
    assert status.blocked == (val_blocked,)
    assert status.stopped == ()
    assert status.pending == (val_runnable, val_blocked)
    assert stops.stopped == ()


def test_operational_status_tags_kind() -> None:
    epic = epics.IssueRef("org", ".github", 124)
    val = epics.IssueRef("org", "repo", 7)
    dep = epics.IssueRef("org", "repo", 8)
    children = [epics.ChildState(val, "OPEN"), epics.ChildState(dep, "OPEN")]

    def kind(ref: epics.IssueRef) -> str | None:
        return "validation" if ref.number == 7 else "deployment"

    with (
        patch(_LISTING, return_value=_listed(children)),
        patch("vergil_tooling.lib.epics.operational_kind", side_effect=kind),
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]),
    ):
        status = epic_audit.operational_status(epic, stops=trust.BatchStops())
    # keyed by IssueRef (cross-repo safe), not bare number
    assert status.by_kind[val] == "validation"
    assert status.by_kind[dep] == "deployment"


# Spec §5.3 of epic vergil-project/.github#1467: a batch run stops the child (#4971).
_UNTRUSTED_CHILD = trust.Skipped(
    "org/repo", 7, "issue", "drive-by", "2026-11-01T00:00:00Z", "body", trust.UNTRUSTED
)
_UNREADABLE_CHILD = trust.Skipped("org/repo", 8, "issue", None, "", "body", trust.READ_FAILURE)
_STOPPING = {
    7: trust.UntrustedContentError(_UNTRUSTED_CHILD, "the issue body is untrusted"),
    8: trust.TrustReadError("read_issue org/repo#8", "the body is missing"),
}


def test_operational_status_stops_only_the_untrusted_child() -> None:
    epic = epics.IssueRef("org", ".github", 115)
    children = [epics.ChildState(epics.IssueRef("org", "repo", n), "OPEN") for n in (7, 8, 10, 11)]
    stops = trust.BatchStops()
    with (
        patch(_LISTING, return_value=_listed(children)),
        patch("vergil_tooling.lib.epics.operational_kind", return_value="validation"),
        patch(
            "vergil_tooling.lib.epics.blockers_of",
            side_effect=_blockers({**_STOPPING, 11: [_OPEN_DEP]}),
        ),
        patch("vergil_tooling.lib.epics._issue_state", side_effect=_dep_state),
    ):
        status = epic_audit.operational_status(epic, stops=stops)
    assert [ref.number for ref in status.runnable] == [10]
    assert [ref.number for ref in status.blocked] == [11]
    assert [ref.number for ref in status.stopped] == [7, 8]
    # A stopped child is still pending, with its kind.
    assert [ref.number for ref in status.pending] == [10, 11, 7, 8]
    assert status.by_kind[epics.IssueRef("org", "repo", 7)] == "validation"
    assert stops.stopped == (_UNTRUSTED_CHILD, _UNREADABLE_CHILD)


def test_operational_status_other_failures_still_raise() -> None:
    epic = epics.IssueRef("org", ".github", 115)
    failure = github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 502")
    with (
        patch(
            _LISTING,
            return_value=[epics.ChildState(epics.IssueRef("org", "repo", 7), "OPEN")],
        ),
        patch("vergil_tooling.lib.epics.operational_kind", return_value="validation"),
        patch("vergil_tooling.lib.epics.blockers_of", side_effect=failure),
        pytest.raises(github.GitHubAPIError, match="502"),
    ):
        epic_audit.operational_status(epic, stops=trust.BatchStops())


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
    with (
        patch(_LISTING, return_value=_listed(children)),
        patch(
            "vergil_tooling.lib.epics.blockers_of",
            side_effect=_blockers({10: [_CLOSED_DEP], 11: [_OPEN_DEP]}),
        ),
        patch("vergil_tooling.lib.epics._issue_state", side_effect=_dep_state),
    ):
        status = epic_audit.child_status(epic, stops=trust.BatchStops())
    # Every open child is classified, plain tasks included — no kind gating.
    assert status.runnable == (plain_runnable, op_runnable)
    assert status.blocked == (plain_blocked,)
    assert status.stopped == ()


def test_child_status_empty_when_no_open_children() -> None:
    epic = epics.IssueRef("org", ".github", 99)
    children = [epics.ChildState(epics.IssueRef("org", "repo", 1), "CLOSED")]
    with (
        patch(_LISTING, return_value=_listed(children)),
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[]) as mock_blockers,
    ):
        status = epic_audit.child_status(epic, stops=trust.BatchStops())
    assert status.runnable == ()
    assert status.blocked == ()
    mock_blockers.assert_not_called()  # closed children are never queried


def test_child_status_stops_only_the_untrusted_child() -> None:
    epic = epics.IssueRef("org", ".github", 99)
    children = [epics.ChildState(epics.IssueRef("org", "repo", n), "OPEN") for n in (7, 8, 10)]
    stops = trust.BatchStops()
    with (
        patch(_LISTING, return_value=_listed(children)),
        patch("vergil_tooling.lib.epics.blockers_of", side_effect=_blockers(_STOPPING)),
    ):
        status = epic_audit.child_status(epic, stops=stops)
    # A stopped child is never runnable: its dependencies are unknown.
    assert [ref.number for ref in status.runnable] == [10]
    assert status.blocked == ()
    assert [ref.number for ref in status.stopped] == [7, 8]
    assert stops.stopped == (_UNTRUSTED_CHILD, _UNREADABLE_CHILD)
    assert stops.exit_code() == 1


def test_operational_pending_collects_only_epics_with_open_validations() -> None:
    val = epics.IssueRef("org", "repo", 7)
    stopped = epics.IssueRef("org", "repo", 9)

    def fake_status(epic: epics.IssueRef, *, stops: trust.BatchStops) -> Any:
        if epic.number == 115:
            return epic_audit.OperationalStatus(epic, (val,), (), {val: "validation"})
        if epic.number == 116:  # only a stopped child: still pending
            return epic_audit.OperationalStatus(epic, (), (), {stopped: "validation"}, (stopped,))
        return epic_audit.OperationalStatus(epic, (), (), {})  # nothing pending

    stops = trust.BatchStops()
    with (
        patch(
            "vergil_tooling.lib.epic_audit.roadmap.gather",
            return_value=[MagicMock(number=115), MagicMock(number=116), MagicMock(number=200)],
        ),
        patch(
            "vergil_tooling.lib.epic_audit.operational_status", side_effect=fake_status
        ) as status,
    ):
        pending = epic_audit.operational_pending("org", stops=stops)
    assert [s.epic.number for s in pending] == [115, 116]
    assert all(call.kwargs["stops"] is stops for call in status.call_args_list)


def _run_closed_operational(
    monkeypatch: pytest.MonkeyPatch,
    search: list[dict[str, Any]],
    comments: dict[int, list[dict[str, Any]]],
) -> list[str]:
    """Run the invariant over *search* (the closed validation issues).

    Each issue's comments are read through the trust layer, *comments* giving
    each issue's comment nodes.
    """
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    items = trust_stubs.FakeItems(
        {
            (str(row["repository"]["nameWithOwner"]), int(row["number"])): trust_stubs.item(
                nodes=comments.get(int(row["number"]), [])
            )
            for row in search
            if row["repository"]
        },
        strict=True,
    )

    def fake_read_json(*args: str) -> object:
        assert args[0] == "search", args
        # invariant loops each operational label; return the fixture once
        return search if args[5] == "validation" else []

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
        patch(f"{_AUDIT}.load_trusted_authors", return_value=trust_stubs.AUTHORS) as load,
    ):
        result = epic_audit.closed_operational_without_success("org")
    load.assert_called_once_with()
    return result


def test_closed_operational_without_success_flags_missing_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = [
        {"number": 120, "repository": {"nameWithOwner": "org/.github"}},  # has PASS -> ok
        {"number": 55, "repository": {"nameWithOwner": "org/repo"}},  # no PASS -> flagged
        {"number": 77, "repository": {}},  # no repo -> skipped
    ]
    comments = {
        120: trust_stubs.comments("ran it\n- Outcome: PASS"),
        55: trust_stubs.comments("closed early; no result recorded"),
    }
    assert _run_closed_operational(monkeypatch, search, comments) == ["org/repo#55"]


def test_closed_validation_pass_marker_excludes_unresolved_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scaffold's unresolved "Outcome: PASS / FAIL" line must NOT read as a pass.
    search = [{"number": 1, "repository": {"nameWithOwner": "org/repo"}}]
    comments = {1: trust_stubs.comments("- Outcome: PASS / FAIL")}
    assert _run_closed_operational(monkeypatch, search, comments) == ["org/repo#1"]


def test_success_marker_accepts_success_and_legacy_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    search = [
        {"number": 1, "repository": {"nameWithOwner": "org/repo"}},  # SUCCESS -> ok
        {"number": 2, "repository": {"nameWithOwner": "org/repo"}},  # legacy PASS -> ok
        {"number": 3, "repository": {"nameWithOwner": "org/repo"}},  # neither -> flagged
    ]
    comments = {
        1: trust_stubs.comments("- Outcome: SUCCESS"),
        2: trust_stubs.comments("- Outcome: PASS"),
        3: trust_stubs.comments("closed early"),
    }
    assert _run_closed_operational(monkeypatch, search, comments) == ["org/repo#3"]


def test_forged_outcome_does_not_hide_hand_closed_task(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A success line from an outsider, in a trusted comment an outsider edited,
    # or from a User named like the agent App is no outcome: each task is flagged.
    forged = f"- Outcome: SUCCESS\n{trust_stubs.SPAM_TEXT}"
    search = [{"number": n, "repository": {"nameWithOwner": "org/repo"}} for n in (1, 2, 3, 4)]
    comments = {
        1: [trust_stubs.comment(11, forged, trust_stubs.OUTSIDER)],
        2: [trust_stubs.comment(12, forged, trust_stubs.HUMAN, editor=trust_stubs.OUTSIDER)],
        3: [trust_stubs.comment(13, forged, trust_stubs.IMPOSTOR)],
        # The agent's own record still counts.
        4: [trust_stubs.comment(14, "- Outcome: SUCCESS", trust_stubs.AGENT)],
    }
    result = _run_closed_operational(monkeypatch, search, comments)
    assert result == ["org/repo#1", "org/repo#2", "org/repo#3"]
    err = capsys.readouterr().err
    created, edited = trust_stubs.CREATED, trust_stubs.EDITED
    assert err.splitlines() == [
        f"skipped untrusted content: org/repo#1 comment drive-by at {created}"
        " on comment 11 (untrusted)",
        f"skipped untrusted content: org/repo#2 edit drive-by at {edited}"
        " on comment 12 (untrusted)",
        f"skipped untrusted content: org/repo#3 comment vergil-user at {created}"
        " on comment 13 (wrong-type)",
    ]
    assert "SPAM-MARKER-TEXT" not in err
    assert "example.invalid" not in err


def test_closed_operational_read_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    search = [{"number": 1, "repository": {"nameWithOwner": "org/repo"}}]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=search),
        patch("vergil_tooling.lib.github.graphql", return_value={}),
        patch(f"{_AUDIT}.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        pytest.raises(trust.TrustReadError, match=r"read_issue org/repo#1"),
    ):
        epic_audit.closed_operational_without_success("org")


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
    assert "stopped" not in out


def test_render_counts_stopped_operational_children_never_naming_them() -> None:
    val = epics.IssueRef("org", "repo", 7)
    stopped = epics.IssueRef("org", "repo", 9)
    status = epic_audit.OperationalStatus(
        epics.IssueRef("org", ".github", 115),
        (val,),
        (),
        {val: "validation", stopped: "validation"},
        (stopped,),
    )
    out = epic_audit.render([], [], org="org", window_days=30, pending_operational=[status])
    assert (
        "- org/.github#115 — runnable: org/repo#7 (validation); blocked: none; "
        "stopped: 1 (see stderr)"
    ) in out.splitlines()
    # The report may run under GitHub Actions: an untrusted child is counted only.
    assert "org/repo#9" not in out


def test_render_flags_closed_operational_without_success() -> None:
    out = epic_audit.render(
        [], [], org="org", window_days=30, closed_operational_no_success=["org/repo#55"]
    )
    assert "PASS comment" in out
    assert "org/repo#55" in out


def test_operational_pending_reads_dotgithub_epics() -> None:
    captured: list[epics.IssueRef] = []

    def fake_status(epic: epics.IssueRef, *, stops: trust.BatchStops) -> Any:
        captured.append(epic)
        return epic_audit.OperationalStatus(epic, (), (), {})

    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=[MagicMock(number=5)]),
        patch("vergil_tooling.lib.epic_audit.operational_status", side_effect=fake_status),
    ):
        epic_audit.operational_pending("org", stops=trust.BatchStops())
    assert captured == [epics.IssueRef("org", ".github", 5)]


def test_epic_drift_reads_the_org_roadmap() -> None:
    with patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=[]) as gather:
        _epic_drift("org")
    gather.assert_called_once_with("org")


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
    out = json.loads(epic_audit.render_epic_children(epic, children, containers=1))
    assert out["epic"] == "vergil-project/.github#201"
    assert out["containers"] == 1
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
    out = json.loads(epic_audit.render_epic_children(epic, [], containers=1))
    assert out == {"epic": "vergil-project/.github#201", "containers": 1, "children": []}


# -- orphan pass and label retirement (epic .github#176, Task 5) ---------------


def _issue(
    repo: str,
    number: int,
    *,
    labels: tuple[str, ...] = (),
    title: str = "feat: a task",
    body: str = "",
    native_parent: epics.IssueRef | None = None,
) -> epic_audit.OpenIssue:
    owner, name = repo.split("/", 1)
    return epic_audit.OpenIssue(
        ref=epics.IssueRef(owner, name, number),
        title=title,
        body=body,
        labels=frozenset(labels),
        native_parent=native_parent,
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
            "labels": {"totalCount": 1, "nodes": [{"name": "epic"}]},
            "parent": None,
        }
    return node


def test_open_issues_enumerates_managed_repos_with_paging() -> None:
    pages: dict[tuple[str, str | None], dict[str, object]] = {
        ("tool", None): _gql_page([_gql_node(5, labels=("idea",))], has_next=True, cursor="c1"),
        ("tool", "c1"): _gql_page([_gql_node(6, parent=("org", ".github", 9), body=None)]),
        ("app", None): _gql_page([_gql_node(3)]),
    }
    calls: list[tuple[str, str | None]] = []

    def fake_graphql(_query: str, **variables: object) -> object:
        after = variables.get("after")
        key = (str(variables["name"]), None if after is None else str(after))
        calls.append(key)
        return pages[key]

    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool", "fork", "app"]),
        patch(
            "vergil_tooling.lib.epics.is_managed_repo",
            side_effect=lambda repo: repo != "org/fork",
        ),
        patch("vergil_tooling.lib.github.graphql", side_effect=fake_graphql),
    ):
        issues = epic_audit.open_issues("org")
    assert calls == [("tool", None), ("tool", "c1"), ("app", None)]  # fork never read
    assert [i.ref.slug for i in issues] == ["org/tool#5", "org/tool#6", "org/app#3"]
    assert issues[0].labels == frozenset({"idea"})
    assert issues[0].native_parent is None
    assert issues[1].native_parent == epics.IssueRef("org", ".github", 9)
    assert issues[1].body == ""


def test_open_issues_fails_loud_on_label_overflow() -> None:
    node = _gql_node(5)
    node["labels"] = {"totalCount": 101, "nodes": []}
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool"]),
        patch("vergil_tooling.lib.epics.is_managed_repo", return_value=True),
        patch("vergil_tooling.lib.github.graphql", return_value=_gql_page([node])),
        pytest.raises(RuntimeError, match="org/tool#5"),
    ):
        epic_audit.open_issues("org")


@pytest.mark.parametrize("payload", [{}, {"repository": None}])
def test_open_issues_fails_loud_on_unreadable_repository(payload: dict[str, object]) -> None:
    # A listed repo whose issues the token cannot read is never counted as
    # empty: that would under-report it silently (#3513).
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool"]),
        patch("vergil_tooling.lib.epics.is_managed_repo", return_value=True),
        patch("vergil_tooling.lib.github.graphql", return_value=payload),
        pytest.raises(RuntimeError, match="org/tool is listed but its issues cannot be read"),
    ):
        epic_audit.open_issues("org")


def test_open_issues_reads_a_repository_with_no_issues() -> None:
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool"]),
        patch("vergil_tooling.lib.epics.is_managed_repo", return_value=True),
        patch("vergil_tooling.lib.github.graphql", return_value={"repository": {"issues": {}}}),
    ):
        assert epic_audit.open_issues("org") == []


@pytest.mark.parametrize(
    "check",
    [
        lambda org: epic_audit.orphan_drift(org, parents=epic_audit.BodyParents()),
        epic_audit.retired_label_drift,
        lambda org: list(
            epic_audit.body_only_parents(
                org, stops=trust.BatchStops(), authors=trust_stubs.AUTHORS
            ).pairs
        ),
    ],
    ids=["orphan_drift", "retired_label_drift", "body_only_parents"],
)
def test_orphan_pass_checks_enumerate_when_not_handed_issues(check: Any) -> None:
    with patch("vergil_tooling.lib.epic_audit.open_issues", return_value=[]) as enum:
        assert check("org") == []
    enum.assert_called_once_with("org")


def _judged_parents(
    issues: list[epic_audit.OpenIssue],
    *,
    stops: trust.BatchStops | None = None,
    items: dict[tuple[str, int], Any] | None = None,
) -> epic_audit.BodyParents:
    """body_only_parents of *issues*, each body read through the real trust layer.

    Each issue's trusted body is its listed body, by the trusted human, unless
    *items* gives that issue's item answer.
    """
    answers = {(f"{i.ref.owner}/{i.ref.repo}", i.ref.number): trust_stubs.item(body=i.body)
               for i in issues}  # fmt: skip
    answers.update(items or {})
    with patch("vergil_tooling.lib.github.graphql", side_effect=trust_stubs.FakeItems(answers)):
        return epic_audit.body_only_parents(
            "org",
            stops=trust.BatchStops() if stops is None else stops,
            authors=trust_stubs.AUTHORS,
            issues=issues,
        )


def _orphan_drift(issues: list[epic_audit.OpenIssue]) -> list[epic_audit.Orphan]:
    """orphan_drift of *issues*, their body parents judged by :func:`_judged_parents`."""
    return epic_audit.orphan_drift("org", parents=_judged_parents(issues), issues=issues)


def _attach(
    orphans: list[epic_audit.Orphan], stops: trust.BatchStops | None = None
) -> epic_audit.LinkPass:
    """attach_orphans of *orphans*, judged with the stub authors."""
    return epic_audit.attach_orphans(
        orphans, stops=trust.BatchStops() if stops is None else stops, authors=trust_stubs.AUTHORS
    )


def _homes(repo: str) -> tuple[str, str]:
    # Every repo's queues are its org's .github queues.
    owner = repo.split("/", 1)[0]
    return (f"{owner}/.github", owner)


def test_orphan_drift_targets_intake_epic_of_home() -> None:
    issues = [_issue("org/tool", 5), _issue("org/app", 3)]
    with patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes):
        orphans = _orphan_drift(issues)
    assert orphans == [
        epic_audit.Orphan(epics.IssueRef("org", "tool", 5), home="org/.github", name="org"),
        epic_audit.Orphan(epics.IssueRef("org", "app", 3), home="org/.github", name="org"),
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
    layer = trust_stubs.FakeItems(
        {("org/tool", 9): trust_stubs.item(body="<!-- vrg-release:progress -->\n- [ ] tag\n")},
        strict=True,
    )
    with (
        patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes) as qh,
        patch("vergil_tooling.lib.github.graphql", side_effect=layer),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        parents = epic_audit.body_only_parents(
            "org", stops=trust.BatchStops(), authors=trust_stubs.AUTHORS, issues=issues
        )
        assert epic_audit.orphan_drift("org", parents=parents, issues=issues) == []
    qh.assert_not_called()


def test_orphan_drift_untrusted_release_issue_is_an_orphan(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A forged release title and marker from an outsider never hide the issue from the sweep.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    body = f"{trust_stubs.SPAM_TEXT}\n<!-- vrg-release:progress -->\n"
    issues = [_issue("org/tool", 9, title="release: 2.1.258", body=body)]
    layer = trust_stubs.FakeItems(
        {("org/tool", 9): trust_stubs.item(body=body, author=trust_stubs.OUTSIDER)}, strict=True
    )
    with (
        patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes),
        patch("vergil_tooling.lib.github.graphql", side_effect=layer),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        parents = epic_audit.body_only_parents(
            "org", stops=trust.BatchStops(), authors=trust_stubs.AUTHORS, issues=issues
        )
        orphans = epic_audit.orphan_drift("org", parents=parents, issues=issues)
    assert [o.ref for o in orphans] == [epics.IssueRef("org", "tool", 9)]
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: org/tool#9 issue drive-by at {trust_stubs.CREATED} "
        "on body (untrusted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_orphan_drift_skips_archives_and_epics() -> None:
    issues = [
        _issue("org/.github", 1, labels=("epic",)),
        _issue("org/.github", 2, labels=("archive", "intake")),
        _issue("org/.github", 3, labels=("epic", "intake")),
    ]
    assert _orphan_drift(issues) == []


def test_orphan_drift_skips_unmanaged_repos() -> None:
    # The shared enumeration never reads an unmanaged repo, so nothing from it
    # can become an orphan; orphan_drift enumerates when not handed issues.
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["fork"]),
        patch("vergil_tooling.lib.epics.is_managed_repo", return_value=False),
        patch("vergil_tooling.lib.github.graphql") as gql,
    ):
        assert epic_audit.orphan_drift("org", parents=epic_audit.BodyParents()) == []
    gql.assert_not_called()


def test_orphan_drift_treats_body_parent_as_parented() -> None:
    issues = [
        _issue("org/tool", 5, body="Parent: org/.github#40\n"),
        _issue("org/tool", 6, native_parent=epics.IssueRef("org", ".github", 40)),
    ]
    with patch("vergil_tooling.lib.epics.is_epic", return_value=True):
        assert _orphan_drift(issues) == []


def test_orphan_drift_attaches_body_parent_that_is_not_an_epic() -> None:
    # A body Parent: naming a non-epic is not epic-parented: it is an orphan.
    issues = [_issue("org/tool", 5, body="Parent: org/tool#4\n")]
    with (
        patch("vergil_tooling.lib.epics.is_epic", return_value=False),
        patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes),
    ):
        orphans = _orphan_drift(issues)
    assert [o.ref.number for o in orphans] == [5]


def _intake_epic(home: str) -> epics.IssueRef:
    owner, repo = home.split("/", 1)
    return epics.IssueRef(owner, repo, 100 if owner == "org" else 7)


_ORG_INTAKE = epics.IssueRef("org", ".github", 100)


def _intake_graph(
    monkeypatch: pytest.MonkeyPatch,
    *,
    homes: tuple[str, ...] = ("org/.github",),
    open_items: int = 0,
    closed_items: int = 0,
) -> tuple[FakeSubIssueGraph, MagicMock]:
    """A graph holding each home's intake epic, the org's with *open_items* and *closed_items*.

    ``ensure_standing_epic`` is patched to find each home's intake epic
    (:func:`_intake_epic`); the mock is returned with the graph.
    """
    graph = FakeSubIssueGraph().install(monkeypatch)
    for home in homes:
        owner = home.split("/", 1)[0]
        graph.add_issue(
            _intake_epic(home), title=f"Epic (intake): {owner}", labels=["epic", "intake"]
        )
    for i in range(open_items + closed_items):
        item = graph.add_issue(
            epics.IssueRef("org", "tool", 1000 + i),
            title=f"item {i}",
            state="OPEN" if i < open_items else "CLOSED",
        )
        graph.link(_ORG_INTAKE, item)
    ensure = MagicMock(side_effect=lambda _kind, home, _name: _intake_epic(home))
    monkeypatch.setattr(epics, "ensure_standing_epic", ensure)
    return graph, ensure


def _orphans_in(
    graph: FakeSubIssueGraph, repo: str, numbers: tuple[int, ...]
) -> list[epic_audit.Orphan]:
    """Open, unparented issues *numbers* of *repo* in *graph*, each an orphan of its org's queue."""
    owner, name = repo.split("/", 1)
    orphans: list[epic_audit.Orphan] = []
    for number in numbers:
        ref = epics.IssueRef(owner, name, number)
        if ref not in graph.issues:
            graph.add_issue(ref, title=f"orphan {number}")
        orphans.append(epic_audit.Orphan(ref, home=f"{owner}/.github", name=owner))
    return orphans


def test_attach_orphans_attaches_each_under_its_intake_epic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, ensure = _intake_graph(monkeypatch, homes=("org/.github", "other/.github"))
    orphans = _orphans_in(graph, "org/tool", (5, 6)) + _orphans_in(graph, "other/tool", (3,))
    with patch("vergil_tooling.lib.epics.child_states", wraps=epics.child_states) as cs:
        assert _attach(orphans) == epic_audit.LinkPass(linked=3)
    assert ensure.call_count == 2  # resolved once per home
    assert ensure.call_args_list[0].args == (epics.INTAKE, "org/.github", "org")
    assert cs.call_count == 2  # open-items count fetched once per epic
    other = epics.IssueRef("other", ".github", 7)
    assert graph.mutations == [
        ("add", _ORG_INTAKE, epics.IssueRef("org", "tool", 5)),
        ("add", _ORG_INTAKE, epics.IssueRef("org", "tool", 6)),
        ("add", other, epics.IssueRef("other", "tool", 3)),
    ]


def test_attach_orphans_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    # After the attach, each issue has a native parent, so a re-run finds nothing.
    graph, _ = _intake_graph(monkeypatch)
    _orphans_in(graph, "org/tool", (5,))
    before = [_issue("org/tool", 5)]
    with patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes):
        assert _attach(_orphan_drift(before)) == epic_audit.LinkPass(linked=1)
    assert graph.parent_of(epics.IssueRef("org", "tool", 5)) == _ORG_INTAKE
    after = [_issue("org/tool", 5, native_parent=_ORG_INTAKE)]
    assert _orphan_drift(after) == []


def test_orphan_pass_past_95_links_and_nudges(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cap of 95 open items is the review nudge, never a stop (spec §4.7 of
    # .github#1609): every orphan is linked through the chain, and the result
    # names the queue and its open items once the pass is done. A closed item
    # is not an open one.
    cap = epics._ADHOC_ARCHIVE_SEGMENT_CAP
    graph, _ = _intake_graph(monkeypatch, open_items=cap, closed_items=1)
    orphans = _orphans_in(graph, "org/tool", (5, 6, 7))
    assert _attach(orphans) == epic_audit.LinkPass(
        linked=3,
        nudges=("intake queue org/.github#100 holds 98 open items; run epic-review intake",),
    )
    assert [graph.parent_of(o.ref) for o in orphans] == [_ORG_INTAKE] * 3


def test_attach_orphans_nudges_when_attaching_reaches_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, _ = _intake_graph(monkeypatch, open_items=epics._ADHOC_ARCHIVE_SEGMENT_CAP - 2)
    first = _attach(_orphans_in(graph, "org/tool", (5,)))
    assert first == epic_audit.LinkPass(linked=1)  # 94 open: under the cap, no nudge
    second = _attach(_orphans_in(graph, "org/tool", (6,)))
    assert second.nudges == (
        "intake queue org/.github#100 holds 95 open items; run epic-review intake",
    )


def test_orphan_pass_rollover_disabled_reports_full_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rollover off and the intake head at GitHub's 100: each of its orphans is
    # reported with RolloverDisabledError's message, as a closed epic is;
    # nothing is linked or created there, and another home still attaches.
    assert not epic_chain.ROLLOVER_ENABLED
    graph, _ = _intake_graph(
        monkeypatch, homes=("org/.github", "other/.github"), open_items=90, closed_items=10
    )
    graph.clear_log()
    orphans = _orphans_in(graph, "org/tool", (5, 6)) + _orphans_in(graph, "other/tool", (3,))
    result = _attach(orphans)
    message = (
        "epic org/.github#100 is at GitHub's limit of 100 sub-issues, and rollover into a"
        " continuation is not enabled yet"
    )
    assert result == epic_audit.LinkPass(
        linked=1,
        refused=(f"org/tool#5 not linked: {message}", f"org/tool#6 not linked: {message}"),
    )
    assert graph.events == [
        ("add", epics.IssueRef("other", ".github", 7), epics.IssueRef("other", "tool", 3))
    ]


def test_attach_orphans_rolls_a_full_queue_over(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # With rollover on, an orphan past the head's 99 items goes into a new
    # continuation, and the queue's rollover is the review nudge.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph, _ = _intake_graph(monkeypatch, open_items=10, closed_items=epic_chain.ITEM_CAP - 10)
    orphan = _orphans_in(graph, "org/tool", (5,))[0]
    assert _attach([orphan]) == epic_audit.LinkPass(
        linked=1,
        nudges=(),
    )
    continuation = graph.parent_of(orphan.ref)
    assert continuation is not None
    assert graph.parent_of(continuation) == _ORG_INTAKE
    assert f"queue org/.github#100 rolled over into continuation {continuation.slug}" in (
        capsys.readouterr().out
    )


def _outsider_orphans(
    graph: FakeSubIssueGraph,
) -> list[epic_audit.Orphan]:
    """Orphans #5-#9 in *graph*: #5 and #7 outsider-opened, #9 a User named like the agent App.

    #6 is the trusted human's; #8 is too, its body and a comment an outsider's.
    Every read of an issue *graph* does not hold fails the test.
    """
    spam = trust_stubs.SPAM_TEXT
    answers = {
        5: trust_stubs.item(body=spam, author=trust_stubs.OUTSIDER),
        6: trust_stubs.item(),
        7: trust_stubs.item(body=spam, author=trust_stubs.OUTSIDER),
        8: trust_stubs.item(
            body=spam,
            editor=trust_stubs.OUTSIDER,
            nodes=[trust_stubs.comment(1, spam, trust_stubs.OUTSIDER)],
        ),
        9: trust_stubs.item(body=spam, author=trust_stubs.IMPOSTOR),
    }
    orphans = _orphans_in(graph, "org/tool", tuple(answers))
    for number, answer in answers.items():
        graph.layer.items[("org/tool", number)] = answer
    graph.layer.strict = True
    return orphans


def test_attach_orphans_skips_outsider_issue_clean_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An outsider-opened orphan is never attached: it is reported, metadata
    # only, and left out of the count, and the pass exits clean. Only the
    # author decides it, so #8 (an outsider's edit and comment) is attached.
    graph, _ = _intake_graph(monkeypatch)
    orphans = _outsider_orphans(graph)
    assert _attach(orphans) == epic_audit.LinkPass(linked=2)
    assert graph.mutations == [
        ("add", _ORG_INTAKE, epics.IssueRef("org", "tool", 6)),
        ("add", _ORG_INTAKE, epics.IssueRef("org", "tool", 8)),
    ]
    assert graph.layer.reads == [("org/tool", n) for n in (5, 6, 7, 8, 9)]
    err = capsys.readouterr().err
    created = trust_stubs.CREATED
    assert err == (
        f"skipped untrusted content: org/tool#5 issue drive-by at {created} on body (untrusted)\n"
        f"skipped untrusted content: org/tool#7 issue drive-by at {created} on body (untrusted)\n"
        f"skipped untrusted content: org/tool#9 issue vergil-user at {created} on body"
        " (wrong-type)\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_attach_orphans_outsider_issues_under_actions_are_counts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph, _ = _intake_graph(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    orphans = _outsider_orphans(graph)
    assert _attach(orphans) == epic_audit.LinkPass(linked=2)
    err = capsys.readouterr().err
    assert err == "skipped 3 untrusted item(s) (untrusted: 2, wrong-type: 1)\n"


def test_attach_orphans_outsider_issues_stay_out_of_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One below the cap: the outsider-opened orphans before the trusted one
    # are never counted, so the queue's count is 95 once #6 is linked.
    graph, _ = _intake_graph(monkeypatch, open_items=epics._ADHOC_ARCHIVE_SEGMENT_CAP - 1)
    orphans = _outsider_orphans(graph)
    del orphans[3]  # keep #6 as the only trusted orphan
    assert _attach(orphans) == epic_audit.LinkPass(
        linked=1,
        nudges=("intake queue org/.github#100 holds 95 open items; run epic-review intake",),
    )
    assert graph.parent_of(epics.IssueRef("org", "tool", 6)) == _ORG_INTAKE


class _FailingItems(trust_stubs.FakeItems):
    """The trust layer's reads, with #9's item unreadable."""

    def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        if variables.get("number") == 9:
            return {"repository": {"issueOrPullRequest": None}}
        return super().__call__(query, **variables)


def test_attach_orphans_read_failure_is_a_per_item_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A read the layer cannot complete stops that orphan alone: it is never
    # attached, the trusted orphans are, and the stop is recorded for the
    # run's exit status (spec §5.3 of epic .github#1467).
    graph, _ = _intake_graph(monkeypatch)
    orphans = _outsider_orphans(graph)
    graph.layer = _FailingItems(graph.layer.items, strict=True)
    stops = trust.BatchStops()
    assert _attach(orphans, stops) == epic_audit.LinkPass(linked=2)
    assert graph.mutations == [
        ("add", _ORG_INTAKE, epics.IssueRef("org", "tool", 6)),
        ("add", _ORG_INTAKE, epics.IssueRef("org", "tool", 8)),
    ]
    assert stops.stopped == (
        trust.Skipped("org/tool", 9, "issue", None, "", "body", trust.READ_FAILURE),
    )
    stops.print_summary(sys.stderr)
    err = capsys.readouterr().err
    assert "stopped on a read failure: org/tool#9 on body: cannot read who wrote" in err
    assert "SPAM-MARKER-TEXT" not in err


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
    # only removes the label — it never re-parents, so no sub-issue query or
    # mutation is sent.
    drift = [epic_audit.RetiredLabels(epics.IssueRef("vergil-project", ".github", 428), ("idea",))]
    with (
        patch("vergil_tooling.lib.github.run") as run,
        patch("vergil_tooling.lib.github.graphql") as gql,
    ):
        assert epic_audit.strip_retired_labels(drift) == 1
    run.assert_called_once_with(
        "issue", "edit", "428", "--repo", "vergil-project/.github", "--remove-label", "idea"
    )
    gql.assert_not_called()


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
        parents = _judged_parents(issues)
    assert parents.pairs == (
        (epics.IssueRef("org", "tool", 5), epics.IssueRef("org", ".github", 40)),
    )
    assert parents.held == frozenset()


def _nativize_graph(monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    """A graph with the open epic org/.github#41, the closed #40, and tasks org/tool#5 and #6."""
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(
        epics.IssueRef("org", ".github", 40), title="Done", labels=["epic"], state="CLOSED"
    )
    graph.add_issue(epics.IssueRef("org", ".github", 41), title="Live", labels=["epic"])
    for number in (5, 6):
        graph.add_issue(epics.IssueRef("org", "tool", number), title=f"task {number}")
    return graph


def test_nativize_skips_closed_epic_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    # Closed is closed (.github#1609): the task naming a closed epic is reported
    # naming both, the next task is still linked, and nothing is reopened.
    graph = _nativize_graph(monkeypatch)
    closed_task, closed_epic = (
        epics.IssueRef("org", "tool", 5),
        epics.IssueRef("org", ".github", 40),
    )
    open_task, open_epic = epics.IssueRef("org", "tool", 6), epics.IssueRef("org", ".github", 41)
    result = epic_audit.nativize_parents([(closed_task, closed_epic), (open_task, open_epic)])
    assert result == epic_audit.LinkPass(
        linked=1,
        refused=(
            "org/tool#5 not linked: epic org/.github#40 is closed; closed epics are not "
            "reopened by the tooling",
        ),
    )
    assert graph.events == [("add", open_epic, open_task)]
    assert graph.issues[closed_epic].state == "CLOSED"


def test_nativize_links_a_continuation_ref_into_its_head_chain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A Parent: line naming a continuation means its head (spec §4.1): the
    # task goes into the head's chain by first fit, here the head itself.
    graph = _nativize_graph(monkeypatch)
    head = epics.IssueRef("org", ".github", 41)
    continuation = graph.add_continuation(epics.IssueRef("org", ".github", 42), head, head)
    graph.clear_log()
    task = epics.IssueRef("org", "tool", 5)
    assert epic_audit.nativize_parents([(task, continuation)]) == epic_audit.LinkPass(linked=1)
    assert graph.events == [("add", head, task)]
    assert "org/.github#42 is a continuation of org/.github#41" in capsys.readouterr().err


def test_attach_orphans_reports_closed_intake_epic(monkeypatch: pytest.MonkeyPatch) -> None:
    # A home whose only intake epic is closed links nothing there and reopens
    # nothing; each of its orphans is named, and another home's still attaches.
    graph, _ = _intake_graph(monkeypatch, homes=("other/.github",))
    closed = epics.ClosedStandingEpicError(_ORG_INTAKE)
    orphans = _orphans_in(graph, "org/tool", (5, 6)) + _orphans_in(graph, "other/tool", (3,))

    def ensure(_kind: object, home: str, _name: str) -> epics.IssueRef:
        if home == "org/.github":
            raise closed
        return _intake_epic(home)

    with patch("vergil_tooling.lib.epics.ensure_standing_epic", side_effect=ensure) as mock_ensure:
        result = _attach(orphans)
    message = (
        "epic org/.github#100 is closed; closed epics are not reopened by the tooling; "
        "a person may reopen it by hand"
    )
    assert result == epic_audit.LinkPass(
        linked=1,
        refused=(f"org/tool#5 not linked: {message}", f"org/tool#6 not linked: {message}"),
    )
    assert mock_ensure.call_count == 2  # the closed home is looked up once
    assert graph.events == [
        ("add", epics.IssueRef("other", ".github", 7), epics.IssueRef("other", "tool", 3))
    ]


def test_attach_orphans_epic_closed_at_link_time_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The epic closes between the lookup and the link: the refusal is reported
    # for that orphan and the rest of its home, never raised out of the pass,
    # and the epic is read once.
    graph, _ = _intake_graph(monkeypatch)
    graph.close(_ORG_INTAKE)
    orphans = _orphans_in(graph, "org/tool", (5, 6))
    with patch("vergil_tooling.lib.epic_chain.link_into", wraps=epic_chain.link_into) as link:
        result = _attach(orphans)
    assert result.linked == 0
    assert [line.split(" not linked: ")[0] for line in result.refused] == [
        "org/tool#5",
        "org/tool#6",
    ]
    link.assert_called_once()
    assert graph.events == []


def test_nativize_parents_adds_native_link(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _nativize_graph(monkeypatch)
    task = epics.IssueRef("org", "tool", 5)
    epic = epics.IssueRef("org", ".github", 41)
    assert epic_audit.nativize_parents([(task, epic)]) == epic_audit.LinkPass(linked=1)
    assert graph.parent_of(task) == epic
    assert graph.mutations == [("add", epic, task)]


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
        patch("vergil_tooling.lib.epics.unbuildable_kind", return_value="intake"),
    ):
        assert _drift(prs, "org") == []
    parent.assert_called_once_with(epics.IssueRef("org", "repo", 8))


def test_task_drift_skips_ops_issue() -> None:
    # A merged PR naming an ops issue never closes it: an ops issue is closed by
    # hand (epic vergil-project/.github#1494). The parent's labels are read for real.
    prs = _one_pr_reffing_8()
    ops_epic = epics.IssueRef("org", ".github", 3)

    def fake_read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        if args[2] == "3":
            return {"labels": [{"name": "epic"}, {"name": "operations"}]}
        return {"state": "open", "title": "t", "body": "b"}

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_read_json),
        patch("vergil_tooling.lib.epics.parent_of", return_value=ops_epic) as parent,
        patch("vergil_tooling.lib.epics._container_parent", return_value=None),
    ):
        assert _drift(prs, "org") == []
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
        patch("vergil_tooling.lib.epics.unbuildable_kind", return_value=None),
    ):
        result = _drift(prs, "org")
    assert result == [TaskDrift("org/repo", 8, 31, "u")]


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
    )
    assert "No drift" not in out
    assert "- org/tool#5 → Epic (intake): org in org/.github" in out
    assert "- org/tool#6 — idea" in out
    assert "- org/tool#7 → org/.github#40" in out


def test_render_orphan_pass_sections_only_when_present() -> None:
    out = epic_audit.render([], [], org="org", window_days=30, stray=["org/.github#3"])
    assert "Invariant violations" in out
    assert "Orphans" not in out
    assert "Retired" not in out


# -- Misplaced archives (vergil-tooling#3817) ----------------------------------

_INTAKE_EPIC = epics.IssueRef("org", ".github", 381)
_ADHOC_EPIC = epics.IssueRef("org", ".github", 7)
_LOST_LABEL_TITLE = "Archive (ad hoc): lab — 2026-Q3"


def test_orphan_drift_never_adopts_an_archive_that_lost_its_label() -> None:
    # The #3817 shape: a partial legacy normalize left the new title, no epic
    # and no archive label; the orphan pass attached it to an intake epic.
    issues = [_issue("org/.github", 180, labels=("ad-hoc",), title=_LOST_LABEL_TITLE)]
    with patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes) as qh:
        assert _orphan_drift(issues) == []
    qh.assert_not_called()


def test_misplaced_archives_flags_parented_and_unlabelled_archives() -> None:
    issues = [
        _issue(  # the #3817 case: under an intake epic, label lost
            "org/.github",
            180,
            labels=("ad-hoc",),
            title=_LOST_LABEL_TITLE,
            native_parent=_INTAKE_EPIC,
        ),
        _issue(  # labelled, but parented under a non-queue epic
            "org/.github",
            181,
            labels=("archive", "ad-hoc"),
            title="Archive (ad hoc): lab — 2026-Q4",
            native_parent=_ADHOC_EPIC,
        ),
        _issue("org/lab", 9, labels=("ad-hoc",), title=_LOST_LABEL_TITLE),  # top-level, no label
        _issue(  # a well-formed archive: fine
            "org/.github",
            182,
            labels=("archive", "intake"),
            title="Archive (intake): org — 2026-Q4",
        ),
        _issue("org/tool", 5, native_parent=_INTAKE_EPIC),  # a queued task: not an archive
        _issue(  # a normalize whose second edit dropped epic but not the legacy title
            "org/.github",
            183,
            labels=("archive", "ad-hoc"),
            title="Epic (ad hoc): lab — 2026-Q3",
        ),
        _issue(  # an overflow segment's stamped title: fine
            "org/.github",
            184,
            labels=("archive", "ad-hoc"),
            title="Archive (ad hoc): lab — 2026-Q4 (2)",
        ),
    ]
    kinds = {_INTAKE_EPIC: "intake", _ADHOC_EPIC: None}
    with (
        patch("vergil_tooling.lib.epics.queue_kind", side_effect=kinds.__getitem__) as qk,
        patch("vergil_tooling.lib.github.run") as run,
    ):
        found = epic_audit.misplaced_archives("org", issues=issues)
    assert found == [
        epic_audit.MisplacedArchive(
            epics.IssueRef("org", ".github", 180),
            "parented under the intake queue epic org/.github#381; missing the `archive` label",
        ),
        epic_audit.MisplacedArchive(
            epics.IssueRef("org", ".github", 181), "parented under org/.github#7"
        ),
        epic_audit.MisplacedArchive(epics.IssueRef("org", "lab", 9), "missing the `archive` label"),
        epic_audit.MisplacedArchive(
            epics.IssueRef("org", ".github", 183), "title is not a stamped archive title"
        ),
    ]
    assert qk.call_count == 2  # only parented archives read their parent's labels
    run.assert_not_called()  # report only


def test_misplaced_archives_enumerates_when_not_handed_issues() -> None:
    issues = [_issue("org/lab", 9, labels=("ad-hoc",), title=_LOST_LABEL_TITLE)]
    with patch("vergil_tooling.lib.epic_audit.open_issues", return_value=issues) as enum:
        found = epic_audit.misplaced_archives("org")
    enum.assert_called_once_with("org")
    assert [m.ref for m in found] == [epics.IssueRef("org", "lab", 9)]


def test_render_lists_misplaced_archives_as_invariant_violations() -> None:
    misplaced = [
        epic_audit.MisplacedArchive(epics.IssueRef("org", "lab", 9), "missing the `archive` label")
    ]
    out = epic_audit.render([], [], org="org", window_days=30, misplaced=misplaced)
    assert "No drift" not in out
    assert "## Invariant violations" in out
    assert "**Misplaced archives**" in out
    assert "- org/lab#9 — missing the `archive` label" in out


def test_render_closed_lists_misplaced_archives() -> None:
    misplaced = [
        epic_audit.MisplacedArchive(epics.IssueRef("org", "lab", 9), "missing the `archive` label")
    ]
    out = epic_audit.render_closed([], org="org", window_days=30, misplaced=misplaced)
    assert "No drift" not in out
    assert "## Needs a human (report only)" in out
    assert "- org/lab#9 — missing the `archive` label" in out


# -- Epic documents check (epic vergil-project/.github#819) --------------------


def _link(
    path: str, *, repo: str = ".github", owner: str = "org", branch: str = "develop"
) -> epic_docs.DocLink:
    return epic_docs.DocLink(owner, repo, path, branch)


def _section(
    spec: epic_docs.Entry = epic_docs.PENDING,
    plan: epic_docs.Entry = epic_docs.PENDING,
    retrospective: epic_docs.Entry = epic_docs.PENDING,
) -> str:
    block = epic_docs.render(
        {"spec": spec, "plan": plan, "retrospective": retrospective}, ".github"
    )
    return f"{block}\n\nThe epic's description.\n"


def _linked() -> str:
    return _section(
        _link("epics/5-x/spec.md"), _link("epics/5-x/plan.md"), _link("epics/5-x/retrospective.md")
    )


def _not_found() -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ["gh", "api"], output="", stderr="gh: Not Found (HTTP 404)")


def _documents(
    listing: list[epic_docs.EpicRecord],
    *,
    kind: str | None = "file",
    branch: str = "develop",
) -> tuple[list[epic_audit.EpicDocsFinding], MagicMock]:
    content_type = MagicMock(return_value=kind)
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=listing),
        patch("vergil_tooling.lib.epic_docs.default_branch", return_value=branch),
        patch("vergil_tooling.lib.epic_docs.content_type", content_type),
    ):
        return epic_audit.epic_documents("org"), content_type


def test_epic_documents_missing_section() -> None:
    findings, _ = _documents([_record(1, state="OPEN", body="No section."), _record(2, body=None)])
    assert findings == [
        epic_audit.EpicDocsFinding(1, "OPEN", "no Documents section"),
        epic_audit.EpicDocsFinding(2, "CLOSED", "no Documents section"),
    ]


def test_epic_documents_malformed() -> None:
    half = f"{epic_docs.BEGIN}\n{epic_docs.HEADING}\n\n- Spec: pending\n"
    findings, _ = _documents([_record(3, state="OPEN", body=half)])
    assert findings == [
        epic_audit.EpicDocsFinding(
            3,
            "OPEN",
            f"malformed Documents section: expected one {epic_docs.BEGIN} followed by one "
            f"{epic_docs.END}, found 1 and 0",
        )
    ]


def test_epic_documents_bad_entry_is_malformed() -> None:
    body = _section().replace("- Plan: pending", "- Plan: see the wiki")
    findings, _ = _documents([_record(4, body=body)])
    assert [f.problem for f in findings] == [
        "malformed Documents section: Documents entry is neither a note nor a link: 'see the wiki'"
    ]


def test_epic_documents_closed_link_404() -> None:
    findings, content_type = _documents([_record(5, body=_linked())], kind=None)
    problems = [f.problem for f in findings]
    assert problems == [
        "Spec link `.github/epics/5-x/spec.md` does not resolve on `develop`",
        "Plan link `.github/epics/5-x/plan.md` does not resolve on `develop`",
        "Retrospective link `.github/epics/5-x/retrospective.md` does not resolve on `develop`",
    ]
    content_type.assert_any_call("org", ".github", "epics/5-x/spec.md", "develop")


def test_epic_documents_closed_link_to_a_directory() -> None:
    body = _section(_link("epics/5-x"), epic_docs.NOT_WRITTEN, epic_docs.NOT_WRITTEN)
    findings, content_type = _documents([_record(5, body=body)], kind="dir")
    assert [f.problem for f in findings] == [
        "Spec links a directory, `.github/epics/5-x`, on `develop`, not a file"
    ]
    content_type.assert_called_once_with("org", ".github", "epics/5-x", "develop")


def test_epic_documents_closed_link_to_another_kind() -> None:
    body = _section(_link("epics/5-x/spec.md"), epic_docs.NOT_WRITTEN, epic_docs.NOT_WRITTEN)
    findings, _ = _documents([_record(5, body=body)], kind="submodule")
    assert [f.problem for f in findings] == [
        "Spec links `.github/epics/5-x/spec.md`, a submodule on `develop`, not a file"
    ]


def test_epic_documents_closed_links_resolve() -> None:
    findings, content_type = _documents([_record(5, body=_linked())])
    assert findings == []
    assert content_type.call_count == 3


def test_epic_documents_closed_link_off_the_default_branch() -> None:
    body = _section(
        _link("epics/5-x/spec.md", branch="main"), epic_docs.NOT_WRITTEN, epic_docs.NOT_WRITTEN
    )
    findings, content_type = _documents([_record(5, body=body)])
    assert [f.problem for f in findings] == [
        "Spec links `.github/epics/5-x/spec.md` on branch `main`, not the default branch `develop`"
    ]
    content_type.assert_called_once_with("org", ".github", "epics/5-x/spec.md", "develop")


def test_epic_documents_closed_link_to_a_missing_repo() -> None:
    body = _section(
        _link("docs/specs/x.md", repo="gone"), epic_docs.NOT_WRITTEN, epic_docs.NOT_WRITTEN
    )
    content_type = MagicMock(return_value="file")
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=[_record(6, body=body)]),
        patch("vergil_tooling.lib.epic_docs.default_branch", side_effect=_not_found()),
        patch("vergil_tooling.lib.epic_docs.content_type", content_type),
    ):
        findings = epic_audit.epic_documents("org")
    assert [f.problem for f in findings] == [
        "Spec links `gone/docs/specs/x.md` in a repo that does not exist"
    ]
    content_type.assert_not_called()


def test_epic_documents_other_api_errors_propagate() -> None:
    error = github.GitHubAPIError(1, ["gh", "api"], output="", stderr="HTTP 502: Bad Gateway")
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=[_record(5, body=_linked())]),
        patch("vergil_tooling.lib.epic_docs.default_branch", side_effect=error),
        pytest.raises(github.GitHubAPIError),
    ):
        epic_audit.epic_documents("org")


def test_epic_documents_caches_default_branches() -> None:
    default_branch = MagicMock(return_value="develop")
    listing = [_record(5, body=_linked()), _record(6, body=_linked())]
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=listing),
        patch("vergil_tooling.lib.epic_docs.default_branch", default_branch),
        patch("vergil_tooling.lib.epic_docs.content_type", return_value="file"),
    ):
        assert epic_audit.epic_documents("org") == []
    default_branch.assert_called_once_with("org", ".github")


def test_epic_documents_closed_link_outside_the_org() -> None:
    body = _section(
        _link("docs/specs/x.md", owner="elsewhere", repo="tools"),
        epic_docs.NOT_WRITTEN,
        epic_docs.NOT_WRITTEN,
    )
    findings, content_type = _documents([_record(7, body=body)])
    assert [f.problem for f in findings] == [
        "Spec links `tools/docs/specs/x.md` outside the org org"
    ]
    content_type.assert_not_called()


def test_epic_documents_closed_pending() -> None:
    body = _section(_link("epics/5-x/spec.md"), _link("epics/5-x/plan.md"))
    findings, _ = _documents([_record(8, body=body)])
    assert [f.problem for f in findings] == ["Retrospective is still `pending` on a closed epic"]


def test_epic_documents_closed_notes_are_fine() -> None:
    body = _section(
        epic_docs.PREDATES_SPEC_PLAN, epic_docs.PREDATES_SPEC_PLAN, epic_docs.PREDATES_RETRO
    )
    findings, _ = _documents([_record(9, body=body, reason="COMPLETED")])
    assert findings == []


def test_epic_documents_open_link_not_checked() -> None:
    findings, content_type = _documents([_record(10, state="OPEN", body=_linked())], kind=None)
    assert findings == []
    content_type.assert_not_called()


def test_epic_documents_open_pending_is_fine() -> None:
    findings, _ = _documents([_record(11, state="OPEN", body=_section())])
    assert findings == []


@pytest.mark.parametrize(
    ("title", "labels"),
    [
        ("Epic (ad-hoc): lab", ("epic", "ad-hoc")),
        ("Epic (intake): lab", ("epic", "intake")),
        ("Old standing", ("epic", "standing")),
        ("Epic (standing): misc", ("epic",)),
        ("[archived] Epic: old", ("epic",)),
    ],
)
def test_epic_documents_skips(title: str, labels: tuple[str, ...]) -> None:
    findings, _ = _documents([_record(12, title=title, labels=labels, body=None)])
    assert findings == []


def test_epic_documents_scopes_to_home_under_its_owner_token() -> None:
    seen: list[str | None] = []

    def fake_list(home: str, *, apply_skips: bool) -> list[epic_docs.EpicRecord]:
        assert home == "lab-org/lab"
        assert apply_skips is False
        seen.append(github._target_org.get())
        return []

    with patch("vergil_tooling.lib.epic_docs.list_epics", side_effect=fake_list):
        assert epic_audit.epic_documents("org", home="lab-org/lab") == []
    assert seen == ["lab-org"]


def test_epic_documents_uses_a_given_listing() -> None:
    with patch("vergil_tooling.lib.epic_docs.list_epics") as mock_list:
        assert epic_audit.epic_documents("org", listing=[]) == []
    mock_list.assert_not_called()


def test_render_epic_documents_section() -> None:
    findings = [
        epic_audit.EpicDocsFinding(
            30, "CLOSED", "Retrospective is still `pending` on a closed epic"
        ),
        epic_audit.EpicDocsFinding(4, "OPEN", "no Documents section"),
    ]
    out = epic_audit.render([], [], org="org", window_days=30, epic_docs_findings=findings)
    assert "No drift" not in out
    section = out.split("## Epic documents\n", 1)[1]
    lines = [line for line in section.splitlines() if line.startswith("- ")]
    assert lines == [
        "- org/.github#4 (open) — no Documents section",
        "- org/.github#30 (closed) — Retrospective is still `pending` on a closed epic",
    ]


def test_render_without_epic_documents_findings_has_no_section() -> None:
    out = epic_audit.render([], [], org="org", window_days=30, epic_docs_findings=[])
    assert "Epic documents" not in out
    assert "No drift" in out


# -- bounded searches and lists (#4038) ----------------------------------------


def _limited(rows: list[dict[str, Any]]) -> Any:
    """A gh list/search stand-in that returns at most ``--limit`` rows (30 without one)."""

    def fake(*args: str) -> list[dict[str, Any]]:
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 30
        return rows[:limit]

    return fake


def test_merged_qualifiers_slice_the_window_by_week_and_end_open() -> None:
    assert epic_audit._merged_qualifiers("2026-06-01", date(2026, 6, 20)) == [
        "merged:2026-06-01..2026-06-07",
        "merged:2026-06-08..2026-06-14",
        "merged:>=2026-06-15",
    ]


def test_merged_qualifiers_within_a_week_is_one_open_slice() -> None:
    assert epic_audit._merged_qualifiers("2026-06-01", date(2026, 6, 7)) == ["merged:>=2026-06-01"]


def test_task_drift_slices_end_today_by_default() -> None:
    since = datetime.now(UTC).date().isoformat()
    with patch("vergil_tooling.lib.github.read_json", return_value=[]) as search:
        assert (
            epic_audit.task_drift(
                since, org="org", stops=trust.BatchStops(), authors=trust_stubs.AUTHORS
            )
            == []
        )
    search.assert_called_once()
    assert f"merged:>={since}" in search.call_args.args


def _merged(count: int) -> list[dict[str, Any]]:
    return [
        {"number": i, "repository": {"nameWithOwner": "org/repo"}, "url": f"u{i}", "body": ""}
        for i in range(1, count + 1)
    ]


def test_merged_prs_sees_a_pr_past_row_100_and_reads_every_slice() -> None:
    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> list[dict[str, Any]]:
        calls.append(args)
        return _limited(_merged(101))(*args)

    with patch("vergil_tooling.lib.github.read_json", side_effect=fake):
        prs = epic_audit._merged_prs("2026-06-01", org="org", today=date(2026, 6, 8))
    assert len(prs) == 202  # two slices of 101 each
    assert [c[4] for c in calls] == ["merged:2026-06-01..2026-06-07", "merged:>=2026-06-08"]


def test_task_drift_refuses_a_slice_that_may_be_truncated() -> None:
    with (
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=_limited(_merged(epics.LISTING_LIMIT)),
        ),
        pytest.raises(epics.EpicListTruncatedError, match="merged:>=2026-06-01"),
    ):
        _drift([], "org")


def _closed_operational(count: int) -> list[dict[str, Any]]:
    return [{"number": i, "repository": {"nameWithOwner": "org/repo"}} for i in range(1, count + 1)]


def test_closed_operational_without_success_sees_an_issue_past_row_30() -> None:
    search = _limited(_closed_operational(31))

    def fake(*args: str) -> object:
        if args[0] == "search":
            return search(*args) if args[5] == "validation" else []
        raise AssertionError(args)

    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=fake),
        patch("vergil_tooling.lib.github.graphql", side_effect=trust_stubs.FakeItems()),
        patch(f"{_AUDIT}.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        result = epic_audit.closed_operational_without_success("org")
    assert len(result) == 31


def test_closed_operational_without_success_refuses_a_search_that_may_be_truncated() -> None:
    with (
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=_limited(_closed_operational(epics.LISTING_LIMIT)),
        ),
        patch(f"{_AUDIT}.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        pytest.raises(epics.EpicListTruncatedError, match="closed .* issues"),
    ):
        epic_audit.closed_operational_without_success("org")


def _open_epics(count: int) -> list[dict[str, Any]]:
    return [{"number": i, "repository": {"nameWithOwner": "org/repo"}} for i in range(1, count + 1)]


def test_epic_outside_dotgithub_sees_an_epic_past_row_30() -> None:
    with patch("vergil_tooling.lib.github.read_json", side_effect=_limited(_open_epics(31))):
        assert len(epic_audit.epic_outside_dotgithub("org")) == 31


def test_epic_outside_dotgithub_refuses_a_search_that_may_be_truncated() -> None:
    with (
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=_limited(_open_epics(epics.LISTING_LIMIT)),
        ),
        pytest.raises(epics.EpicListTruncatedError, match="open epics"),
    ):
        epic_audit.epic_outside_dotgithub("org")


def _unlabelled(count: int) -> list[dict[str, Any]]:
    return [{"number": i, "labels": []} for i in range(1, count + 1)]


def test_stray_dotgithub_issue_sees_a_stray_past_row_500() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_limited(_unlabelled(501))),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        assert len(epic_audit.stray_dotgithub_issue("org")) == 501


def test_stray_dotgithub_issue_refuses_a_list_that_may_be_truncated() -> None:
    with (
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=_limited(_unlabelled(epics.LISTING_LIMIT)),
        ),
        pytest.raises(epics.EpicListTruncatedError, match="open issues of org/.github"),
    ):
        epic_audit.stray_dotgithub_issue("org")


def test_epic_drift_skips_proxy() -> None:
    # A proxy whose tasks are all closed is not drift: it lives as long as its
    # primary (#1417), so the daily sweep's drift close never closes it. The
    # finite epic beside it, all closed too, still is drift.
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u40",
        },
        {
            "number": 9,
            "title": "Epic (proxy): keep ad-hoc queues standalone",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "proxy"}],
            "url": "u9",
        },
    ]
    closed = [epics.ChildState(epics.IssueRef("vergil-project", "vergil-tooling", 1), "CLOSED")]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=closed),
        patch("vergil_tooling.lib.github.graphql", side_effect=trust_stubs.FakeItems()),
    ):
        result = _epic_drift("vergil-project")
    assert [e.number for e in result] == [40]


# --- proxy children and the link check (epic .github#1417) ---------------------

_PRIMARY = epics.IssueRef("vergil-project", ".github", 1417)
_PROXY = epics.IssueRef("logical-minds-foundry", ".github", 320)
_PX = "vergil_tooling.lib.proxies"


def _child(owner: str, repo: str, number: int, state: str = "OPEN") -> epics.ChildState:
    return epics.ChildState(epics.IssueRef(owner, repo, number), state, f"task {number}")


def test_epic_children_includes_proxy_tasks() -> None:
    # The primary's own children, then each proxy's, each read under its own
    # org's token.
    own = [_child("vergil-project", "vergil-tooling", 1)]
    proxied = [_child("logical-minds-foundry", "mq-rest-admin", 7)]
    seen: list[tuple[epics.IssueRef, str | None]] = []

    def child_states(epic: epics.IssueRef) -> list[epics.ChildState]:
        seen.append((epic, github._target_org.get()))
        return own if epic == _PRIMARY else proxied

    with (
        patch("vergil_tooling.lib.epics.child_states", side_effect=child_states),
        patch(f"{_PX}.proxies_of", return_value=(_PROXY,)),
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
    ):
        children = epic_audit.epic_children(_PRIMARY)
    assert children == own + proxied
    assert seen == [(_PRIMARY, "vergil-project"), (_PROXY, "logical-minds-foundry")]


def test_epic_children_without_proxies_is_child_states() -> None:
    own = [_child("vergil-project", "vergil-tooling", 1)]
    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=own),
        patch(f"{_PX}.proxies_of", return_value=()),
    ):
        assert epic_audit.epic_children(_PRIMARY) == own


def test_epic_children_bad_proxy_fails() -> None:
    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
        patch(f"{_PX}.proxies_of", return_value=(_PROXY,)),
        patch(
            f"{_PX}.primary_of",
            side_effect=proxies.ProxyLinkError("proxy has no 'Primary:' line in its body"),
        ),
        pytest.raises(proxies.ProxyLinkError, match="no 'Primary:' line"),
    ):
        epic_audit.epic_children(_PRIMARY)


def test_epic_children_proxy_naming_another_primary_fails() -> None:
    other = epics.IssueRef("vergil-project", ".github", 9)
    with (
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
        patch(f"{_PX}.proxies_of", return_value=(_PROXY,)),
        patch(f"{_PX}.primary_of", return_value=other),
        pytest.raises(
            proxies.ProxyLinkError, match=r"'Primary:' line names vergil-project/\.github#9"
        ),
    ):
        epic_audit.epic_children(_PRIMARY)


# --- each child's open blockers (#5124) ----------------------------------------

_EPICS = "vergil_tooling.lib.epics"


def _blocker(owner: str, repo: str, number: int, state: str) -> epics.BlockerState:
    return epics.BlockerState(epics.IssueRef(owner, repo, number), state)


def test_epic_children_with_blockers_reports_open_native_and_body_blockers() -> None:
    # An open child's open blockers are its native links not closed (a blocker
    # in another org included) and its Blocked-by: lines not closed, each once.
    native = (
        _blocker("other-org", "lib", 131, "OPEN"),
        _blocker("vergil-project", "vergil-tooling", 7, "CLOSED"),
        _blocker("vergil-project", "vergil-tooling", 8, "OPEN"),
    )
    open_child = replace(_child("vergil-project", "vergil-tooling", 1), native_blockers=native)
    closed_child = _child("vergil-project", "vergil-tooling", 2, "CLOSED")
    body = [
        epics.IssueRef("vergil-project", "vergil-tooling", 8),  # also native
        epics.IssueRef("vergil-project", "vergil-tooling", 9),
        epics.IssueRef("vergil-project", "vergil-tooling", 10),
    ]
    states = {9: "OPEN", 10: "CLOSED"}
    with (
        patch(
            f"{_EPICS}.child_states_with_blockers", return_value=[open_child, closed_child]
        ) as cs,
        patch(f"{_EPICS}.blockers_of", return_value=body) as bo,
        patch(f"{_EPICS}.issue_state", side_effect=lambda ref: states[ref.number]) as st,
        patch(f"{_EPICS}.native_blockers_of") as nb,
        patch(f"{_PX}.proxies_of", return_value=()),
    ):
        listed = epic_audit.epic_children_with_blockers(_PRIMARY)
    assert listed == [
        epic_audit.ListedChild(
            open_child,
            (
                epics.IssueRef("other-org", "lib", 131),
                epics.IssueRef("vergil-project", "vergil-tooling", 8),
                epics.IssueRef("vergil-project", "vergil-tooling", 9),
            ),
        ),
        epic_audit.ListedChild(closed_child, ()),
    ]
    # Only the open children's lists are checked; a closed child's body is not read.
    checked = cs.call_args.kwargs["checked"]
    assert checked(open_child) and not checked(closed_child)
    bo.assert_called_once_with(open_child.ref)
    assert sorted(c.args[0].number for c in st.call_args_list) == [9, 10]
    nb.assert_not_called()


def test_epic_children_with_blockers_reads_a_reflink_childs_native_list() -> None:
    # A reflink-fallback child carries no native list, so it is read on its own.
    child = _child("vergil-project", "vergil-tooling", 1)
    with (
        patch(f"{_EPICS}.child_states_with_blockers", return_value=[child]),
        patch(f"{_EPICS}.blockers_of", return_value=[]),
        patch(
            f"{_EPICS}.native_blockers_of",
            return_value=(_blocker("vergil-project", "vergil-tooling", 5, "OPEN"),),
        ) as nb,
        patch(f"{_PX}.proxies_of", return_value=()),
    ):
        listed = epic_audit.epic_children_with_blockers(_PRIMARY)
    nb.assert_called_once_with(child.ref)
    assert listed[0].open_blockers == (epics.IssueRef("vergil-project", "vergil-tooling", 5),)


def test_epic_children_with_blockers_includes_proxy_tasks_under_their_token() -> None:
    own = replace(_child("vergil-project", "vergil-tooling", 1), native_blockers=())
    proxied = replace(_child("logical-minds-foundry", "mq-rest-admin", 7), native_blockers=())
    seen: list[tuple[epics.IssueRef, str | None]] = []

    def listing(epic: epics.IssueRef, **_: object) -> list[epics.ChildState]:
        seen.append((epic, github._target_org.get()))
        return [own] if epic == _PRIMARY else [proxied]

    def body(ref: epics.IssueRef) -> list[epics.IssueRef]:
        seen.append((ref, github._target_org.get()))
        return []

    with (
        patch(f"{_EPICS}.child_states_with_blockers", side_effect=listing),
        patch(f"{_EPICS}.blockers_of", side_effect=body),
        patch(f"{_PX}.proxies_of", return_value=(_PROXY,)),
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
    ):
        listed = epic_audit.epic_children_with_blockers(_PRIMARY)
    assert [c.child for c in listed] == [own, proxied]
    assert seen == [
        (_PRIMARY, "vergil-project"),
        (own.ref, "vergil-project"),
        (_PROXY, "logical-minds-foundry"),
        (proxied.ref, "logical-minds-foundry"),
    ]


def test_epic_children_with_blockers_proxy_naming_another_primary_fails() -> None:
    with (
        patch(f"{_EPICS}.child_states_with_blockers", return_value=[]),
        patch(f"{_PX}.proxies_of", return_value=(_PROXY,)),
        patch(f"{_PX}.primary_of", return_value=epics.IssueRef("vergil-project", ".github", 9)),
        pytest.raises(proxies.ProxyLinkError, match="'Primary:' line names"),
    ):
        epic_audit.epic_children_with_blockers(_PRIMARY)


@pytest.mark.parametrize(
    ("failing", "error"),
    [
        (
            "native_blockers_of",
            epics.NativeBlockersReadError("x has 2 native blockers but only 1 could be read"),
        ),
        ("blockers_of", trust.TrustReadError("read_issue x", "truncated")),
    ],
    ids=["native", "body"],
)
def test_epic_children_with_blockers_incomplete_read_raises(failing: str, error: Exception) -> None:
    # A read that cannot complete fails the listing, never "no blockers".
    child = _child("vergil-project", "vergil-tooling", 1)
    with (
        patch(f"{_EPICS}.child_states_with_blockers", return_value=[child]),
        patch(f"{_EPICS}.native_blockers_of", return_value=()),
        patch(f"{_EPICS}.blockers_of", return_value=[]),
        # Applied last, so it overrides the matching patch above.
        patch(f"{_EPICS}.{failing}", side_effect=error),
        patch(f"{_PX}.proxies_of", return_value=()),
        pytest.raises(type(error)),
    ):
        epic_audit.epic_children_with_blockers(_PRIMARY)


def test_render_epic_children_with_blockers_adds_the_new_fields() -> None:
    open_child = _child("vergil-project", "vergil-tooling", 1)
    free_child = _child("vergil-project", "vergil-tooling", 2)
    closed_child = _child("vergil-project", "vergil-tooling", 3, "CLOSED")
    listed = [
        epic_audit.ListedChild(closed_child),
        epic_audit.ListedChild(free_child),
        epic_audit.ListedChild(open_child, (epics.IssueRef("other-org", "lib", 131),)),
    ]
    out = json.loads(epic_audit.render_epic_children(_PRIMARY, listed, containers=1))
    assert out["children"] == [
        {
            "repo": "vergil-project/vergil-tooling",
            "number": 1,
            "title": "task 1",
            "state": "OPEN",
            "open_blockers": ["other-org/lib#131"],
            "blocked": True,
        },
        {
            "repo": "vergil-project/vergil-tooling",
            "number": 2,
            "title": "task 2",
            "state": "OPEN",
            "open_blockers": [],
            "blocked": False,
        },
        {
            "repo": "vergil-project/vergil-tooling",
            "number": 3,
            "title": "task 3",
            "state": "CLOSED",
            "open_blockers": [],
            "blocked": False,
        },
    ]


def _link_problems(
    epic: epics.IssueRef,
    *,
    proxy_refs: set[epics.IssueRef],
    primary_of: Any,
    proxies_of: Any,
) -> list[str]:
    with (
        patch(f"{_PX}.is_proxy_ref", side_effect=lambda ref: ref in proxy_refs),
        patch(f"{_PX}.primary_of", side_effect=primary_of),
        patch(f"{_PX}.proxies_of", side_effect=proxies_of),
    ):
        return epic_audit.proxy_link_problems(epic)


def test_proxy_link_problems_agreeing_pair_is_clean() -> None:
    for epic in (_PRIMARY, _PROXY):
        assert (
            _link_problems(
                epic,
                proxy_refs={_PROXY},
                primary_of=lambda _ref: _PRIMARY,
                proxies_of=lambda ref: (_PROXY,) if ref == _PRIMARY else (),
            )
            == []
        )


def test_proxy_link_problems_proxy_without_comment_on_primary() -> None:
    problems = _link_problems(
        _PROXY, proxy_refs={_PROXY}, primary_of=lambda _ref: _PRIMARY, proxies_of=lambda _ref: ()
    )
    assert problems == [
        "proxy logical-minds-foundry/.github#320 names vergil-project/.github#1417 as its"
        " primary, but vergil-project/.github#1417 has no trusted"
        " 'Proxy: logical-minds-foundry/.github#320' comment"
    ]


def test_proxy_link_problems_proxy_with_bad_primary_line() -> None:
    def primary_of(_ref: epics.IssueRef) -> epics.IssueRef:
        raise proxies.ProxyLinkError("proxy has 2 'Primary:' lines in its body")

    problems = _link_problems(
        _PROXY, proxy_refs={_PROXY}, primary_of=primary_of, proxies_of=lambda _ref: ()
    )
    assert problems == ["proxy has 2 'Primary:' lines in its body"]


def test_proxy_link_problems_primary_lists_a_disagreeing_proxy() -> None:
    other = epics.IssueRef("vergil-project", ".github", 9)
    problems = _link_problems(
        _PRIMARY,
        proxy_refs={_PROXY},
        primary_of=lambda _ref: other,
        proxies_of=lambda _ref: (_PROXY,),
    )
    assert problems == [
        "vergil-project/.github#1417 lists the proxy logical-minds-foundry/.github#320,"
        " whose 'Primary:' line names vergil-project/.github#9"
    ]


def test_proxy_link_problems_primary_lists_a_non_proxy() -> None:
    problems = _link_problems(
        _PRIMARY,
        proxy_refs=set(),
        primary_of=lambda _ref: _PRIMARY,
        proxies_of=lambda _ref: (_PROXY,),
    )
    assert len(problems) == 1
    assert "which is not a proxy epic" in problems[0]


def test_proxy_link_problems_primary_lists_a_missing_proxy() -> None:
    def is_proxy_ref(ref: epics.IssueRef) -> bool:
        if ref == _PROXY:
            raise github.GitHubAPIError(1, "gh", "", "Could not resolve to an issue")
        return False

    with (
        patch(f"{_PX}.is_proxy_ref", side_effect=is_proxy_ref),
        patch(f"{_PX}.proxies_of", return_value=(_PROXY,)),
    ):
        problems = epic_audit.proxy_link_problems(_PRIMARY)
    assert len(problems) == 1
    assert problems[0].startswith(
        "vergil-project/.github#1417 lists the proxy logical-minds-foundry/.github#320,"
        " which could not be read:"
    )


def test_proxy_link_problems_malformed_trusted_proxy_line() -> None:
    def proxies_of(_ref: epics.IssueRef) -> tuple[epics.IssueRef, ...]:
        raise proxies.ProxyLinkError("malformed line in a comment on vergil-project/.github#1417")

    problems = _link_problems(
        _PRIMARY, proxy_refs=set(), primary_of=lambda _ref: _PRIMARY, proxies_of=proxies_of
    )
    assert problems == ["malformed line in a comment on vergil-project/.github#1417"]


def _epic_record(number: int, state: str, labels: list[str]) -> dict[str, Any]:
    return {"number": number, "state": state, "labels": labels}


def test_proxy_link_drift_checks_only_open_proxies() -> None:
    home = "logical-minds-foundry/.github"
    listing = [
        _epic_record(320, "OPEN", ["epic", "proxy"]),
        _epic_record(321, "CLOSED", ["epic", "proxy"]),
        _epic_record(5, "OPEN", ["epic"]),
    ]
    checked: list[epics.IssueRef] = []

    def proxy_side(proxy: epics.IssueRef) -> list[str]:
        checked.append(proxy)
        return [f"problem with {proxy.slug}"]

    with patch.object(epic_audit, "_proxy_side_problems", side_effect=proxy_side):
        problems = epic_audit.proxy_link_drift(
            home, stops=trust.BatchStops(), listing=cast("list[epic_docs.EpicRecord]", listing)
        )
    assert checked == [_PROXY]
    assert problems == ["problem with logical-minds-foundry/.github#320"]


def test_render_reports_proxy_link_drift() -> None:
    out = epic_audit.render(
        [], [], org="org", window_days=30, proxy_link_drift=["proxy org/.github#3 is broken"]
    )
    assert "## Proxy link drift" in out
    assert "- proxy org/.github#3 is broken" in out
    assert "No drift" not in out


def test_render_epic_children_primary_key_only_on_a_proxy() -> None:
    children = [_child("logical-minds-foundry", "mq-rest-admin", 7)]
    on_proxy = json.loads(
        epic_audit.render_epic_children(_PROXY, children, containers=1, primary=_PRIMARY)
    )
    assert on_proxy["primary"] == "vergil-project/.github#1417"
    assert list(on_proxy) == ["epic", "primary", "containers", "children"]
    plain = json.loads(epic_audit.render_epic_children(_PRIMARY, children, containers=1))
    assert "primary" not in plain


def test_proxy_link_drift_lists_the_home_when_not_given() -> None:
    with (
        patch.object(epic_audit, "list_home_epics", return_value=[]) as listing,
        patch.object(epic_audit, "_proxy_side_problems") as proxy_side,
    ):
        assert epic_audit.proxy_link_drift("org/.github", stops=trust.BatchStops()) == []
    listing.assert_called_once_with("org/.github")
    proxy_side.assert_not_called()


def test_proxy_link_problems_unreadable_primary_of_a_proxy() -> None:
    def proxies_of(_ref: epics.IssueRef) -> tuple[epics.IssueRef, ...]:
        raise github.NoInstallationError("vergil-project", [])

    problems = _link_problems(
        _PROXY, proxy_refs={_PROXY}, primary_of=lambda _ref: _PRIMARY, proxies_of=proxies_of
    )
    assert len(problems) == 1
    assert problems[0].startswith("the link of proxy logical-minds-foundry/.github#320 could not")


def test_proxy_link_problems_primary_lists_a_proxy_with_a_bad_primary_line() -> None:
    def primary_of(_ref: epics.IssueRef) -> epics.IssueRef:
        raise proxies.ProxyLinkError(
            "proxy logical-minds-foundry/.github#320 has no 'Primary:' line"
        )

    problems = _link_problems(
        _PRIMARY, proxy_refs={_PROXY}, primary_of=primary_of, proxies_of=lambda _ref: (_PROXY,)
    )
    assert problems == ["proxy logical-minds-foundry/.github#320 has no 'Primary:' line"]


# --- the sweep's proxy close pass (epic .github#1417) -----------------------------


def _proxy_closures(  # noqa: PLR0913 - each a knob of the faked reads
    *,
    primary_state: str,
    children: list[epics.ChildState],
    primary_of: Any = None,
    closer: Any = trust_stubs.HUMAN,
    stops: trust.BatchStops | None = None,
    authors: TrustedAuthors = trust_stubs.AUTHORS,
) -> tuple[list[epic_audit.ProxyClosure], list[str]]:
    """proxy_closures of the proxy home, the primary's close (by *closer*) read by the layer."""
    listing = [
        {"number": 320, "state": "OPEN", "labels": ["epic", "proxy"]},
        {"number": 321, "state": "CLOSED", "labels": ["epic", "proxy"]},
        {"number": 5, "state": "OPEN", "labels": ["epic"]},
    ]
    items = trust_stubs.FakeItems(closers={("vergil-project/.github", 1417): closer}, strict=True)
    with (
        patch(f"{_PX}.primary_of", side_effect=primary_of or (lambda _ref: _PRIMARY)),
        patch("vergil_tooling.lib.epics.issue_state", return_value=primary_state),
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
    ):
        return epic_audit.proxy_closures(
            "logical-minds-foundry/.github",
            stops=trust.BatchStops() if stops is None else stops,
            authors=authors,
            listing=cast("list[epic_docs.EpicRecord]", listing),
        )


def test_proxy_closed_when_primary_closed_and_no_open_task(monkeypatch: pytest.MonkeyPatch) -> None:
    closures, errors = _proxy_closures(
        primary_state="CLOSED", children=[_child("logical-minds-foundry", "x", 1, "CLOSED")]
    )
    assert errors == []
    assert closures == [epic_audit.ProxyClosure(_PROXY, _PRIMARY, 0)]
    graph = _proxy_graph(monkeypatch)
    drift = epic_audit.close_proxies(closures)
    assert drift == []
    assert graph.events == [
        ("close", _PROXY, "completed"),
        (
            "comment",
            _PROXY,
            "Closed by `vrg-epic-audit`: its primary epic vergil-project/.github#1417 is closed"
            " and no task under this proxy is open.",
        ),
    ]


def _proxy_graph(monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    """A graph holding the open proxy :data:`_PROXY`, its chain the head alone."""
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_PROXY, title="Proxy", labels=["epic", "proxy"])
    return graph


def test_close_proxies_closes_whole_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxy closes with its chain, head first (spec §4.6 of .github#1609).
    graph = _proxy_graph(monkeypatch)
    c2 = graph.add_continuation(
        epics.IssueRef("logical-minds-foundry", ".github", 321), _PROXY, _PROXY
    )
    graph.clear_log()
    drift = epic_audit.close_proxies([epic_audit.ProxyClosure(_PROXY, _PRIMARY, 0)])
    assert drift == []
    assert [event[:2] for event in graph.events] == [
        ("close", _PROXY),
        ("close", c2),
        ("comment", _PROXY),
    ]


def test_proxy_with_open_task_after_primary_closed_is_drift() -> None:
    # A primary closed (completed or not planned) while a proxy task is open:
    # the proxy stays open and is reported, never closed silently.
    closures, errors = _proxy_closures(
        primary_state="CLOSED",
        children=[
            _child("logical-minds-foundry", "x", 1, "OPEN"),
            _child("logical-minds-foundry", "x", 2, "CLOSED"),
        ],
    )
    assert errors == []
    assert closures == [epic_audit.ProxyClosure(_PROXY, _PRIMARY, 1)]
    run = MagicMock()
    with patch("vergil_tooling.lib.github.run", run):
        drift = epic_audit.close_proxies(closures)
    run.assert_not_called()
    assert drift == [
        "logical-minds-foundry/.github#320 — its primary vergil-project/.github#1417 is closed,"
        " but 1 task(s) under it are still open; left open"
    ]


def test_proxy_left_open_while_primary_open() -> None:
    closures, errors = _proxy_closures(primary_state="OPEN", children=[])
    assert (closures, errors) == ([], [])


def test_proxy_bad_primary_line_is_an_error_not_raised() -> None:
    def primary_of(_ref: epics.IssueRef) -> epics.IssueRef:
        raise proxies.ProxyLinkError("proxy has no 'Primary:' line in its body")

    closures, errors = _proxy_closures(primary_state="CLOSED", children=[], primary_of=primary_of)
    assert closures == []
    assert errors == ["proxy has no 'Primary:' line in its body"]


def test_proxy_unreadable_primary_is_an_error_not_raised() -> None:
    listing = [{"number": 320, "state": "OPEN", "labels": ["epic", "proxy"]}]
    with (
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
        patch(
            "vergil_tooling.lib.epics.issue_state",
            side_effect=github.GitHubAPIError(1, "gh", "", "HTTP 404"),
        ),
        patch.object(epic_audit, "list_home_epics", return_value=listing) as list_epics,
    ):
        closures, errors = epic_audit.proxy_closures(
            "logical-minds-foundry/.github", stops=trust.BatchStops(), authors=trust_stubs.AUTHORS
        )
    list_epics.assert_called_once_with("logical-minds-foundry/.github")
    assert closures == []
    assert len(errors) == 1
    assert errors[0].startswith("proxy logical-minds-foundry/.github#320 could not be judged:")


def test_render_closed_reports_proxy_pass() -> None:
    out = epic_audit.render_closed(
        [],
        org="org",
        window_days=30,
        proxies_closed=["org/.github#4"],
        proxy_drift=["org/.github#5 — left open"],
        proxy_errors=["proxy org/.github#3 broke"],
    )
    assert "## Proxies closed (primary closed, no open task)" in out
    assert "- org/.github#4" in out
    assert "**Proxies left open**" in out
    assert "- org/.github#5 — left open" in out
    assert "**Proxy link errors**" in out
    assert "- proxy org/.github#3 broke" in out
    assert "nothing to close" not in out


# --- misplaced ad-hoc tasks (epic .github#1417, spec §4.8) ------------------------

_LIVE_ADHOC_REF = epics.IssueRef("vergil-project", ".github", 50)


def _adhoc_child(
    number: int,
    *,
    title: str = "a task",
    body: str = "",
    labels: frozenset[str] = frozenset(),
    parent: epics.IssueRef | None = _LIVE_ADHOC_REF,
    repo: str = "vergil-tooling",
) -> epic_audit.OpenIssue:
    return epic_audit.OpenIssue(
        ref=epics.IssueRef("vergil-project", repo, number),
        title=title,
        body=body,
        labels=labels,
        native_parent=parent,
    )


_LIVE_ADHOC = epic_audit.OpenIssue(
    ref=_LIVE_ADHOC_REF,
    title="Epic (ad hoc): vergil-tooling",
    body="",
    labels=frozenset({"epic", "ad-hoc"}),
    native_parent=None,
)


def _misplaced(
    issues: list[epic_audit.OpenIssue],
    *,
    epics_by_slug: dict[str, bool] | None = None,
    comments: dict[int, list[dict[str, Any]]] | None = None,
    unreadable: frozenset[str] = frozenset(),
) -> tuple[list[epic_audit.MisplacedTask], MagicMock, MagicMock]:
    epics_by_slug = epics_by_slug or {}
    comments = comments or {}

    def is_epic(ref: epics.IssueRef) -> bool:
        if ref.slug in unreadable:
            raise github.GitHubAPIError(1, "gh", "", "Could not resolve to an issue")
        return epics_by_slug.get(ref.slug, False)

    # Each task's comments, read through the real trust layer.
    layer = trust_stubs.FakeItems(
        {
            ("vergil-project/vergil-tooling", number): trust_stubs.item(nodes=nodes)
            for number, nodes in comments.items()
        }
    )
    trusted = MagicMock(return_value=trust_stubs.AUTHORS)
    run = MagicMock()
    with (
        patch("vergil_tooling.lib.epics.is_epic", side_effect=is_epic),
        patch("vergil_tooling.lib.github.read_json", side_effect=AssertionError("no gh read")),
        patch("vergil_tooling.lib.github.graphql", side_effect=layer),
        patch(f"{_AUDIT}.load_trusted_authors", trusted),
        patch("vergil_tooling.lib.trust.load_trusted_authors", side_effect=AssertionError),
        patch("vergil_tooling.lib.github.run", run),
    ):
        found = epic_audit.misplaced_adhoc("vergil-project", issues=[_LIVE_ADHOC, *issues])
    return found, trusted, run


def test_misplaced_logbook_title() -> None:
    found, _, _ = _misplaced([_adhoc_child(1, title="Logbook: 2026-11-19")])
    assert found == [
        epic_audit.MisplacedTask(
            epics.IssueRef("vergil-project", "vergil-tooling", 1),
            "titled `Logbook:`; it belongs under the hoshin epic",
        )
    ]


def test_misplaced_cross_org_epic_ref() -> None:
    body = (
        "Part of logical-minds-foundry/.github#12, see also"
        " https://github.com/logical-minds-foundry/.github/issues/12 and vergil-project/.github#1."
    )
    found, _, _ = _misplaced(
        [_adhoc_child(2, body=body)],
        epics_by_slug={"logical-minds-foundry/.github#12": True, "vergil-project/.github#1": True},
    )
    assert [m.reason for m in found] == [
        "names logical-minds-foundry/.github#12, an epic in another org"
    ]


def test_misplaced_cross_org_url_form() -> None:
    body = "Primary: https://github.com/other-org/.github/issues/7"
    found, _, _ = _misplaced(
        [_adhoc_child(2, body=body)], epics_by_slug={"other-org/.github#7": True}
    )
    assert [m.reason for m in found] == ["names other-org/.github#7, an epic in another org"]


def test_cross_org_ref_to_non_epic_not_reported() -> None:
    found, trusted, _ = _misplaced([_adhoc_child(3, body="Upstream bug: other-org/lib#99")])
    assert found == []
    trusted.assert_not_called()  # no candidate, so the trusted authors are never read


def test_cross_org_ref_unreadable_reported() -> None:
    found, _, _ = _misplaced(
        [_adhoc_child(4, body="See secret-org/private#3")],
        unreadable=frozenset({"secret-org/private#3"}),
    )
    assert [m.reason for m in found] == ["names secret-org/private#3, which could not be read"]


def test_misplaced_only_under_a_live_adhoc_epic() -> None:
    other_epic = epics.IssueRef("vergil-project", ".github", 1417)
    found, _, _ = _misplaced(
        [
            _adhoc_child(5, title="Logbook: under a development epic", parent=other_epic),
            _adhoc_child(6, title="Logbook: no parent", parent=None),
        ]
    )
    assert found == []


@pytest.mark.parametrize("login", ["vergil-user", "app/vergil-user", "vergil-user[bot]"])
def test_standalone_confirmed_suppresses(login: str) -> None:
    agent = {"__typename": "Bot", "login": login}
    comments = {7: [trust_stubs.comment(1, "Ruled:\n  Standalone-confirmed  \n", agent)]}
    found, _, _ = _misplaced([_adhoc_child(7, title="Logbook: x")], comments=comments)
    assert found == []


def test_standalone_confirmed_untrusted_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    comments = {
        8: [
            trust_stubs.comment(1, "Standalone-confirmed", trust_stubs.OUTSIDER),
            trust_stubs.comment(2, "not Standalone-confirmed here", trust_stubs.HUMAN),
        ]
    }
    found, _, _ = _misplaced([_adhoc_child(8, title="Logbook: x")], comments=comments)
    assert len(found) == 1
    assert "on comment 1 (untrusted)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("author", "editor", "kind", "login", "at", "cause"),
    [
        (trust_stubs.IMPOSTOR, None, "comment", "vergil-user", trust_stubs.CREATED, "wrong-type"),
        (
            trust_stubs.AGENT,
            trust_stubs.OUTSIDER,
            "edit",
            "drive-by",
            trust_stubs.EDITED,
            "untrusted",
        ),
    ],
    ids=["user-named-like-app", "edited-by-outsider"],
)
def test_standalone_confirmed_from_impostor_does_not_drop_the_task(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    author: Any,
    editor: Any,
    kind: str,
    login: str,
    at: str,
    cause: str,
) -> None:
    # A Standalone-confirmed line counts only from a comment the layer trusts:
    # a User account named like the agent App (or an outsider's edit of a
    # trusted comment) leaves the misplaced task reported, metadata only.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    forged = f"{trust_stubs.SPAM_TEXT}\nStandalone-confirmed\n"
    comments = {14: [trust_stubs.comment(41, forged, author, editor=editor)]}
    found, _, _ = _misplaced([_adhoc_child(14, title="Logbook: x")], comments=comments)
    assert found == [
        epic_audit.MisplacedTask(
            epics.IssueRef("vergil-project", "vergil-tooling", 14),
            "titled `Logbook:`; it belongs under the hoshin epic",
        )
    ]
    out, err = capsys.readouterr()
    assert err == (
        f"skipped untrusted content: vergil-project/vergil-tooling#14 {kind} {login} at {at}"
        f" on comment 41 ({cause})\n"
    )
    assert "SPAM-MARKER-TEXT" not in out + err
    assert "Standalone-confirmed" not in out + err


def test_misplaced_report_never_writes() -> None:
    _, _, run = _misplaced(
        [_adhoc_child(9, title="Logbook: x", body="other-org/.github#7")],
        epics_by_slug={"other-org/.github#7": True},
    )
    run.assert_not_called()


def test_misplaced_reads_each_cross_org_ref_once_and_enumerates_when_not_given() -> None:
    issues = [
        _LIVE_ADHOC,
        _adhoc_child(10, body="other-org/.github#7"),
        _adhoc_child(11, body="other-org/.github#7"),
    ]
    is_epic = MagicMock(return_value=False)
    with (
        patch.object(epic_audit, "open_issues", return_value=issues) as enum,
        patch("vergil_tooling.lib.epics.is_epic", is_epic),
    ):
        assert epic_audit.misplaced_adhoc("vergil-project") == []
    enum.assert_called_once_with("vergil-project")
    is_epic.assert_called_once_with(epics.IssueRef("other-org", ".github", 7))


def test_standalone_confirmed_bad_response_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.graphql", return_value={"repository": None}),
        pytest.raises(trust.TrustReadError, match="read_issue o/r#1"),
    ):
        epic_audit._standalone_confirmed(epics.IssueRef("o", "r", 1), trust_stubs.AUTHORS)


def test_render_reports_misplaced_adhoc() -> None:
    task = epics.IssueRef("org", "tool", 3)
    out = epic_audit.render(
        [],
        [],
        org="org",
        window_days=30,
        misplaced_adhoc=[epic_audit.MisplacedTask(task, "titled `Logbook:`")],
    )
    assert "## Ad-hoc tasks that look misplaced (report only)" in out
    assert "`Standalone-confirmed`" in out
    assert "- org/tool#3 — titled `Logbook:`" in out


def test_misplaced_loads_trusted_authors_once() -> None:
    found, trusted, _ = _misplaced(
        [_adhoc_child(12, title="Logbook: a"), _adhoc_child(13, title="Logbook: b")]
    )
    assert [m.task.number for m in found] == [12, 13]
    trusted.assert_called_once_with()


# --- trusted content in the sweep (epic vergil-project/.github#1467, Task 8b) ----

_SPAM = trust_stubs.SPAM_TEXT
_REPO = "vergil-project/vergil-tooling"


def _pr_reffing(number: int, body: str) -> dict[str, Any]:
    return {
        "number": number,
        "repository": {"nameWithOwner": _REPO},
        "url": f"u{number}",
        "body": body,
    }


def _open_tasks(prs: list[dict[str, Any]], *numbers: int) -> Any:
    """A read_json fake: the search answers *prs*; each task in *numbers* is open."""

    def read_json(*args: str) -> object:
        if args[0] == "search":
            return prs
        state = "open" if int(args[2]) in numbers else "closed"
        return {"state": state, "title": f"feat: task {args[2]}", "body": "task body"}

    return read_json


@pytest.mark.parametrize(
    ("author", "cause"),
    [(trust_stubs.OUTSIDER, "untrusted"), (trust_stubs.IMPOSTOR, "wrong-type")],
    ids=["outsider", "user-named-like-app"],
)
def test_task_drift_ignores_untrusted_closing_line(
    author: Any, cause: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A linkage line counts only from a trusted PR body: an untrusted one is
    # reported, metadata only, and closes nothing; the run carries on clean.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    body = f"Ref #1947\n{_SPAM}\n"
    prs = [_pr_reffing(1948, body)]
    stops = trust.BatchStops()
    untrusted = trust_stubs.item(body=body, author=author, typename="PullRequest")
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_open_tasks(prs, 1947)),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        result = _drift(prs, "vergil-project", stops=stops, items={f"{_REPO}#1948": untrusted})
    assert result == []
    assert stops.stopped == ()
    login = "drive-by" if author is trust_stubs.OUTSIDER else "vergil-user"
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: {_REPO}#1948 pr {login} at {trust_stubs.CREATED}"
        f" on body ({cause})\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_task_drift_ignores_body_edited_after_merge_by_outsider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every editor counts: an outsider who edits a trusted PR body after its
    # merge to add a linkage line makes the whole body untrusted.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    body = f"Ref #1947\nCloses #2000\n{_SPAM}\n"
    prs = [_pr_reffing(1948, body)]
    edited = trust_stubs.item(body=body, editor=trust_stubs.OUTSIDER, typename="PullRequest")
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_open_tasks(prs, 1947, 2000)),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        assert _drift(prs, "vergil-project", items={f"{_REPO}#1948": edited}) == []
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: {_REPO}#1948 edit drive-by at {trust_stubs.EDITED}"
        " on body (untrusted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_task_drift_takes_the_refs_from_the_trusted_body() -> None:
    # The search's copy of the body only picks the candidates: a task the
    # trusted body, read again through the layer, does not link is not drift.
    prs = [_pr_reffing(50, "Closes #10\nCloses #11\n")]
    trusted = trust_stubs.item(body="Closes #10\nRef #12\n", typename="PullRequest")
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_open_tasks(prs, 10, 11)),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        result = _drift(prs, "vergil-project", items={f"{_REPO}#50": trusted})
    assert result == [TaskDrift(_REPO, 10, 50, "u50")]


def test_task_drift_counts_only_the_closing_roles() -> None:
    # #1440's closing-reference roles: a PR body by the org's automation App
    # is not a closing reference, though its comments would be trusted.
    authors = TrustedAuthors(
        human=("human",), agents=("vergil-user",), orgs={"vergil-project": OrgConfig("vp-auto")}
    )
    automation = {"__typename": "Bot", "login": "vp-auto"}
    prs = [_pr_reffing(60, "Ref #59\n")]
    items = trust_stubs.FakeItems(
        {(_REPO, 60): trust_stubs.item(body="Ref #59\n", author=automation, typename="PullRequest")}
    )
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_open_tasks(prs, 59)),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
    ):
        result = epic_audit.task_drift(
            "2026-06-01",
            today=_TODAY,
            org="vergil-project",
            stops=trust.BatchStops(),
            authors=authors,
        )
    assert result == []


def test_task_drift_unreadable_pr_body_is_per_item_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A PR body the layer cannot read in full stops that PR alone; the other
    # PR's drift is still found.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    prs = [_pr_reffing(70, "Ref #69\n"), _pr_reffing(72, "Ref #71\n")]
    stops = trust.BatchStops()
    truncated = trust_stubs.item(body="Ref #69\n", typename="PullRequest")
    edits = truncated["repository"]["issueOrPullRequest"]["userContentEdits"]
    edits["pageInfo"] = {"hasNextPage": True, "endCursor": None}
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_open_tasks(prs, 69, 71)),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        result = _drift(prs, "vergil-project", stops=stops, items={f"{_REPO}#70": truncated})
    assert result == [TaskDrift(_REPO, 71, 72, "u72")]
    assert stops.stopped == (trust.Skipped(_REPO, 70, "pr", None, "", "body", trust.READ_FAILURE),)
    stops.print_summary(sys.stderr)
    assert f"stopped on a read failure: {_REPO}#70 on body: " in capsys.readouterr().err


def test_task_drift_skips_are_counts_under_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    prs = [_pr_reffing(1948, "Ref #1947\n")]
    untrusted = trust_stubs.item(
        body="Ref #1947\n", author=trust_stubs.OUTSIDER, typename="PullRequest"
    )
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=_open_tasks(prs, 1947)),
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
    ):
        assert _drift(prs, "vergil-project", items={f"{_REPO}#1948": untrusted}) == []
    assert capsys.readouterr().err == "skipped 1 untrusted item(s) (untrusted: 1)\n"


def test_nativize_skips_untrusted_body_parent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A Parent: line counts only from a trusted body: #5 (an outsider's body)
    # and #6 (a trusted body an outsider edited) are reported and never
    # nativized, so each is an orphan; #7's trusted line is nativized.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    line = f"Parent: org/.github#40\n{_SPAM}\n"
    issues = [_issue("org/tool", n, body=line) for n in (5, 6, 7)]
    items = {
        ("org/tool", 5): trust_stubs.item(body=line, author=trust_stubs.OUTSIDER),
        ("org/tool", 6): trust_stubs.item(body=line, editor=trust_stubs.OUTSIDER),
    }
    with (
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes),
    ):
        parents = _judged_parents(issues, items=items)
        orphans = epic_audit.orphan_drift("org", parents=parents, issues=issues)
    task, epic = epics.IssueRef("org", "tool", 7), epics.IssueRef("org", ".github", 40)
    assert parents.pairs == ((task, epic),)
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(epic, title="Live", labels=["epic"])
    graph.add_issue(task, title="task 7")
    assert epic_audit.nativize_parents(list(parents.pairs)) == epic_audit.LinkPass(linked=1)
    assert graph.mutations == [("add", epic, task)]
    assert [o.ref.number for o in orphans] == [5, 6]
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: org/tool#5 issue drive-by at {trust_stubs.CREATED}"
        " on body (untrusted)\n"
        f"skipped untrusted content: org/tool#6 edit drive-by at {trust_stubs.EDITED}"
        " on body (untrusted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_body_only_parents_takes_the_parent_from_the_trusted_body() -> None:
    # The listed body only picks the candidate: the pair is the trusted body's.
    issues = [_issue("org/tool", 5, body="Parent: org/.github#40\n")]
    items = {("org/tool", 5): trust_stubs.item(body="Parent: org/.github#41\n")}
    with patch("vergil_tooling.lib.epics.is_epic", return_value=True) as is_epic:
        parents = _judged_parents(issues, items=items)
    assert parents.pairs == (
        (epics.IssueRef("org", "tool", 5), epics.IssueRef("org", ".github", 41)),
    )
    assert [c.args[0].number for c in is_epic.call_args_list] == [40, 41]


def test_body_only_parents_unreadable_body_is_held_and_stops() -> None:
    # A body the layer cannot read stops that issue alone: it is neither
    # nativized nor attached as an orphan.
    issues = [_issue("org/tool", 5, body="Parent: org/.github#40\n"), _issue("org/tool", 6)]
    stops = trust.BatchStops()
    with (
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch("vergil_tooling.lib.epics.queue_home", side_effect=_homes),
    ):
        parents = _judged_parents(
            issues, stops=stops, items={("org/tool", 5): {"repository": None}}
        )
        orphans = epic_audit.orphan_drift("org", parents=parents, issues=issues)
    assert parents.pairs == ()
    assert parents.held == frozenset({epics.IssueRef("org", "tool", 5)})
    assert [o.ref.number for o in orphans] == [6]
    assert stops.stopped == (
        trust.Skipped("org/tool", 5, "issue", None, "", "body", trust.READ_FAILURE),
    )


# -- unlabelled Security advisory: lines (spec 6.3 of .github#1331; #4996) ----

_ADVISORY_LINE = "Security advisory: org/.github GHSA-abcd-2345-wxyz\n"


def _judged_unlabelled(
    issues: list[epic_audit.OpenIssue],
    *,
    stops: trust.BatchStops | None = None,
    items: dict[tuple[str, int], Any] | None = None,
) -> tuple[list[epics.IssueRef], MagicMock]:
    """unlabelled_advisory_lines of *issues*, each body read through the real trust layer.

    Each issue's trusted body is its listed body, by the trusted human, unless
    *items* gives that issue's item answer. Also returns the graphql spy.
    """
    answers = {(f"{i.ref.owner}/{i.ref.repo}", i.ref.number): trust_stubs.item(body=i.body)
               for i in issues}  # fmt: skip
    answers.update(items or {})
    graphql = MagicMock(side_effect=trust_stubs.FakeItems(answers))
    with patch("vergil_tooling.lib.github.graphql", graphql):
        found = epic_audit.unlabelled_advisory_lines(
            "org",
            stops=trust.BatchStops() if stops is None else stops,
            authors=trust_stubs.AUTHORS,
            issues=issues,
        )
    return found, graphql


def test_unlabelled_advisory_lines_reports_only_unlabelled_issues_with_a_line() -> None:
    issues = [
        _issue("org/tool", 5, body=f"intro\n{_ADVISORY_LINE}"),  # broken pair
        _issue("org/tool", 6, labels=("security",), body=_ADVISORY_LINE),  # a real pair
        _issue("org/tool", 7, body="mentions Security advisory: in passing\n"),  # no exact line
        _issue("org/tool", 8),
    ]
    found, graphql = _judged_unlabelled(issues)
    assert found == [epics.IssueRef("org", "tool", 5)]
    # Only the candidate is read again through the trust layer.
    assert graphql.call_count == 1


def test_unlabelled_advisory_lines_takes_the_line_from_the_trusted_body() -> None:
    # The listed body only picks the candidate: a trusted body without the
    # line is no broken pair.
    issues = [_issue("org/tool", 5, body=_ADVISORY_LINE)]
    items = {("org/tool", 5): trust_stubs.item(body="no line here\n")}
    found, _ = _judged_unlabelled(issues, items=items)
    assert found == []


def test_unlabelled_advisory_lines_never_trusts_an_outsider_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    issues = [_issue("org/tool", 5, body=_ADVISORY_LINE)]
    items = {("org/tool", 5): trust_stubs.item(body=_ADVISORY_LINE, author=trust_stubs.OUTSIDER)}
    found, _ = _judged_unlabelled(issues, items=items)
    assert found == []
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: org/tool#5 issue drive-by at {trust_stubs.CREATED}"
        " on body (untrusted)\n"
    )
    assert "GHSA" not in err


def test_unlabelled_advisory_lines_enumerates_the_open_issues_by_default() -> None:
    with patch("vergil_tooling.lib.epic_audit.open_issues", return_value=[_issue("org/tool", 5)]):
        found = epic_audit.unlabelled_advisory_lines(
            "org", stops=trust.BatchStops(), authors=trust_stubs.AUTHORS
        )
    assert found == []


def test_unlabelled_advisory_lines_unreadable_body_stops_that_issue() -> None:
    issues = [
        _issue("org/tool", 5, body=_ADVISORY_LINE),
        _issue("org/tool", 6, body=_ADVISORY_LINE),
    ]
    stops = trust.BatchStops()
    found, _ = _judged_unlabelled(
        issues, stops=stops, items={("org/tool", 5): {"repository": None}}
    )
    assert found == [epics.IssueRef("org", "tool", 6)]
    assert stops.stopped == (
        trust.Skipped("org/tool", 5, "issue", None, "", "body", trust.READ_FAILURE),
    )


def _all_closed(*numbers: int) -> list[roadmap.EpicSummary]:
    return [
        roadmap.EpicSummary(n, f"Epic {n}", "2026-06-01", None, (), 2, 2, f"u{n}") for n in numbers
    ]


def _children(epic: epics.IssueRef) -> list[epics.ChildState]:
    """Epic N's two closed children, tool#N1 and tool#N2."""
    return [
        epics.ChildState(epics.IssueRef("org", "tool", epic.number * 10 + i), "CLOSED")
        for i in (1, 2)
    ]


def test_epic_drift_outsider_close_is_per_item_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A child closed by an outsider is not closed: its epic is a stop on that
    # epic alone, and the other all-closed epic is still drift.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    stops = trust.BatchStops()
    items = trust_stubs.FakeItems(closers={("org/tool", 42): trust_stubs.OUTSIDER})
    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=_all_closed(4, 5)),
        patch("vergil_tooling.lib.epics.child_states", side_effect=_children),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
    ):
        result = _epic_drift("org", stops)
    assert [e.number for e in result] == [5]
    assert stops.stopped == (
        trust.Skipped(
            "org/tool", 42, "event", "drive-by", trust_stubs.EDITED, "close", "untrusted"
        ),
    )
    assert stops.exit_code() == 1
    stops.print_summary(sys.stderr)
    assert capsys.readouterr().err == (
        f"stopped: org/tool#42 event drive-by at {trust_stubs.EDITED} on close (untrusted)\n"
    )


def test_epic_drift_stops_are_counts_under_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    stops = trust.BatchStops()
    items = trust_stubs.FakeItems(closers={("org/tool", 41): trust_stubs.OUTSIDER})
    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=_all_closed(4)),
        patch("vergil_tooling.lib.epics.child_states", side_effect=_children),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
    ):
        assert _epic_drift("org", stops) == []
    stops.print_summary(sys.stderr)
    assert capsys.readouterr().err == "stopped 1 item(s) (untrusted: 1)\n"


def test_epic_drift_unreadable_close_is_per_item_stop() -> None:
    def events(query: str, **variables: Any) -> dict[str, Any]:
        if int(variables["number"]) == 41:
            return {"repository": None}
        return trust_stubs.closed_timeline()

    stops = trust.BatchStops()
    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=_all_closed(4, 5)),
        patch("vergil_tooling.lib.epics.child_states", side_effect=_children),
        patch("vergil_tooling.lib.github.graphql", side_effect=events),
    ):
        assert [e.number for e in _epic_drift("org", stops)] == [5]
    assert stops.stopped == (
        trust.Skipped("org/tool", 41, "event", None, "", "close", trust.READ_FAILURE),
    )


def test_close_drift_ignores_outsider_closed_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # A child's close counts only when close_judgement gives a role: the epic
    # whose child an outsider closed is never closed by the drift close.
    items = trust_stubs.FakeItems(closers={("org/tool", 51): trust_stubs.OUTSIDER})
    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=_all_closed(4, 5)),
        patch("vergil_tooling.lib.epics.child_states", side_effect=_children),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
    ):
        drift = _epic_drift("org")
    graph = FakeSubIssueGraph().install(monkeypatch)
    for number in (4, 5):
        graph.add_issue(epics.IssueRef("org", ".github", number), title="E", labels=["epic"])
    closed = epic_audit.close_drift([], drift, org="org")
    assert closed == ["org/.github#4"]
    assert graph.issues[epics.IssueRef("org", ".github", 4)].state == "CLOSED"
    assert graph.issues[epics.IssueRef("org", ".github", 5)].state == "OPEN"


def _reopened_timeline(actor: Any) -> dict[str, Any]:
    timeline = trust_stubs.closed_timeline()
    nodes = timeline["repository"]["issueOrPullRequest"]["timelineItems"]["nodes"]
    nodes.append({"__typename": "ReopenedEvent", "actor": actor, "createdAt": trust_stubs.EDITED})
    return timeline


def test_closed_epic_open_child_untrusted_reopen_is_per_item_stop() -> None:
    # A child an outsider reopened is not open: its closed epic is a stop, never
    # a violation, so the sweep never reopens it; a trusted reopen still counts.
    listing = [_record(19), _record(20)]
    kids = {19: epics.IssueRef("org", "lab", 462), 20: epics.IssueRef("org", "lab", 463)}

    def events(query: str, **variables: Any) -> dict[str, Any]:
        actor = trust_stubs.OUTSIDER if int(variables["number"]) == 462 else trust_stubs.AGENT
        return _reopened_timeline(actor)

    stops = trust.BatchStops()
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=listing),
        patch(
            "vergil_tooling.lib.epics.child_states",
            side_effect=lambda epic: [epics.ChildState(kids[epic.number], "OPEN")],
        ),
        patch("vergil_tooling.lib.github.graphql", side_effect=events),
    ):
        result = epic_audit.closed_epic_open_child("org", stops=stops, authors=trust_stubs.AUTHORS)
    assert result == [(epics.IssueRef("org", ".github", 20), (kids[20],))]
    assert stops.stopped == (
        trust.Skipped(
            "org/lab", 462, "event", "drive-by", trust_stubs.EDITED, "reopen", "untrusted"
        ),
    )


_ORGS_AUTHORS = TrustedAuthors(
    human=("human",),
    agents=("vergil-user",),
    orgs={
        "vergil-project": OrgConfig("vergil-project-automation"),
        "logical-minds-foundry": OrgConfig("logical-minds-foundry-automation"),
    },
)


def test_proxy_primary_closed_by_its_own_automation_is_trusted_cross_org() -> None:
    # The primary's close is judged in the primary's org: its own automation
    # App is trusted there, read from the proxy's org with the delivered copy.
    automation = {"__typename": "Bot", "login": "vergil-project-automation"}
    stops = trust.BatchStops()
    closures, errors = _proxy_closures(
        primary_state="CLOSED",
        children=[],
        closer=automation,
        stops=stops,
        authors=_ORGS_AUTHORS,
    )
    assert (closures, errors) == ([epic_audit.ProxyClosure(_PROXY, _PRIMARY, 0)], [])
    assert stops.stopped == ()


@pytest.mark.parametrize(
    ("closer", "login"),
    [
        (trust_stubs.OUTSIDER, "drive-by"),
        # The proxy org's automation holds no role in the primary's org.
        (
            {"__typename": "Bot", "login": "logical-minds-foundry-automation"},
            "logical-minds-foundry-automation",
        ),
    ],
    ids=["outsider", "other-orgs-automation"],
)
def test_proxy_closures_untrusted_primary_close_is_per_item_stop(closer: Any, login: str) -> None:
    stops = trust.BatchStops()
    closures, errors = _proxy_closures(
        primary_state="CLOSED", children=[], closer=closer, stops=stops, authors=_ORGS_AUTHORS
    )
    assert (closures, errors) == ([], [])
    assert stops.stopped == (
        trust.Skipped(
            "vergil-project/.github", 1417, "event", login, trust_stubs.EDITED, "close", "untrusted"
        ),
    )


def _untrusted_proxy_body(_ref: epics.IssueRef) -> epics.IssueRef:
    item = trust.Skipped(
        _PROXY.slug.split("#")[0], 320, "issue", "drive-by", "t", "body", "untrusted"
    )
    raise trust.UntrustedContentError(item, "the issue body is untrusted")


def test_proxy_closures_untrusted_proxy_body_is_per_item_stop() -> None:
    # A Primary: line counts only from a trusted body: the proxy is a stop,
    # never closed and never a link error.
    stops = trust.BatchStops()
    closures, errors = _proxy_closures(
        primary_state="CLOSED", children=[], primary_of=_untrusted_proxy_body, stops=stops
    )
    assert (closures, errors) == ([], [])
    assert [(s.repo, s.number, s.cause) for s in stops.stopped] == [
        ("logical-minds-foundry/.github", 320, "untrusted")
    ]


def test_proxy_closures_unreadable_primary_close_is_per_item_stop() -> None:
    stops = trust.BatchStops()
    with (
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
        patch("vergil_tooling.lib.epics.issue_state", return_value="CLOSED"),
        patch("vergil_tooling.lib.github.graphql", return_value={"repository": None}),
    ):
        closures, errors = epic_audit.proxy_closures(
            "logical-minds-foundry/.github",
            stops=stops,
            authors=trust_stubs.AUTHORS,
            listing=cast(
                "list[epic_docs.EpicRecord]", [_epic_record(320, "OPEN", ["epic", "proxy"])]
            ),
        )
    assert (closures, errors) == ([], [])
    assert stops.stopped == (
        trust.Skipped(
            "logical-minds-foundry/.github", 320, "issue", None, "", "primary", trust.READ_FAILURE
        ),
    )


def test_proxy_link_drift_untrusted_proxy_body_is_per_item_stop() -> None:
    home = "logical-minds-foundry/.github"
    listing = [
        _epic_record(320, "OPEN", ["epic", "proxy"]),
        _epic_record(322, "OPEN", ["epic", "proxy"]),
    ]

    def primary_of(ref: epics.IssueRef) -> epics.IssueRef:
        if ref.number == 320:
            return _untrusted_proxy_body(ref)
        raise trust.TrustReadError("read_issue x", "truncated")

    stops = trust.BatchStops()
    with patch(f"{_PX}.primary_of", side_effect=primary_of):
        problems = epic_audit.proxy_link_drift(
            home, stops=stops, listing=cast("list[epic_docs.EpicRecord]", listing)
        )
    assert problems == []
    assert [(s.number, s.cause) for s in stops.stopped] == [
        (320, "untrusted"),
        (322, trust.READ_FAILURE),
    ]


def test_epic_drift_child_open_again_is_no_drift_and_no_stop() -> None:
    # The roadmap counted every child closed, but one is open again when the
    # closes are judged: the epic is not drift, and nothing is stopped.
    stops = trust.BatchStops()
    children = [
        epics.ChildState(epics.IssueRef("org", "tool", 41), "OPEN"),
        epics.ChildState(epics.IssueRef("org", "tool", 42), "CLOSED"),
    ]
    with (
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=_all_closed(4)),
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
        patch("vergil_tooling.lib.github.graphql", side_effect=AssertionError("no read")),
    ):
        assert _epic_drift("org", stops) == []
    assert stops.stopped == ()


def test_closed_epic_open_child_unreadable_events_is_per_item_stop() -> None:
    stops = trust.BatchStops()
    kid = epics.IssueRef("org", "lab", 462)
    with (
        patch("vergil_tooling.lib.epic_docs.list_epics", return_value=[_record(19)]),
        patch(
            "vergil_tooling.lib.epics.child_states", return_value=[epics.ChildState(kid, "OPEN")]
        ),
        patch("vergil_tooling.lib.github.graphql", return_value={"repository": None}),
    ):
        result = epic_audit.closed_epic_open_child("org", stops=stops, authors=trust_stubs.AUTHORS)
    assert result == []
    assert stops.stopped == (
        trust.Skipped("org/lab", 462, "event", None, "", "reopen", trust.READ_FAILURE),
    )


# -- Chained epics (epic vergil-project/.github#1609, Task 9a) -------------------

_HOME = "org/.github"
_HEAD = epics.IssueRef("org", ".github", 40)
_C2 = epics.IssueRef("org", ".github", 50)
_C3 = epics.IssueRef("org", ".github", 60)


def _home_listing(graph: FakeSubIssueGraph) -> list[epic_docs.EpicRecord]:
    """The home's epics as :func:`epic_audit.list_home_epics` lists them, from *graph*."""
    return [
        epic_docs.EpicRecord(
            number=ref.number,
            title=issue.title,
            state=issue.state,
            stateReason=None,
            createdAt="2026-11-01T00:00:00Z",
            labels=list(issue.labels),
            body=issue.body,
        )
        for ref, issue in graph.issues.items()
        if (ref.owner, ref.repo) == ("org", ".github") and "epic" in issue.labels
    ]


def _chain_graph(
    monkeypatch: pytest.MonkeyPatch, *, head_labels: tuple[str, ...] = ("epic",)
) -> FakeSubIssueGraph:
    """A head (#40) with one item, and a continuation (#50) under it holding another."""
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=head_labels)
    graph.link(_HEAD, graph.add_issue(epics.IssueRef("org", "tool", 1), title="first"))
    graph.add_continuation(_C2, _HEAD, _HEAD)
    graph.link(_C2, graph.add_issue(epics.IssueRef("org", "tool", 2), title="second"))
    return graph


def test_continuation_not_reported_missing_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    # A continuation has no Documents section of its own: its head carries it.
    graph = _chain_graph(monkeypatch, head_labels=("epic", "ad-hoc"))
    findings = epic_audit.epic_documents("org", home=_HOME, listing=_home_listing(graph))
    assert findings == []


def test_continuation_not_a_closed_epic_with_open_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # A continuation closed by hand in an open chain still holds an open item:
    # it is no closed epic, and its open item is not reported against it.
    graph = _chain_graph(monkeypatch)
    graph.close(_C2)
    stops = trust.BatchStops()
    found = epic_audit.closed_epic_open_child(
        "org", stops=stops, authors=trust_stubs.AUTHORS, home=_HOME, listing=_home_listing(graph)
    )
    assert found == []
    assert stops.stopped == ()


def test_closed_epic_open_child_judges_a_closed_head_across_its_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A closed head whose only open item sits in its continuation is reported.
    graph = _chain_graph(monkeypatch)
    graph.close(_HEAD)
    graph.close(epics.IssueRef("org", "tool", 1))
    found = epic_audit.closed_epic_open_child(
        "org",
        stops=trust.BatchStops(),
        authors=trust_stubs.AUTHORS,
        home=_HOME,
        listing=_home_listing(graph),
    )
    assert found == [(_HEAD, (epics.IssueRef("org", "tool", 2),))]


def test_closed_epic_open_child_skips_a_drifted_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # A closed head whose chain breaks a rule cannot be read past the drift;
    # chain_drift reports it, so this check leaves it out instead of failing.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"], state="CLOSED")
    graph.add_issue(
        _C2,
        title="Epic: big (continuation 2)",
        labels=["epic", epics.CONTINUATION_LABEL],
        body="Continues: org/.github#999\nContainer: 2\n",
    )
    graph.link(_HEAD, _C2)
    found = epic_audit.closed_epic_open_child(
        "org",
        stops=trust.BatchStops(),
        authors=trust_stubs.AUTHORS,
        home=_HOME,
        listing=_home_listing(graph),
    )
    assert found == []
    assert [d.kind for d in epic_audit.chain_drift(_HOME, listing=_home_listing(graph))] == [
        "drift"
    ]


def test_continuation_not_a_primary_in_proxy_drift() -> None:
    # A continuation carries no kind label; one that carries `proxy` by
    # mistake is still a container, never a proxy whose link is checked.
    listing = [
        _record(320, labels=("epic", "proxy", epics.CONTINUATION_LABEL), state="OPEN"),
    ]
    stops = trust.BatchStops()
    with (
        patch.object(proxies, "primary_of", side_effect=AssertionError("no read")),
        patch.object(proxies, "proxies_of", side_effect=AssertionError("no read")),
    ):
        assert epic_audit.proxy_link_drift(_HOME, stops=stops, listing=listing) == []
        closures, errors = epic_audit.proxy_closures(
            _HOME, stops=stops, authors=trust_stubs.AUTHORS, listing=listing
        )
    assert (closures, errors) == ([], [])
    assert stops.stopped == ()


def _org_issues(monkeypatch: pytest.MonkeyPatch, *repos: str) -> list[epic_audit.OpenIssue]:
    """:func:`epic_audit.open_issues` of ``org`` over *repos*, every one managed."""
    monkeypatch.setattr(github, "list_org_repos", lambda _org: list(repos))
    monkeypatch.setattr(epics, "is_managed_repo", lambda _repo: True)
    return epic_audit.open_issues("org")


def test_misplaced_adhoc_sees_task_in_adhoc_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    # The task's raw parent is the ad-hoc epic's continuation; it maps to the
    # ad-hoc head, so the task is judged as under the live ad-hoc epic.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic (ad hoc): tool", labels=["epic", "ad-hoc"])
    graph.add_continuation(_C2, _HEAD, _HEAD)
    task = graph.add_issue(epics.IssueRef("org", "tool", 7), title="Logbook: 2026-11-25")
    graph.link(_C2, task)
    issues = _org_issues(monkeypatch, ".github", "tool")
    with (
        patch(f"{_AUDIT}.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        patch(f"{_AUDIT}._standalone_confirmed", return_value=False),
    ):
        found = epic_audit.misplaced_adhoc("org", issues=issues)
    assert [m.task for m in found] == [task]
    assert "hoshin" in found[0].reason


def test_epic_children_spans_chain_and_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _chain_graph(monkeypatch)
    proxy = graph.add_issue(
        epics.IssueRef("other", ".github", 9), title="Epic (proxy): big", labels=["epic", "proxy"]
    )
    graph.link(proxy, graph.add_issue(epics.IssueRef("other", "lib", 3), title="third"))
    with (
        patch.object(proxies, "proxies_of", return_value=(proxy,)),
        patch.object(proxies, "primary_of", return_value=_HEAD),
    ):
        children = epic_audit.epic_children(_HEAD)
    assert [c.ref.slug for c in children] == ["org/tool#1", "org/tool#2", "other/lib#3"]


def _drift_fixture(graph: FakeSubIssueGraph, kind: str) -> list[epic_audit.ChainDrift]:
    """Build the chain behind one :class:`ChainDrift` *kind* in *graph*; return what it reports."""
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    if kind == "drift":
        graph.add_issue(
            _C2,
            title="Epic: big (continuation 2)",
            labels=["epic", epics.CONTINUATION_LABEL],
            body="Continues: org/.github#999\nContainer: 2\n",
        )
        graph.link(_HEAD, _C2)
        return [epic_audit.ChainDrift(_HEAD, "drift", (_C2,), "continues-mismatch")]
    if kind == "full-tail":
        for n in range(1, epic_chain.SUBISSUE_LIMIT + 1):
            graph.link(_HEAD, graph.add_issue(epics.IssueRef("org", "tool", n), title=f"t{n}"))
        return [epic_audit.ChainDrift(_HEAD, "full-tail", (_HEAD,))]
    if kind == "duplicate":
        graph.add_continuation(_C2, _HEAD, _HEAD)
        graph.add_continuation(_C3, _HEAD, _HEAD)
        return [epic_audit.ChainDrift(_HEAD, "duplicate", (_HEAD,), "two-continuations")]
    if kind == "closed-head-open-continuation":
        graph.add_continuation(_C2, _HEAD, _HEAD)
        graph.close(_HEAD)
        return [epic_audit.ChainDrift(_HEAD, "closed-head-open-continuation", (_C2,))]
    if kind == "orphan":
        graph.add_continuation(_C2, _HEAD, None)
        return [epic_audit.ChainDrift(_HEAD, "orphan", (_C2,))]
    assert kind == "closed-continuation"
    graph.add_continuation(_C2, _HEAD, _HEAD)
    graph.close(_C2)
    return [epic_audit.ChainDrift(_HEAD, "closed-continuation", (_C2,))]


@pytest.mark.parametrize("kind", epic_audit.CHAIN_DRIFT_KINDS)
def test_audit_reports_each_drift_kind(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    expected = _drift_fixture(graph, kind)
    graph.clear_log()
    found = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    assert found == expected
    assert graph.events == []  # report only
    report = epic_audit.render([], [], org="org", window_days=30, chain_drift=found)
    section = "## Chain notes" if kind == "closed-continuation" else "## Chain drift"
    assert section in report
    assert f"- {epic_audit.chain_drift_line(found[0])}" in report
    assert "_No drift" not in report


def test_audit_full_head_is_no_drift_while_rollover_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # While rollover is off a head at 100 is today's limit, refused by name
    # at link time: no continuation can be made, so nothing is left to heal.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", False)
    graph = FakeSubIssueGraph().install(monkeypatch)
    _drift_fixture(graph, "full-tail")
    assert epic_audit.chain_drift(_HOME, listing=_home_listing(graph)) == []


def test_audit_whole_chain_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _chain_graph(monkeypatch)
    assert epic_audit.chain_drift(_HOME, listing=_home_listing(graph)) == []


def test_audit_notes_closed_continuation_in_open_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not drift: a note, naming it, and the chain is still walked past it.
    graph = _chain_graph(monkeypatch)
    graph.add_continuation(_C3, _HEAD, _C2, k=3)
    graph.close(_C2)
    found = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    assert found == [epic_audit.ChainDrift(_HEAD, "closed-continuation", (_C2,))]
    report = epic_audit.render([], [], org="org", window_days=30, chain_drift=found)
    assert "## Chain drift" not in report
    assert (
        "- epic org/.github#40: its continuation org/.github#50 is closed; its items are"
        " still read, and nothing is linked into it"
    ) in report


def test_audit_lists_orphan_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    # An orphan of an open head, and one of a closed head, are each listed,
    # naming the head; nothing is adopted.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    graph.add_continuation(_C2, _HEAD, None)
    closed = graph.add_issue(
        epics.IssueRef("org", ".github", 41), title="Epic: done", labels=["epic"], state="CLOSED"
    )
    orphan = graph.add_continuation(_C3, closed, None)
    graph.clear_log()
    found = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    assert found == [
        epic_audit.ChainDrift(_HEAD, "orphan", (_C2,)),
        epic_audit.ChainDrift(closed, "orphan", (orphan,)),
    ]
    assert graph.events == []
    assert epic_audit.chain_drift_line(found[0]) == (
        "epic org/.github#40: org/.github#50 is an orphaned continuation, open and not"
        " reached from the head"
    )


def test_audit_unresolvable_continuation_is_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unlinked continuation whose body is not trusted names no head the
    # audit may believe: it is drift on its own, never guessed past.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    graph.add_continuation(_C2, _HEAD, None, author=trust_stubs.OUTSIDER)
    found = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    assert found == [epic_audit.ChainDrift(_C2, "drift", (_C2,), "untrusted-body")]
    assert epic_audit.chain_drift_line(found[0]) == (
        "org/.github#50 breaks the untrusted-body rule"
    )


def test_audit_continuation_of_drifted_chain_is_reported_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # C3 sits past a container that holds two continuations: the walk stops
    # at the drift, and C3, which the walk never reached, is no orphan.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    graph.add_continuation(_C2, _HEAD, _HEAD)
    graph.add_continuation(epics.IssueRef("org", ".github", 55), _HEAD, _HEAD)
    graph.add_continuation(_C3, _HEAD, _C2, k=3)
    found = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    assert found == [epic_audit.ChainDrift(_HEAD, "duplicate", (_HEAD,), "two-continuations")]


def test_orphan_detection_still_sees_native_parents(monkeypatch: pytest.MonkeyPatch) -> None:
    # An open issue with a native parent, in a continuation or not, is no
    # orphan; one with none is.
    graph = _chain_graph(monkeypatch)
    lone = graph.add_issue(epics.IssueRef("org", "tool", 3), title="lone")
    issues = _org_issues(monkeypatch, ".github", "tool")
    monkeypatch.setattr(epics, "queue_home", lambda _repo: (_HOME, "org"))
    with patch.object(release, "is_release_tracking_issue", return_value=False):
        orphans = epic_audit.orphan_drift("org", parents=epic_audit.BodyParents(), issues=issues)
    assert [o.ref for o in orphans] == [lone]
    parents = {i.ref.slug: i.native_parent for i in issues}
    assert parents["org/tool#1"] == _HEAD
    assert parents["org/tool#2"] == _HEAD


def test_misplaced_archives_still_sees_native_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    # An archive linked in the intake queue's continuation is parented under
    # the intake queue epic, named by its head.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic (intake): org", labels=["epic", "intake"])
    graph.add_continuation(_C2, _HEAD, _HEAD)
    archive = graph.add_issue(
        epics.IssueRef("org", ".github", 70),
        title=epics.archive_title(epics.INTAKE, "org", "2026-Q3"),
        labels=["archive", "intake"],
    )
    graph.link(_C2, archive)
    issues = _org_issues(monkeypatch, ".github")
    found = epic_audit.misplaced_archives("org", issues=issues)
    assert found == [
        epic_audit.MisplacedArchive(archive, "parented under the intake queue epic org/.github#40")
    ]


def test_chain_drift_lists_the_home_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _chain_graph(monkeypatch)
    graph.close(_C2)
    with patch.object(epic_audit, "list_home_epics", return_value=_home_listing(graph)) as listing:
        found = epic_audit.chain_drift(_HOME)
    listing.assert_called_once_with(_HOME)
    assert [d.kind for d in found] == ["closed-continuation"]


def test_audit_orphan_naming_no_epic_of_the_home_is_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unlinked continuation whose trusted Continues: line names an issue
    # that is no epic of the home is never walked from: it is drift.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="not an epic")
    graph.add_continuation(_C2, _HEAD, None)
    graph.clear_log()
    found = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    assert found == [epic_audit.ChainDrift(_C2, "drift", (_C2,), "continues-mismatch")]
    assert graph.events == []


def test_chain_drift_line_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown chain drift kind 'other'"):
        epic_audit.chain_drift_line(epic_audit.ChainDrift(_HEAD, "other", (_HEAD,)))


def test_audit_unlinked_continuation_without_epic_label_is_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unlinked issue labelled `continuation` that lost its `epic` label is
    # in no epic listing and no walk reaches it: it is still found, and
    # reported as drift on the label rule.
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    graph.add_continuation(_C2, _HEAD, None)
    graph.issues[_C2].labels = [epics.CONTINUATION_LABEL]
    graph.clear_log()
    found = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    assert found == [epic_audit.ChainDrift(_C2, "drift", (_C2,), "label")]
    assert graph.events == []


# -- The sweep's chain heals (epic vergil-project/.github#1609, Task 9b) ---------


def _heal(graph: FakeSubIssueGraph) -> epic_audit.ChainHeals:
    """heal_chains of the home's chain drift, as *graph* holds it, with the log cleared first."""
    drift = epic_audit.chain_drift(_HOME, listing=_home_listing(graph))
    graph.clear_log()
    return epic_audit.heal_chains(drift)


def test_sweep_heals_full_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    _drift_fixture(graph, "full-tail")
    heals = _heal(graph)
    assert heals == epic_audit.ChainHeals(
        healed=("healed the full tail org/.github#40 of epic org/.github#40",)
    )
    chain = epic_chain.containers(_HEAD)
    assert [c.item_count for c in chain] == [epic_chain.ITEM_CAP, 1]
    assert epic_audit.chain_drift(_HOME, listing=_home_listing(graph)) == []


def test_sweep_heals_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    _drift_fixture(graph, "duplicate")
    heals = _heal(graph)
    assert heals == epic_audit.ChainHeals(
        healed=("healed the duplicate continuations of epic org/.github#40 under org/.github#40",)
    )
    assert graph.children_of(_HEAD) == [_C2]
    assert graph.issues[_C3].state == "CLOSED"
    assert epic_audit.chain_drift(_HOME, listing=_home_listing(graph)) == []


def test_sweep_closes_open_continuations_of_closed_head(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeSubIssueGraph().install(monkeypatch)
    _drift_fixture(graph, "closed-head-open-continuation")
    heals = _heal(graph)
    assert heals == epic_audit.ChainHeals(
        healed=("closed the open continuation(s) org/.github#50 of the closed epic org/.github#40",)
    )
    assert graph.events == [("close", _C2, "completed")]
    assert epic_audit.chain_drift(_HOME, listing=_home_listing(graph)) == []


def test_sweep_adopts_orphan_and_reruns_rollup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The orphan held the chain's last open item, now closed: that close fired
    # no rollup the head could see, so once the orphan is adopted the rollup
    # runs again and closes the whole chain.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    graph.link(
        _HEAD, graph.add_issue(epics.IssueRef("org", "tool", 1), title="first", state="CLOSED")
    )
    graph.add_continuation(_C2, _HEAD, None)
    graph.link(_C2, graph.add_issue(epics.IssueRef("org", "tool", 2), title="last", state="CLOSED"))
    heals = _heal(graph)
    assert heals == epic_audit.ChainHeals(
        healed=("adopted the orphaned continuation org/.github#50 into epic org/.github#40",)
    )
    assert graph.parent_of(_C2) == _HEAD
    assert graph.issues[_HEAD].state == "CLOSED"
    assert graph.issues[_C2].state == "CLOSED"
    assert "Rolling up epic org/.github#40" in capsys.readouterr().out


@pytest.mark.parametrize("first_state", ["OPEN", "CLOSED"])
def test_sweep_adopted_orphan_with_open_item_holds_the_epic_open(
    monkeypatch: pytest.MonkeyPatch, first_state: str
) -> None:
    # With no closed item there is nothing to roll up; with one, the rollup
    # runs and finds the orphan's open item.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    graph.link(
        _HEAD, graph.add_issue(epics.IssueRef("org", "tool", 1), title="first", state=first_state)
    )
    graph.add_continuation(_C2, _HEAD, None)
    graph.link(_C2, graph.add_issue(epics.IssueRef("org", "tool", 2), title="open"))
    heals = _heal(graph)
    assert heals.healed == (
        "adopted the orphaned continuation org/.github#50 into epic org/.github#40",
    )
    assert graph.parent_of(_C2) == _HEAD
    assert graph.issues[_HEAD].state == "OPEN"


def test_sweep_reports_orphan_of_closed_head_without_adopting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Closed is closed: the orphan is reported, naming it and its closed head,
    # nothing is linked, and the other heals still run.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: done", labels=["epic"], state="CLOSED")
    graph.add_continuation(_C2, _HEAD, None)
    live = graph.add_issue(
        epics.IssueRef("org", ".github", 41), title="Epic: live", labels=["epic"]
    )
    graph.add_continuation(_C3, live, live)
    graph.close(live)  # its open continuation is healed
    heals = _heal(graph)
    assert heals.unhealed == (
        "epic org/.github#40 is closed, but org/.github#50 is an orphaned continuation of it;"
        " not adopted, since closed epics are not written to",
    )
    assert heals.healed == (
        "closed the open continuation(s) org/.github#60 of the closed epic org/.github#41",
    )
    assert graph.parent_of(_C2) is None
    assert graph.issues[_C2].state == "OPEN"
    assert graph.events == [("close", _C3, "completed")]


@pytest.mark.parametrize("kind", ["duplicate", "orphan"])
def test_sweep_heals_no_continuation_while_rollover_is_off(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # While rollover is off nothing may merge or adopt a continuation: the
    # drift is reported unhealed, saying so, and nothing is written.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    drift = _drift_fixture(graph, kind)
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", False)
    graph.clear_log()
    heals = epic_audit.heal_chains(drift)
    assert heals.healed == ()
    assert len(heals.unhealed) == 1
    assert "rollover into a continuation is not enabled yet" in heals.unhealed[0]
    assert graph.events == []


def test_sweep_full_tail_is_unhealed_while_rollover_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = FakeSubIssueGraph().install(monkeypatch)
    drift = _drift_fixture(graph, "full-tail")
    graph.clear_log()
    heals = epic_audit.heal_chains(drift)
    assert heals.unhealed == (
        "epic org/.github#40: its tail org/.github#40 holds 100 sub-issues and no continuation;"
        " not healed: rollover into a continuation is not enabled yet",
    )
    assert graph.events == []


def test_sweep_reports_drift_and_notes_unhealed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A broken rule is never guessed past; a closed continuation is a note.
    graph = FakeSubIssueGraph().install(monkeypatch)
    drift = _drift_fixture(graph, "drift")
    note = epic_audit.ChainDrift(_HEAD, "closed-continuation", (_C3,))
    graph.clear_log()
    heals = epic_audit.heal_chains([*drift, note])
    assert heals == epic_audit.ChainHeals(
        unhealed=(epic_audit.chain_drift_line(drift[0]),),
        notes=(epic_audit.chain_drift_line(note),),
    )
    assert graph.events == []


def test_sweep_heal_failure_is_one_unhealed_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # A heal that fails is reported; the other heals still run.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    drift = _drift_fixture(graph, "closed-head-open-continuation")
    failing = epic_audit.ChainDrift(epics.IssueRef("org", ".github", 41), "full-tail", (_C3,))
    orphan = epic_audit.ChainDrift(epics.IssueRef("org", ".github", 42), "orphan", (_C3,))
    with (
        patch.object(epic_chain, "heal_full_tail", side_effect=RuntimeError("boom")),
        patch.object(epics, "issue_state", side_effect=RuntimeError("no state")),
    ):
        heals = epic_audit.heal_chains([failing, *drift, orphan])
    assert heals.unhealed == (
        f"{epic_audit.chain_drift_line(failing)}; not healed: boom",
        "epic org/.github#42: the orphaned continuation(s) org/.github#60 not adopted: no state",
    )
    assert heals.healed == (
        "closed the open continuation(s) org/.github#50 of the closed epic org/.github#40",
    )


def test_sweep_raced_heal_writes_no_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # A racing writer healed it first: nothing to do and nothing reported.
    monkeypatch.setattr(epic_chain, "ROLLOVER_ENABLED", True)
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Epic: big", labels=["epic"])
    drift = [
        epic_audit.ChainDrift(_HEAD, "full-tail", (_HEAD,)),
        epic_audit.ChainDrift(_HEAD, "duplicate", (_HEAD,), "two-continuations"),
        epic_audit.ChainDrift(_HEAD, "closed-head-open-continuation", (_C2,)),
    ]
    graph.close(_HEAD)
    assert epic_audit.heal_chains(drift) == epic_audit.ChainHeals()
    assert graph.events == []


def test_sweep_reports_chained_development_epics(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each open development or proxy epic with a continuation is a report
    # line; a standing epic's chain, an unchained epic, a closed one and a
    # drifted chain are not.
    graph = _chain_graph(monkeypatch)
    proxy = graph.add_issue(
        epics.IssueRef("org", ".github", 41), title="Epic (proxy): x", labels=["epic", "proxy"]
    )
    graph.add_continuation(epics.IssueRef("org", ".github", 51), proxy, proxy)
    intake = graph.add_issue(
        epics.IssueRef("org", ".github", 42), title="Epic (intake): org", labels=["epic", "intake"]
    )
    graph.add_continuation(epics.IssueRef("org", ".github", 52), intake, intake)
    graph.add_issue(epics.IssueRef("org", ".github", 43), title="Epic: small", labels=["epic"])
    done = graph.add_issue(
        epics.IssueRef("org", ".github", 44), title="Epic: done", labels=["epic"], state="CLOSED"
    )
    graph.add_continuation(epics.IssueRef("org", ".github", 54), done, done)
    drifted = graph.add_issue(
        epics.IssueRef("org", ".github", 45), title="Epic: drifted", labels=["epic"]
    )
    graph.add_continuation(
        epics.IssueRef("org", ".github", 55), drifted, drifted, author=trust_stubs.OUTSIDER
    )
    graph.clear_log()
    lines = epic_audit.chained_epics(_HOME, listing=_home_listing(graph))
    assert lines == [
        "epic org/.github#40 spans 2 containers (2 items); consider splitting it",
        "epic org/.github#41 spans 2 containers (0 items); consider splitting it",
    ]
    assert graph.events == []


def test_chained_epics_lists_the_home_when_not_given(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _chain_graph(monkeypatch)
    with patch.object(epic_audit, "list_home_epics", return_value=_home_listing(graph)) as listing:
        lines = epic_audit.chained_epics(_HOME)
    listing.assert_called_once_with(_HOME)
    assert len(lines) == 1


def test_render_closed_lists_chain_heals_and_chained_epics() -> None:
    heals = epic_audit.ChainHeals(healed=("healed A",), unhealed=("broken B",), notes=("note C",))
    out = epic_audit.render_closed(
        [], org="org", window_days=30, chain_heals=heals, chained=["epic D spans 2"]
    )
    assert "## Chains healed\n\n- healed A\n" in out
    assert "## Chain notes\n\n- note C\n" in out
    assert "## Chained epics (report only)\n\n- epic D spans 2\n" in out
    assert "## Needs a human (report only)" in out
    assert (
        "**Chain drift the sweep could not heal** — the run fails; settle each:\n- broken B" in out
    )
    assert "_No drift" not in out


def test_render_closed_chained_epics_alone_is_not_no_drift() -> None:
    out = epic_audit.render_closed([], org="org", window_days=30, chained=["epic D spans 2"])
    assert "_No drift" not in out
    assert "## Needs a human" not in out


def test_heal_chains_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="chain drift kind 'other' has no heal"):
        epic_audit.heal_chains([epic_audit.ChainDrift(_HEAD, "other", (_HEAD,))])


def test_attach_orphans_other_chain_refusal_is_that_orphan_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A refusal that does not shut the epic (here its depth limit) refuses
    # that orphan alone: the next orphan of the same home is still linked.
    graph, _ = _intake_graph(monkeypatch)
    orphans = _orphans_in(graph, "org/tool", (5, 6))
    real = epic_chain.link_into

    def link_into(head: epics.IssueRef, task: epics.IssueRef) -> epics.IssueRef:
        if task.number == 5:
            raise epic_chain.ChainDepthError(head, items=1, containers=8)
        return real(head, task)

    with patch.object(epic_chain, "link_into", side_effect=link_into):
        result = _attach(orphans)
    assert result.linked == 1
    assert result.refused == (
        "org/tool#5 not linked: epic org/.github#100 cannot grow: 1 item in 8 containers, at"
        " GitHub's nesting limit of 8; split the epic",
    )
    assert graph.mutations == [("add", _ORG_INTAKE, epics.IssueRef("org", "tool", 6))]
