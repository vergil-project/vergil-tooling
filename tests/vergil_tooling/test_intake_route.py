"""Tests for vergil_tooling.lib.intake_route (deterministic intake routing)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, call, patch

import pytest

from vergil_tooling.lib import epics, github, intake_route, security_records
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.intake_route import (
    Defer,
    Drop,
    OneOff,
    RouteRefusedError,
    RouteResult,
    Split,
    ToIntake,
    ToPlanning,
    route,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.intake_route"
# Captured before any patching, to run the real planning ensure under Env.
_REAL_ENSURE_PLANNING_EPIC = epics.ensure_planning_epic

ITEM = IssueRef("org", ".github", 352)
INTAKE = IssueRef("org", ".github", 7)
PLANNING = IssueRef("org", ".github", 8)
ADHOC_DOTGITHUB = IssueRef("org", ".github", 9)
DEFERRED = IssueRef("org", ".github", 10)
TOOL_ADHOC = IssueRef("org", ".github", 11)
DEV_EPIC = IssueRef("org", ".github", 400)
TODAY = date(2026, 10, 23)
ORIG_URL = "https://github.com/org/.github/issues/352"
GHSA = "GHSA-abcd-2345-wxyz"
ADVISORY_LINE = f"Security advisory: org/.github {GHSA}"


class Env:
    """The patched GitHub and epics boundary one test drives."""

    def __init__(self) -> None:
        self.parent: IssueRef | None = INTAKE
        # The container_kind map; queue_kind is derived from it.
        self.kinds: dict[IssueRef, str | None] = {
            INTAKE: "intake",
            PLANNING: "planning",
            DEFERRED: "deferred",
            ADHOC_DOTGITHUB: "ad-hoc",
            TOOL_ADHOC: "ad-hoc",
        }
        self.labels: list[str] = []
        self.comments: list[str] = []
        # Closing-PR URL -> state (OPEN, CLOSED, MERGED).
        self.closing_prs: dict[str, str] = {}
        self.public: dict[str, bool] = {}
        self.native_parent: dict[IssueRef, IssueRef | None] = {}
        self.epic_state = "open"
        self.transfer: Any = "https://github.com/org/tool/issues/900"
        self.issue_view: dict[str, object] = {
            "title": "Make the thing faster",
            "body": "It is slow.",
            "labels": [{"name": "triage"}, {"name": "bug"}, {"name": "off-registry"}],
            "url": ORIG_URL,
        }
        self.created_url = "https://github.com/org/tool/issues/901"
        self.run = MagicMock()
        self.read_output = MagicMock(side_effect=self._read_output)
        self.read_json = MagicMock(side_effect=self._read_json)
        self.create_issue = MagicMock(side_effect=lambda **kw: self.created_url)
        self.ensure_labels = MagicMock()
        # A link takes effect: the task's native parent becomes the epic.
        self.add_child = MagicMock(side_effect=self.attach)
        self.reparent_child = MagicMock(side_effect=self.attach)
        self.sleep = MagicMock()
        self.ensure_planning_epic = MagicMock(return_value=PLANNING)
        self.ensure_intake_epic = MagicMock(return_value=INTAKE)
        self.ensure_deferred_epic = MagicMock(return_value=DEFERRED)
        self.resolve_epic_ref = MagicMock(return_value=TOOL_ADHOC)
        self.advisory_home = MagicMock(side_effect=lambda org: f"{org}/.github")
        self.retarget = MagicMock()

    def make_security_item(self) -> None:
        """Turn the item into a security item: the label and the advisory line."""
        self.issue_view = {
            **self.issue_view,
            "body": f"It is slow.\n\n{ADVISORY_LINE}",
            "labels": [{"name": "bug"}, {"name": "security"}],
        }

    def attach(self, epic: IssueRef, task: IssueRef) -> None:
        self.native_parent[task] = epic

    def queue_kind(self, ref: IssueRef) -> str | None:
        kind = self.kinds.get(ref)
        return kind if kind in ("intake", "planning", "deferred") else None

    def _read_json(self, *args: str) -> object:
        fields = args[-1]
        if fields == "labels,comments":
            return {
                "labels": [{"name": name} for name in self.labels],
                "comments": [{"body": body} for body in self.comments],
            }
        if fields == "closedByPullRequestsReferences":
            return {
                "closedByPullRequestsReferences": [
                    {"number": int(url.rsplit("/", 1)[1]), "url": url} for url in self.closing_prs
                ]
            }
        return self.issue_view

    def _read_output(self, *args: str) -> str:
        if args[:2] == ("issue", "transfer"):
            if isinstance(self.transfer, BaseException):
                raise self.transfer
            return str(self.transfer)
        if args[:2] == ("pr", "view"):
            return self.closing_prs[args[2]]
        assert args[0] == "api"
        assert args[-1] == ".state"
        return self.epic_state


@pytest.fixture
def env() -> Iterator[Env]:
    state = Env()
    with (
        patch(f"{_MOD}.epics.parent_of", side_effect=lambda ref: state.parent),
        patch(f"{_MOD}.epics.queue_kind", side_effect=state.queue_kind),
        patch(f"{_MOD}.epics.container_kind", side_effect=lambda ref: state.kinds.get(ref)),
        patch(
            f"{_MOD}.epics.native_parent_of",
            side_effect=lambda ref: state.native_parent.get(ref),
        ),
        patch(f"{_MOD}.epics.add_child", state.add_child),
        patch(f"{_MOD}.epics.reparent_child", state.reparent_child),
        patch(f"{_MOD}.epics.ensure_planning_epic", state.ensure_planning_epic),
        patch(f"{_MOD}.epics.ensure_intake_epic", state.ensure_intake_epic),
        patch(f"{_MOD}.epics.ensure_deferred_epic", state.ensure_deferred_epic),
        patch(f"{_MOD}.epics.resolve_epic_ref", state.resolve_epic_ref),
        patch(f"{_MOD}.github.is_public", side_effect=lambda r: state.public.get(r, True)),
        patch(f"{_MOD}.github.run", state.run),
        patch(f"{_MOD}.github.read_output", state.read_output),
        patch(f"{_MOD}.github.read_json", state.read_json),
        patch(f"{_MOD}.github.create_issue", state.create_issue),
        patch(f"{_MOD}.ensure_labels", state.ensure_labels),
        patch(f"{_MOD}.time.sleep", state.sleep),
        patch(f"{_MOD}.security_records.advisory_home", state.advisory_home),
        patch(f"{_MOD}.security_records.retarget", state.retarget),
    ):
        yield state


def _transfer_calls(env: Env) -> list[Any]:
    return [c for c in env.read_output.call_args_list if c.args[:2] == ("issue", "transfer")]


# -- drop / defer / split ----------------------------------------------------


def test_route_drop_comments_and_closes(env: Env) -> None:
    result = route(ITEM, Drop(reason="not feasible for years"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "not planned",
        "--comment",
        "Dropped in intake review: not feasible for years",
    )


def _comment_calls(env: Env) -> list[Any]:
    return [c for c in env.run.call_args_list if c.args[:2] == ("issue", "comment")]


def test_route_defer_moves_intake_item_and_records(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    result = route(ITEM, Defer(reason="waits on X", revisit_when="X ships"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.ensure_deferred_epic.assert_called_once_with("org/.github")
    # The intake parent is replaced atomically, never unlinked first.
    env.reparent_child.assert_called_once_with(DEFERRED, ITEM)
    assert env.native_parent[ITEM] == DEFERRED
    env.run.assert_called_once_with(
        "issue",
        "comment",
        "352",
        "--repo",
        "org/.github",
        "--body",
        "Deferred 2026-10-23: waits on X\nRevisit when: X ships\nDeferred from: org/.github#7",
    )


def test_route_defer_links_before_commenting(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    order: list[str] = []
    env.reparent_child.side_effect = lambda *a: order.append("link") or env.attach(*a)
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert order == ["link", "issue:comment"]


def test_route_defer_date_override(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    route(ITEM, Defer(reason="r", revisit_when="w", day=date(2026, 10, 24)), today=TODAY)
    assert _comment_calls(env)[0].args[-1].startswith("Deferred 2026-10-24: r\n")


def test_route_defer_defaults_to_today(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    with patch(f"{_MOD}.date") as mock_date:
        mock_date.today.return_value = date(2027, 1, 2)
        route(ITEM, Defer(reason="later", revisit_when="soon"))
    assert _comment_calls(env)[0].args[-1].startswith("Deferred 2027-01-02: later\n")


def test_route_defer_refresh_does_not_move_or_add_from_line(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    result = route(ITEM, Defer(reason="still waiting", revisit_when="Y ships"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.run.assert_called_once_with(
        "issue",
        "comment",
        "352",
        "--repo",
        "org/.github",
        "--body",
        "Deferred 2026-10-23: still waiting\nRevisit when: Y ships",
    )
    env.ensure_deferred_epic.assert_not_called()
    env.add_child.assert_not_called()
    env.reparent_child.assert_not_called()


def test_route_defer_from_planning_removes_in_progress(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.labels = ["in-progress", "idea"]
    env.comments = ["Some progress note."]
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.reparent_child.assert_called_once_with(DEFERRED, ITEM)
    env.run.assert_any_call(
        "issue", "edit", "352", "--repo", "org/.github", "--remove-label", "in-progress"
    )
    body = _comment_calls(env)[0].args[-1]
    assert body.endswith("\nDeferred from: org/.github#8")


def test_route_defer_from_planning_without_in_progress_leaves_labels(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert [c.args[1] for c in env.run.call_args_list] == ["comment"]


def test_route_defer_from_adhoc_moves(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.reparent_child.assert_called_once_with(DEFERRED, ITEM)
    assert _comment_calls(env)[0].args[-1].endswith("\nDeferred from: org/.github#9")


def test_route_defer_refuses_development_epic_task(env: Env) -> None:
    env.parent = DEV_EPIC
    with pytest.raises(RouteRefusedError, match=r"org/\.github#400"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.run.assert_not_called()
    env.add_child.assert_not_called()
    env.reparent_child.assert_not_called()
    env.ensure_deferred_epic.assert_not_called()


def test_route_defer_refuses_unparented(env: Env) -> None:
    env.parent = None
    with pytest.raises(RouteRefusedError, match="has no parent"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.run.assert_not_called()
    env.ensure_deferred_epic.assert_not_called()


@pytest.mark.parametrize(
    "action", [Defer(reason="r", revisit_when="w"), Drop(reason="no longer needed")]
)
def test_route_planning_with_epic_marker_refuses_defer_and_drop(
    env: Env, action: Defer | Drop
) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.labels = ["in-progress"]
    env.comments = ["Epic: org/.github#600\nDocs task: org/.github#601"]
    with pytest.raises(RouteRefusedError, match=r"has a development epic \(org/\.github#600\)"):
        route(ITEM, action, today=TODAY)
    env.run.assert_not_called()
    env.reparent_child.assert_not_called()
    env.ensure_deferred_epic.assert_not_called()


def test_route_drop_planning_without_marker_closes(env: Env) -> None:
    env.parent = PLANNING
    env.comments = ["Just a note."]
    route(ITEM, Drop(reason="superseded"), today=TODAY)
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "not planned",
        "--comment",
        "Dropped in planning review: superseded",
    )


def test_route_defer_adhoc_refuses_open_closing_pr(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.closing_prs = {
        "https://github.com/org/tool/pull/41": "MERGED",
        "https://github.com/org/tool/pull/42": "OPEN",
    }
    with pytest.raises(RouteRefusedError, match=r"https://github\.com/org/tool/pull/42"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.run.assert_not_called()
    env.reparent_child.assert_not_called()
    env.ensure_deferred_epic.assert_not_called()


def test_route_defer_adhoc_ignores_closed_pr(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    env.closing_prs = {
        "https://github.com/org/tool/pull/41": "MERGED",
        "https://github.com/org/tool/pull/43": "CLOSED",
    }
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.reparent_child.assert_called_once_with(DEFERRED, ITEM)


def test_route_defer_comment_failure_after_move_fails_loudly(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, Defer(reason="waits on X", revisit_when="X ships"), today=TODAY)
    message = str(excinfo.value)
    assert env.native_parent[ITEM] == DEFERRED
    assert "org/.github#352 is under the deferred epic org/.github#10" in message
    assert "without a deferral record" in message
    assert "HTTP 502" in message
    # The full intended comment, including the origin a refresh would not write.
    assert (
        "Deferred 2026-10-23: waits on X\nRevisit when: X ships\nDeferred from: org/.github#7"
        in message
    )


def test_route_defer_label_failure_after_move_fails_loudly(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.labels = ["in-progress"]
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    message = str(excinfo.value)
    assert "without a deferral record" in message
    assert "--remove-label in-progress" in message
    assert "Deferred 2026-10-23: r\nRevisit when: w\nDeferred from: org/.github#8" in message


def test_route_defer_refuses_private_item_to_public_deferred_epic(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    env.public = {"org/secret": False}
    with pytest.raises(RouteRefusedError, match="private repo org/secret"):
        route(secret_item, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.reparent_child.assert_not_called()
    env.add_child.assert_not_called()
    env.run.assert_not_called()


def test_route_defer_uses_items_repo_queue_home(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    secret_deferred = IssueRef("org", "secret", 3)
    env.ensure_deferred_epic.return_value = secret_deferred
    env.public = {"org/secret": False}
    route(secret_item, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.ensure_deferred_epic.assert_called_once_with("org/secret")
    env.add_child.assert_called_once_with(secret_deferred, secret_item)


# -- revive (out of deferred) ------------------------------------------------


def test_route_to_intake_from_deferred(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    result = route(ITEM, ToIntake(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.ensure_intake_epic.assert_called_once_with("org/.github")
    env.reparent_child.assert_called_once_with(INTAKE, ITEM)
    env.run.assert_not_called()


def test_route_to_intake_refused_from_intake(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="the intake route is not allowed"):
        route(ITEM, ToIntake(), today=TODAY)
    env.ensure_intake_epic.assert_not_called()
    env.reparent_child.assert_not_called()


def test_route_to_intake_refuses_private_item_to_public_intake_epic(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    env.parent = DEFERRED
    env.public = {"org/secret": False}
    with pytest.raises(RouteRefusedError, match="private repo org/secret"):
        route(secret_item, ToIntake(), today=TODAY)
    env.reparent_child.assert_not_called()
    env.add_child.assert_not_called()


def test_route_planning_from_deferred(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    route(ITEM, ToPlanning(), today=TODAY)
    env.reparent_child.assert_called_once_with(PLANNING, ITEM)


def test_route_one_off_from_deferred_same_repo_reparents(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.reparent_child.assert_called_once_with(ADHOC_DOTGITHUB, ITEM)
    assert _transfer_calls(env) == []


def test_route_split_from_deferred_closes(env: Env) -> None:
    env.parent = DEFERRED
    route(ITEM, Split(into=(IssueRef("org", "tool", 10),), reason="r"), today=TODAY)
    assert env.run.call_args.args[:2] == ("issue", "close")


@pytest.mark.parametrize(
    ("parent", "queue"), [(INTAKE, "intake"), (PLANNING, "planning"), (DEFERRED, "deferred")]
)
def test_route_drop_names_source_queue(env: Env, parent: IssueRef, queue: str) -> None:
    env.parent = parent
    route(ITEM, Drop(reason="obsolete"), today=TODAY)
    assert env.run.call_args.args[-1] == f"Dropped in {queue} review: obsolete"


def test_routes_table_matches_spec() -> None:
    expected: dict[str | None, frozenset[type]] = {
        "intake": frozenset({Drop, Defer, OneOff, ToPlanning, Split}),
        "planning": frozenset({Defer, Drop}),
        "ad-hoc": frozenset({Defer}),
        "deferred": frozenset({Defer, ToIntake, ToPlanning, OneOff, Drop, Split}),
        None: frozenset({ToPlanning}),
    }
    assert expected == intake_route.ROUTES


def test_route_defer_unexpected_issue_view_fails_loudly(env: Env) -> None:
    env.read_json.side_effect = lambda *a: ["not", "a", "dict"]
    with pytest.raises(RuntimeError, match="unexpected 'gh issue view' response"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.reparent_child.assert_not_called()
    env.run.assert_not_called()


def test_route_defer_adhoc_closing_pr_without_url_fails_loudly(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    view = {"labels": [], "comments": [], "closedByPullRequestsReferences": [{"number": 5}]}
    env.read_json.side_effect = lambda *a: view
    with pytest.raises(RuntimeError, match="has no URL"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.reparent_child.assert_not_called()
    env.run.assert_not_called()


@pytest.mark.parametrize(
    ("parent", "action", "name"),
    [
        (PLANNING, ToPlanning(), "planning"),
        (PLANNING, OneOff(repo="org/tool"), "one-off"),
        (ADHOC_DOTGITHUB, Drop(reason="x"), "drop"),
        (ADHOC_DOTGITHUB, ToIntake(), "intake"),
    ],
)
def test_route_refuses_routes_outside_the_table(
    env: Env, parent: IssueRef, action: Any, name: str
) -> None:
    env.parent = parent
    with pytest.raises(RouteRefusedError, match=f"the {name} route is not allowed"):
        route(ITEM, action, today=TODAY)
    env.run.assert_not_called()
    env.reparent_child.assert_not_called()
    env.resolve_epic_ref.assert_not_called()


def test_route_split_closes_with_links(env: Env) -> None:
    into = (IssueRef("org", "tool", 10), IssueRef("org", "lib", 11))
    result = route(ITEM, Split(into=into, reason="two separate fixes"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "completed",
        "--comment",
        "Split into: org/tool#10, org/lib#11. two separate fixes",
    )


def test_route_split_refuses_empty_into(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="at least one"):
        route(ITEM, Split(into=(), reason="x"), today=TODAY)
    env.run.assert_not_called()


# -- eligibility -------------------------------------------------------------


def test_route_refuses_development_epic_task(env: Env) -> None:
    env.parent = DEV_EPIC
    with pytest.raises(RouteRefusedError, match=r"under org/\.github#400, not a container epic"):
        route(ITEM, Drop(reason="x"), today=TODAY)
    env.run.assert_not_called()


def test_route_refuses_unparented_item_for_non_planning(env: Env) -> None:
    env.parent = None
    with pytest.raises(RouteRefusedError, match="has no parent"):
        route(ITEM, Drop(reason="x"), today=TODAY)
    env.run.assert_not_called()


def test_route_refuses_planning_item_for_planning(env: Env) -> None:
    env.parent = PLANNING
    with pytest.raises(RouteRefusedError, match="the planning route is not allowed"):
        route(ITEM, ToPlanning(), today=TODAY)
    env.reparent_child.assert_not_called()


def test_route_runs_under_item_owner_token(env: Env) -> None:
    seen: list[str | None] = []

    def fake_close(*args: str) -> None:
        seen.append(github._target_org.get())

    env.run.side_effect = fake_close
    route(ITEM, Drop(reason="x"), today=TODAY)
    assert seen == ["org"]


# -- planning ----------------------------------------------------------------


def test_route_planning_reparents_to_planning_epic(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.ensure_planning_epic.assert_called_once_with("org/.github")
    # The intake parent is replaced atomically, never unlinked first.
    env.reparent_child.assert_called_once_with(PLANNING, ITEM)
    env.add_child.assert_not_called()


def test_route_planning_accepts_unparented_issue(env: Env) -> None:
    env.parent = None
    result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.add_child.assert_called_once_with(PLANNING, ITEM)
    env.reparent_child.assert_not_called()


def test_route_planning_uses_items_repo_queue_home(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    secret_planning = IssueRef("org", "secret", 2)
    env.ensure_planning_epic.return_value = secret_planning
    env.native_parent[secret_item] = IssueRef("org", "secret", 1)
    env.public = {"org/secret": False}
    route(secret_item, ToPlanning(), today=TODAY)
    env.ensure_planning_epic.assert_called_once_with("org/secret")
    env.reparent_child.assert_called_once_with(secret_planning, secret_item)


def test_route_planning_ensures_in_progress_label_in_items_repo(env: Env) -> None:
    # Routing a member-repo item to planning goes through the real
    # ensure_planning_epic, which ensures 'in-progress' in the item's repo as
    # well as the home, so epic-create can label it (#3520).
    tool_item = IssueRef("org", "tool", 36)
    env.native_parent[tool_item] = INTAKE
    with (
        patch(f"{_MOD}.epics.ensure_planning_epic", _REAL_ENSURE_PLANNING_EPIC),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.epics._find_epic_by_title", return_value=PLANNING),
        patch(f"{_MOD}.epics.ensure_labels") as mock_ensure,
    ):
        route(tool_item, ToPlanning(), today=TODAY)
    assert [c.args for c in mock_ensure.call_args_list] == [
        ("org/.github", ("in-progress",)),
        ("org/tool", ("in-progress",)),
    ]
    env.reparent_child.assert_called_once_with(PLANNING, tool_item)


def test_route_planning_refuses_private_item_to_public_planning_epic(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    env.public = {"org/secret": False}
    with pytest.raises(RouteRefusedError, match="private repo org/secret"):
        route(secret_item, ToPlanning(), today=TODAY)
    env.reparent_child.assert_not_called()


# -- one-off -----------------------------------------------------------------


def test_route_one_off_same_repo_reparents_only(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.resolve_epic_ref.assert_called_once_with("adhoc", repo="org/.github")
    assert _transfer_calls(env) == []
    env.reparent_child.assert_called_once_with(ADHOC_DOTGITHUB, ITEM)
    env.create_issue.assert_not_called()


def test_route_one_off_transfers_then_links(env: Env) -> None:
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    new = IssueRef("org", "tool", 900)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert _transfer_calls(env) == [
        call("issue", "transfer", "352", "org/tool", "--repo", "org/.github")
    ]
    env.resolve_epic_ref.assert_called_once_with("adhoc", repo="org/tool")
    # The transfer dropped the sub-issue link; it is still linked with
    # replaceParent, in case the old parent surfaces before the link lands.
    env.reparent_child.assert_called_once_with(TOOL_ADHOC, new)
    env.add_child.assert_not_called()
    assert env.native_parent[new] == TOOL_ADHOC
    env.sleep.assert_called_once_with(intake_route._SETTLE_SECONDS)
    env.create_issue.assert_not_called()
    env.run.assert_not_called()


def test_route_one_off_transfer_kept_link_is_replaced(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    env.reparent_child.assert_called_once_with(TOOL_ADHOC, new)
    env.add_child.assert_not_called()
    assert env.native_parent[new] == TOOL_ADHOC


def test_route_one_off_transfer_late_parent_is_replaced_again(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    # The transfer carries the intake parent over only after the first link,
    # overwriting it (the silent #3556 failure); the next round replaces it.
    late = iter([INTAKE])

    def carry_over(*_: object) -> None:
        env.native_parent[new] = next(late, TOOL_ADHOC)

    env.sleep.side_effect = carry_over
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert env.reparent_child.call_args_list == [call(TOOL_ADHOC, new), call(TOOL_ADHOC, new)]
    assert env.native_parent[new] == TOOL_ADHOC
    err = capsys.readouterr().err
    assert "org/tool#900 is under org/.github#7 after linking it under org/.github#11" in err
    assert "(round 1/4); replacing the parent" in err


def test_route_one_off_transfer_parent_mismatch_fails_loudly(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    env.reparent_child.side_effect = None  # the link never takes
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert "but it is under org/.github#7, not under org/.github#11" in message
    assert "half-applied" in message
    assert "vrg-epic-move --task org/tool#900 --epic org/.github#11" in message
    assert env.reparent_child.call_count == intake_route._SETTLE_ROUNDS
    assert capsys.readouterr().err.count("replacing the parent") == intake_route._SETTLE_ROUNDS


def test_route_one_off_already_under_epic_is_left(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = TOOL_ADHOC
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.reparent_child.assert_not_called()
    env.add_child.assert_not_called()


def test_route_one_off_reopens_closed_epic_before_reparent(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.epic_state = "closed"
    finite = IssueRef("org", ".github", 50)
    env.resolve_epic_ref.return_value = finite
    route(ITEM, OneOff(repo="org/.github", epic="#50"), today=TODAY)
    env.resolve_epic_ref.assert_called_once_with("#50", repo="org/.github")
    env.run.assert_called_once_with("issue", "reopen", "50", "--repo", "org/.github")
    env.reparent_child.assert_called_once_with(finite, ITEM)


def test_route_one_off_open_epic_is_not_reopened(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    env.run.assert_not_called()


def test_route_one_off_falls_back_to_recreate_when_transfer_fails(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.transfer = github.GitHubAPIError(
        1, ["gh", "issue", "transfer"], "", "GraphQL: Resource not accessible by integration"
    )
    order: list[str] = []
    env.create_issue.side_effect = lambda **kw: order.append("create") or env.created_url
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    env.add_child.side_effect = lambda *a: order.append("link") or env.attach(*a)

    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)

    new = IssueRef("org", "tool", 901)
    assert result == RouteResult(final=new, moved_by="recreate")
    kwargs = env.create_issue.call_args.kwargs
    assert kwargs["repo"] == "org/tool"
    assert kwargs["title"] == "Make the thing faster"
    assert kwargs["body"] == f"It is slow.\n\nMoved from {ORIG_URL}\n"
    # Retired intake labels are not carried; registry labels are, ensured first.
    assert kwargs["labels"] == ["bug"]
    env.ensure_labels.assert_called_once_with("org/tool", ["bug"])
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "not planned",
        "--comment",
        f"Moved to {env.created_url}",
    )
    env.add_child.assert_called_once_with(TOOL_ADHOC, new)
    # Never half-moved: the copy exists before the original closes, then it is linked.
    assert order == ["create", "issue:close", "link"]
    err = capsys.readouterr().err
    assert "transfer failed (" in err
    assert "Resource not accessible by integration" in err
    assert f"); recreated as {env.created_url}" in err
    assert "label(s) not carried to org/tool#901 (not in the label registry): off-registry" in err
    # A recreated issue has no parent to carry over: no settle wait.
    env.reparent_child.assert_not_called()
    env.sleep.assert_not_called()


def test_route_recreate_parent_mismatch_fails_loudly(env: Env) -> None:
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")
    env.add_child.side_effect = None  # the link never takes
    with pytest.raises(RuntimeError, match=r"but it is unparented, not under org/\.github#11"):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.add_child.assert_called_once_with(TOOL_ADHOC, IssueRef("org", "tool", 901))


def test_route_recreate_with_empty_body_and_no_labels(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")
    env.issue_view = {"title": "T", "body": None, "labels": [{"name": "idea"}], "url": ORIG_URL}
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    kwargs = env.create_issue.call_args.kwargs
    assert kwargs["body"] == f"Moved from {ORIG_URL}\n"
    assert kwargs["labels"] == []
    env.ensure_labels.assert_not_called()
    assert "not carried" not in capsys.readouterr().err


def test_route_one_off_missing_token_is_not_recreated(env: Env) -> None:
    env.transfer = github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN environment variable")
    with pytest.raises(github.MissingGitHubTokenError):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.create_issue.assert_not_called()


def test_route_one_off_unreadable_transfer_output_fails_loudly(env: Env) -> None:
    env.transfer = "done"
    with pytest.raises(RuntimeError, match="could not read the new issue URL"):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.create_issue.assert_not_called()
    env.add_child.assert_not_called()


def test_route_refuses_private_item_to_public_target(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    env.public = {"org/secret": False, "org/tool": True}
    with pytest.raises(RouteRefusedError, match="private repo org/secret.*public org/tool"):
        route(secret_item, OneOff(repo="org/tool"), today=TODAY)
    env.resolve_epic_ref.assert_not_called()
    assert _transfer_calls(env) == []


def test_route_refuses_private_item_to_public_epic(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    env.public = {"org/secret": False, "org/vault": False, "org/.github": True}
    env.resolve_epic_ref.return_value = IssueRef("org", ".github", 60)
    with pytest.raises(RouteRefusedError, match=r"public org/\.github"):
        route(secret_item, OneOff(repo="org/vault", epic="org/.github#60"), today=TODAY)
    assert _transfer_calls(env) == []


def test_route_private_item_to_private_target_is_allowed(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    env.public = {"org/secret": False, "org/vault": False}
    env.resolve_epic_ref.return_value = IssueRef("org", "vault", 3)
    env.transfer = "https://github.com/org/vault/issues/77"
    result = route(secret_item, OneOff(repo="org/vault"), today=TODAY)
    assert result == RouteResult(final=IssueRef("org", "vault", 77), moved_by="transfer")


def test_route_one_off_refuses_cross_org_repo(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="cross-org"):
        route(ITEM, OneOff(repo="other/tool"), today=TODAY)
    env.resolve_epic_ref.assert_not_called()


def test_route_one_off_refuses_malformed_repo(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="owner/repo"):
        route(ITEM, OneOff(repo="tool"), today=TODAY)


def test_route_one_off_refuses_cross_org_epic(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="cross-org"):
        route(ITEM, OneOff(repo="org/tool", epic="other/.github#3"), today=TODAY)
    env.resolve_epic_ref.assert_not_called()


def test_route_one_off_refuses_queue_epic(env: Env) -> None:
    env.resolve_epic_ref.return_value = INTAKE
    with pytest.raises(RouteRefusedError, match="queue epic"):
        route(ITEM, OneOff(repo="org/tool", epic="intake"), today=TODAY)
    assert _transfer_calls(env) == []


# -- security items keep the advisory link (epic vergil-project/.github#517) ----

SPLIT_INTO = (IssueRef("org", "tool", 10), IssueRef("org", "lib", 11))
RETARGET_FAILED = security_records.RetargetError(
    f"advisory {GHSA}: marking org/tool#10 failed: HTTP 500"
)


def _transfer_fails(env: Env) -> None:
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")


def test_split_security_item_retargets_to_all_tasks_before_close(env: Env) -> None:
    env.make_security_item()
    order: list[str] = []
    env.retarget.side_effect = lambda *a, **kw: order.append("retarget")
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.advisory_home.assert_called_once_with("org")
    env.retarget.assert_called_once_with("org/.github", GHSA, SPLIT_INTO, renamed=())
    assert order == ["retarget", "issue:close"]


def test_split_security_item_retarget_failure_leaves_original_open(env: Env) -> None:
    env.make_security_item()
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    message = str(excinfo.value)
    assert f"advisory {GHSA} was not retargeted to org/tool#10, org/lib#11" in message
    assert "marking org/tool#10 failed" in message
    assert "org/.github#352 is left open" in message
    env.run.assert_not_called()


def test_split_security_item_private_home_leaves_original_open(env: Env) -> None:
    env.make_security_item()
    env.advisory_home.side_effect = security_records.PrivateHomeError("org/.github is not public")
    with pytest.raises(RouteRefusedError, match=f"advisory {GHSA} was not retargeted"):
        route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.retarget.assert_not_called()
    env.run.assert_not_called()


def test_recreate_security_item_retargets_before_closing(env: Env) -> None:
    env.make_security_item()
    _transfer_fails(env)
    order: list[str] = []
    env.create_issue.side_effect = lambda **kw: order.append("create") or env.created_url
    env.retarget.side_effect = lambda *a, **kw: order.append("retarget")
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    new = IssueRef("org", "tool", 901)
    assert result == RouteResult(final=new, moved_by="recreate")
    env.retarget.assert_called_once_with("org/.github", GHSA, (new,), renamed=())
    assert order == ["create", "retarget", "issue:close"]
    # The copy carries the label and the advisory line; retarget's mark is idempotent.
    kwargs = env.create_issue.call_args.kwargs
    assert "security" in kwargs["labels"]
    assert ADVISORY_LINE in kwargs["body"]


def test_recreate_security_item_retarget_failure_leaves_original_open(env: Env) -> None:
    env.make_security_item()
    _transfer_fails(env)
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert f"recreated org/.github#352 as org/tool#901, but advisory {GHSA}" in message
    assert "org/.github#352 is left open" in message
    assert f"vrg-security-record retarget {GHSA} --to org/tool#901" in message
    env.run.assert_not_called()
    env.add_child.assert_not_called()


def test_transfer_security_item_retargets_to_final(env: Env) -> None:
    env.make_security_item()
    order: list[str] = []
    env.sleep.side_effect = lambda *_: order.append("settle")
    env.retarget.side_effect = lambda *a, **kw: order.append("retarget")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    final = IssueRef("org", "tool", 900)
    assert result == RouteResult(final=final, moved_by="transfer")
    # The old number is the moved issue itself: rewritten away, never unmarked.
    env.retarget.assert_called_once_with("org/.github", GHSA, (final,), renamed=(ITEM,))
    assert order == ["settle", "retarget"]
    # The item was read before the move, while it still had its labels.
    env.read_json.assert_called_once_with(
        "issue", "view", "352", "--repo", "org/.github", "--json", "labels,body"
    )


def test_transfer_retarget_failure_reports_moved_not_refused(env: Env) -> None:
    env.make_security_item()
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert not isinstance(excinfo.value, RouteRefusedError)
    assert str(excinfo.value) == (
        f"org/.github#352 moved to org/tool#900, but advisory {GHSA} was not retargeted; "
        f"run: vrg-security-record retarget {GHSA} --to org/tool#900"
    )
    assert excinfo.value.__cause__ is RETARGET_FAILED


def test_transfer_parent_mismatch_also_names_the_advisory(env: Env) -> None:
    env.make_security_item()
    env.reparent_child.side_effect = None  # the link never takes
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert "half-applied" in message
    assert f"also, advisory {GHSA} was not retargeted" in message
    assert f"vrg-security-record retarget {GHSA} --to org/tool#900" in message
    env.retarget.assert_not_called()


def test_non_security_item_never_touches_advisories(env: Env) -> None:
    route(ITEM, Split(into=SPLIT_INTO, reason="x"), today=TODAY)
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    _transfer_fails(env)
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.advisory_home.assert_not_called()
    env.retarget.assert_not_called()


def test_planning_route_leaves_advisory_on_item(env: Env) -> None:
    env.make_security_item()
    route(ITEM, ToPlanning(), today=TODAY)
    env.read_json.assert_not_called()
    env.advisory_home.assert_not_called()
    env.retarget.assert_not_called()


@pytest.mark.parametrize(
    ("body", "labels", "has", "lacks"),
    [
        ("Neutral.", [{"name": "security"}], "the security label", "a Security advisory: line"),
        (ADVISORY_LINE, [], "a Security advisory: line", "the security label"),
    ],
)
def test_half_marked_item_is_warned_and_not_retargeted(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    body: str,
    labels: list[dict[str, str]],
    has: str,
    lacks: str,
) -> None:
    env.issue_view = {**env.issue_view, "body": body, "labels": labels}
    route(ITEM, Split(into=SPLIT_INTO, reason="x"), today=TODAY)
    env.retarget.assert_not_called()
    env.run.assert_called_once()
    err = capsys.readouterr().err
    assert f"org/.github#352 has {has} but not {lacks}" in err
    assert "no advisory retargeted (a broken security pair)" in err


def test_item_with_no_body_or_labels_is_not_a_security_item(env: Env) -> None:
    env.issue_view = {**env.issue_view, "body": None, "labels": None}
    route(ITEM, Split(into=SPLIT_INTO, reason="x"), today=TODAY)
    env.retarget.assert_not_called()


# -- several advisories per item (epic vergil-project/.github#573) ----------------

GHSA_B = "GHSA-efgh-6789-stuv"
NEW = IssueRef("org", "tool", 901)
FINAL = IssueRef("org", "tool", 900)


def _name_two_advisories(env: Env) -> None:
    env.make_security_item()
    body = str(env.issue_view["body"])
    env.issue_view = {**env.issue_view, "body": f"{body}\nSecurity advisory: org/.github {GHSA_B}"}


def test_transfer_retargets_every_advisory(env: Env) -> None:
    _name_two_advisories(env)
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=FINAL, moved_by="transfer")
    assert env.retarget.call_args_list == [
        call("org/.github", GHSA, (FINAL,), renamed=(ITEM,)),
        call("org/.github", GHSA_B, (FINAL,), renamed=(ITEM,)),
    ]


def test_transfer_retarget_failure_names_every_pending_advisory(env: Env) -> None:
    _name_two_advisories(env)
    env.retarget.side_effect = [None, RETARGET_FAILED]
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert not isinstance(excinfo.value, RouteRefusedError)
    # The first advisory was retargeted; only the second is left, with its command.
    assert str(excinfo.value) == (
        f"org/.github#352 moved to org/tool#900, but advisory {GHSA_B} was not retargeted; "
        f"run: vrg-security-record retarget {GHSA_B} --to org/tool#900"
    )
    assert excinfo.value.__cause__ is RETARGET_FAILED


def test_transfer_first_retarget_failure_names_both_advisories(env: Env) -> None:
    _name_two_advisories(env)
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert str(excinfo.value) == (
        f"org/.github#352 moved to org/tool#900, but advisories {GHSA}, {GHSA_B} were not "
        f"retargeted; run: vrg-security-record retarget {GHSA} --to org/tool#900, then "
        f"vrg-security-record retarget {GHSA_B} --to org/tool#900"
    )
    env.retarget.assert_called_once()


def test_transfer_parent_mismatch_names_every_advisory(env: Env) -> None:
    _name_two_advisories(env)
    env.reparent_child.side_effect = None  # the link never takes
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert f"also, advisories {GHSA}, {GHSA_B} were not retargeted" in message
    assert f"vrg-security-record retarget {GHSA_B} --to org/tool#900" in message
    env.retarget.assert_not_called()


def test_recreate_retargets_every_advisory_before_closing(env: Env) -> None:
    _name_two_advisories(env)
    _transfer_fails(env)
    order: list[str] = []
    env.create_issue.side_effect = lambda **kw: order.append("create") or env.created_url
    env.retarget.side_effect = lambda _home, ghsa, *a, **kw: order.append(f"retarget:{ghsa}")
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=NEW, moved_by="recreate")
    assert env.retarget.call_args_list == [
        call("org/.github", GHSA, (NEW,), renamed=()),
        call("org/.github", GHSA_B, (NEW,), renamed=()),
    ]
    assert order == ["create", f"retarget:{GHSA}", f"retarget:{GHSA_B}", "issue:close"]


def test_recreate_second_retarget_failure_leaves_original_open(env: Env) -> None:
    _name_two_advisories(env)
    _transfer_fails(env)
    env.retarget.side_effect = [None, RETARGET_FAILED]
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert f"recreated org/.github#352 as org/tool#901, but advisory {GHSA_B} was not" in message
    assert f"vrg-security-record retarget {GHSA_B} --to org/tool#901" in message
    assert f"retarget {GHSA} " not in message
    assert "org/.github#352 is left open" in message
    env.run.assert_not_called()
    env.add_child.assert_not_called()


def test_split_refuses_an_item_naming_several_advisories(env: Env) -> None:
    _name_two_advisories(env)
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    assert str(excinfo.value) == (
        f"org/.github#352 names several advisories ({GHSA}, {GHSA_B}); retarget each by hand "
        "with vrg-security-record retarget, then split"
    )
    # Refused before any change: no comment, no close, no retarget.
    env.run.assert_not_called()
    env.retarget.assert_not_called()
    env.advisory_home.assert_not_called()


def test_split_one_advisory_unchanged(env: Env) -> None:
    env.make_security_item()
    route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.retarget.assert_called_once_with("org/.github", GHSA, SPLIT_INTO, renamed=())
    env.run.assert_called_once()
    assert env.run.call_args.args[:2] == ("issue", "close")
