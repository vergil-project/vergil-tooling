"""Tests for vergil_tooling.lib.intake_route (deterministic intake routing)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, call, patch

import pytest

from vergil_tooling.lib import github, intake_route
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.intake_route import (
    Defer,
    Drop,
    OneOff,
    RouteRefusedError,
    RouteResult,
    Split,
    ToPlanning,
    route,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.intake_route"

ITEM = IssueRef("org", ".github", 352)
INTAKE = IssueRef("org", ".github", 7)
PLANNING = IssueRef("org", ".github", 8)
ADHOC_DOTGITHUB = IssueRef("org", ".github", 9)
TOOL_ADHOC = IssueRef("org", ".github", 11)
TODAY = date(2026, 10, 23)
ORIG_URL = "https://github.com/org/.github/issues/352"


class Env:
    """The patched GitHub and epics boundary one test drives."""

    def __init__(self) -> None:
        self.parent: IssueRef | None = INTAKE
        self.kinds: dict[IssueRef, str | None] = {INTAKE: "intake", PLANNING: "planning"}
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
        self.read_json = MagicMock(side_effect=lambda *a: self.issue_view)
        self.create_issue = MagicMock(side_effect=lambda **kw: self.created_url)
        self.ensure_labels = MagicMock()
        self.add_child = MagicMock()
        self.reparent_child = MagicMock()
        self.ensure_planning_epic = MagicMock(return_value=PLANNING)
        self.resolve_epic_ref = MagicMock(return_value=TOOL_ADHOC)

    def _read_output(self, *args: str) -> str:
        if args[:2] == ("issue", "transfer"):
            if isinstance(self.transfer, BaseException):
                raise self.transfer
            return str(self.transfer)
        assert args[0] == "api"
        assert args[-1] == ".state"
        return self.epic_state


@pytest.fixture
def env() -> Iterator[Env]:
    state = Env()
    with (
        patch(f"{_MOD}.epics.parent_of", side_effect=lambda ref: state.parent),
        patch(f"{_MOD}.epics.queue_kind", side_effect=lambda ref: state.kinds.get(ref)),
        patch(
            f"{_MOD}.epics.native_parent_of",
            side_effect=lambda ref: state.native_parent.get(ref),
        ),
        patch(f"{_MOD}.epics.add_child", state.add_child),
        patch(f"{_MOD}.epics.reparent_child", state.reparent_child),
        patch(f"{_MOD}.epics.ensure_planning_epic", state.ensure_planning_epic),
        patch(f"{_MOD}.epics.resolve_epic_ref", state.resolve_epic_ref),
        patch(f"{_MOD}.github.is_public", side_effect=lambda r: state.public.get(r, True)),
        patch(f"{_MOD}.github.run", state.run),
        patch(f"{_MOD}.github.read_output", state.read_output),
        patch(f"{_MOD}.github.read_json", state.read_json),
        patch(f"{_MOD}.github.create_issue", state.create_issue),
        patch(f"{_MOD}.ensure_labels", state.ensure_labels),
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


def test_route_defer_comments_with_date_only(env: Env) -> None:
    result = route(ITEM, Defer(reason="waiting on the release"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.run.assert_called_once_with(
        "issue",
        "comment",
        "352",
        "--repo",
        "org/.github",
        "--body",
        "Deferred 2026-10-23: waiting on the release",
    )
    env.add_child.assert_not_called()
    env.reparent_child.assert_not_called()


def test_route_defer_defaults_to_today(env: Env) -> None:
    with patch(f"{_MOD}.date") as mock_date:
        mock_date.today.return_value = date(2027, 1, 2)
        route(ITEM, Defer(reason="later"))
    assert env.run.call_args.args[-1] == "Deferred 2027-01-02: later"


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


def test_route_refuses_non_intake_item(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    with pytest.raises(RouteRefusedError, match=r"under org/\.github#9, not an intake epic"):
        route(ITEM, Drop(reason="x"), today=TODAY)
    env.run.assert_not_called()


def test_route_refuses_unparented_item_for_non_planning(env: Env) -> None:
    env.parent = None
    with pytest.raises(RouteRefusedError, match="has no parent"):
        route(ITEM, Defer(reason="x"), today=TODAY)
    env.run.assert_not_called()


def test_route_refuses_planning_item_for_planning(env: Env) -> None:
    env.parent = PLANNING
    with pytest.raises(RouteRefusedError, match="not an intake epic"):
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
    # The transfer dropped the sub-issue link, so it is re-added under the new parent.
    env.add_child.assert_called_once_with(TOOL_ADHOC, new)
    env.create_issue.assert_not_called()
    env.run.assert_not_called()


def test_route_one_off_transfer_kept_link_is_replaced(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.reparent_child.assert_called_once_with(TOOL_ADHOC, new)
    env.add_child.assert_not_called()


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
    env.add_child.side_effect = lambda *a: order.append("link")

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


def test_retired_labels_are_the_old_intake_kinds() -> None:
    assert frozenset({"triage", "idea", "research"}) == intake_route.RETIRED_LABELS
