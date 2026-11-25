"""The tracking issue's identity rule, judged by the trust layer, and its lock.

GitHub is faked at ``github.graphql`` with the trust layer's item and events
queries, in the field shapes of a recorded response (``{ __typename login }``
actors); the layer's own paging and loud failures are tested in
``test_trust.py``. A live recording of a three-page timeline pins that the
guard reads every page and judges a close on the last one.
"""

from __future__ import annotations

import copy
import itertools
import json
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import github, trust
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.guard import (
    GHOST,
    TrackingIssueBreach,
    check_tracking_issue,
    ensure_locked,
)
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

_MOD = "vergil_tooling.lib.release.guard"
_REPO = "owner/repo"
AUTHORS = TrustedAuthors(
    human=("human",), agents=("agent",), orgs={"owner": OrgConfig("owner-automation")}
)

HUMAN = {"__typename": "User", "login": "human"}
AGENT = {"__typename": "Bot", "login": "agent"}
OTHER = {"__typename": "User", "login": "other"}
# A User account named like the agent App, and an App named like the human.
USER_NAMED_LIKE_APP = {"__typename": "User", "login": "agent"}
APP_NAMED_LIKE_HUMAN = {"__typename": "Bot", "login": "human"}


def _event(typename: str, actor: Any, at: str = trust_stubs.EDITED) -> dict[str, Any]:
    return {"__typename": typename, "actor": actor, "createdAt": at}


class _FakeGitHub:
    """The trust layer's item and events reads of the tracking issue, one page each."""

    def __init__(self, item: dict[str, Any], events: list[dict[str, Any]]) -> None:
        self.item = item
        self.events = events
        self.queries: list[str] = []

    def __call__(self, query: str, **variables: object) -> dict[str, Any]:
        assert (variables["owner"], variables["name"], variables["number"]) == ("owner", "repo", 42)
        self.queries.append(query)
        if query == trust._ITEM_QUERY:  # noqa: SLF001 - the layer's own query
            return self.item
        assert query == trust._EVENTS_QUERY  # noqa: SLF001
        return {
            "repository": {
                "issueOrPullRequest": {
                    "__typename": "Issue",
                    "timelineItems": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": self.events,
                    },
                }
            }
        }


def _check(
    *,
    author: Any = HUMAN,
    editor: Any = None,
    edits: list[Any] | None = None,
    comments: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> list[str]:
    item = trust_stubs.item(author=author, editor=editor, nodes=comments)
    body = item["repository"]["issueOrPullRequest"]
    if edits is not None:
        body["userContentEdits"]["nodes"] = [
            {"editedAt": trust_stubs.EDITED, "editor": e} for e in edits
        ]
    fake = _FakeGitHub(item, events or [])
    with patch("vergil_tooling.lib.github.graphql", side_effect=fake):
        return check_tracking_issue(_REPO, 42, authors=AUTHORS)


# --- the identity rule ---------------------------------------------------------


def test_guard_passes_human_only() -> None:
    warnings = _check(
        editor=HUMAN,
        edits=[HUMAN, HUMAN],
        comments=[trust_stubs.comment(1, "a note", HUMAN)],
        events=[_event("ClosedEvent", HUMAN), _event("ReopenedEvent", HUMAN)],
    )
    assert warnings == []


def test_guard_stops_on_agent_body_edit() -> None:
    with pytest.raises(TrackingIssueBreach, match="agent edited the body") as info:
        _check(editor=HUMAN, edits=[HUMAN, AGENT])
    assert (info.value.login, info.value.action) == ("agent", "edited the body")
    assert info.value.cause == trust.NOT_ACCEPTED
    assert str(info.value).startswith("suspected security breach on tracking issue #42:")
    assert isinstance(info.value, ReleaseError)


def test_guard_stops_on_agent_last_editor() -> None:
    with pytest.raises(TrackingIssueBreach, match="agent edited the body"):
        _check(editor=AGENT, edits=[AGENT])


def test_tracking_guard_rejects_agent_with_layer() -> None:
    """An agent App, a trusted identity, holds no role the tracking issue accepts."""
    with pytest.raises(TrackingIssueBreach, match="agent authored the issue") as info:
        _check(author=AGENT)
    assert info.value.cause == trust.NOT_ACCEPTED


def test_tracking_guard_rejects_user_named_like_app() -> None:
    """A role match with the wrong account type is never trusted (spec §5.1)."""
    with pytest.raises(TrackingIssueBreach, match="human authored the issue") as info:
        _check(author=APP_NAMED_LIKE_HUMAN)
    assert info.value.cause == trust.WRONG_TYPE
    with pytest.raises(TrackingIssueBreach, match=r"agent closed the issue \(not-accepted\)"):
        _check(events=[_event("ClosedEvent", AGENT)])
    with pytest.raises(TrackingIssueBreach, match=r"human reopened the issue \(wrong-type\)"):
        _check(events=[_event("ReopenedEvent", APP_NAMED_LIKE_HUMAN)])
    assert _check(comments=[trust_stubs.comment(1, "x", USER_NAMED_LIKE_APP)]) == [
        "agent commented"
    ]


def test_guard_stops_on_foreign_reopen() -> None:
    with pytest.raises(TrackingIssueBreach, match="agent reopened the issue"):
        _check(events=[_event("ClosedEvent", HUMAN), _event("ReopenedEvent", AGENT)])


def test_guard_stops_on_foreign_close() -> None:
    with pytest.raises(TrackingIssueBreach, match="other closed the issue") as info:
        _check(events=[_event("ClosedEvent", OTHER)])
    assert info.value.cause == trust.UNTRUSTED


def test_guard_stops_on_foreign_author() -> None:
    with pytest.raises(TrackingIssueBreach, match="other authored the issue"):
        _check(author=OTHER)


def test_guard_checks_the_body_before_the_events() -> None:
    with pytest.raises(TrackingIssueBreach, match="agent edited the body"):
        _check(edits=[AGENT], events=[_event("ClosedEvent", OTHER)])


def test_guard_treats_a_deleted_account_as_foreign() -> None:
    with pytest.raises(TrackingIssueBreach, match=f"{GHOST} edited the body") as info:
        _check(edits=[None])
    assert info.value.cause == trust.DELETED
    with pytest.raises(TrackingIssueBreach, match=f"{GHOST} closed the issue"):
        _check(events=[_event("ClosedEvent", None)])


def test_guard_breach_names_the_issue_number() -> None:
    breach = TrackingIssueBreach("agent", "edited the body", number=42)
    assert str(breach).startswith("suspected security breach on tracking issue #42: agent")
    assert "can never pass this check again" in str(breach)
    assert "vrg-release --supersede <version>" in str(breach)
    unnumbered = TrackingIssueBreach("agent", "edited the body")
    assert str(unnumbered).startswith("suspected security breach on the tracking issue: agent")


def test_guard_warns_on_foreign_comment_and_crossref() -> None:
    warnings = _check(
        comments=[
            trust_stubs.comment(1, "x", AGENT),
            trust_stubs.comment(2, "y", HUMAN),
        ],
        events=[_event("CrossReferencedEvent", OTHER, "2026-11-03T00:00:00Z")],
    )
    assert warnings == ["agent commented", "other cross-referenced the issue"]


def test_guard_warns_on_a_foreign_comment_edit() -> None:
    warnings = _check(comments=[trust_stubs.comment(1, "x", HUMAN, editor=AGENT)])
    assert warnings == ["agent edited a comment"]


def test_guard_warnings_are_in_time_order() -> None:
    warnings = _check(
        comments=[trust_stubs.comment(1, "x", AGENT)],
        events=[_event("LabeledEvent", OTHER, "2026-10-01T00:00:00Z")],
    )
    assert warnings == ["other added a label", "agent commented"]


@pytest.mark.parametrize(
    ("typename", "action"),
    [
        ("MentionedEvent", "mentioned the issue"),
        ("SubscribedEvent", "subscribed to the issue"),
        ("LabeledEvent", "added a label"),
        ("UnlabeledEvent", "removed a label"),
        ("CrossReferencedEvent", "cross-referenced the issue"),
    ],
)
def test_tracking_guard_still_warns_on_foreign_label_and_mention(
    typename: str, action: str
) -> None:
    assert _check(events=[_event(typename, AGENT)]) == [f"agent {action}"]
    assert _check(events=[_event(typename, HUMAN)]) == []


def test_guard_warnings_carry_no_untrusted_text() -> None:
    warnings = _check(comments=[trust_stubs.comment(1, trust_stubs.SPAM_TEXT, OTHER)])
    assert warnings == ["other commented"]
    assert all("SPAM-MARKER-TEXT" not in warning for warning in warnings)


def test_guard_read_failure_raises() -> None:
    err = github.GitHubAPIError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
    with (
        patch("vergil_tooling.lib.github.graphql", side_effect=err),
        pytest.raises(trust.TrustReadError, match="HTTP 502"),
    ):
        check_tracking_issue(_REPO, 42, authors=AUTHORS)


def test_guard_missing_typename_raises() -> None:
    """An actor without its account type is a read failure, never a guess."""
    with pytest.raises(trust.TrustReadError, match="has no __typename"):
        _check(events=[_event("ClosedEvent", {"login": "human"})])


# --- a recorded multi-page timeline ------------------------------------------------

# The trust layer's events query (trust._EVENTS_QUERY, unmodified) read live on
# 2026-11-25 against vergil-project/.github#1384, a closed epic (issue #5007):
# each page's raw response, in order, untrimmed. Its 243 type-filtered events
# take three pages of 100, 100 and 43, and the one close or reopen event is the
# last node of the last page: the org automation App closing the epic.
#
# The same reads with the connection's counts added gave filteredCount 243, 143
# and 43: after a cursor, filteredCount counts from the cursor onward, so a sum
# of the pages' counts is not the number of events. The guard follows
# hasNextPage and asks for no count (test_events_query_asks_for_no_count).
_RECORDED = Path(__file__).parent / "fixtures" / "tracking_guard" / "epic_close_by_automation.json"
_RECORDED_REPO = "vergil-project/.github"
_RECORDED_NUMBER = 1384
_RECORDED_HUMAN = {"__typename": "User", "login": "wphillipmoore"}
_RECORDED_AUTHORS = TrustedAuthors(
    human=("wphillipmoore",),
    agents=("wphillipmoore-vergil-user",),
    orgs={"vergil-project": OrgConfig("vergil-project-automation")},
)


def _recorded_pages() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = json.loads(_RECORDED.read_text("utf-8"))
    return pages


def _timeline(page: dict[str, Any]) -> dict[str, Any]:
    timeline: dict[str, Any] = page["repository"]["issueOrPullRequest"]["timelineItems"]
    return timeline


class _RecordedGitHub:
    """A human-only issue body, then the recorded event pages, served by cursor."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.cursors: dict[str | None, dict[str, Any]] = {None: pages[0]}
        for before, after in itertools.pairwise(pages):
            self.cursors[_timeline(before)["pageInfo"]["endCursor"]] = after
        self.afters: list[object] = []

    def __call__(self, query: str, **variables: object) -> dict[str, Any]:
        assert (variables["owner"], variables["name"], variables["number"]) == (
            "vergil-project",
            ".github",
            _RECORDED_NUMBER,
        )
        if query == trust._ITEM_QUERY:  # noqa: SLF001 - the layer's own query
            return trust_stubs.item(author=_RECORDED_HUMAN)
        assert query == trust._EVENTS_QUERY  # noqa: SLF001
        after = variables.get("after")
        self.afters.append(after)
        return copy.deepcopy(self.cursors[cast("str | None", after)])


def _check_recorded(pages: list[dict[str, Any]]) -> tuple[_RecordedGitHub, list[str]]:
    fake = _RecordedGitHub(pages)
    with patch("vergil_tooling.lib.github.graphql", side_effect=fake):
        warnings = check_tracking_issue(_RECORDED_REPO, _RECORDED_NUMBER, authors=_RECORDED_AUTHORS)
    return fake, warnings


def test_recorded_timeline_shape() -> None:
    """The recording is the multi-page case: three pages, the close last of all."""
    pages = _recorded_pages()
    sizes = [len(_timeline(page)["nodes"]) for page in pages]
    assert sizes == [100, 100, 43]
    assert [_timeline(page)["pageInfo"]["hasNextPage"] for page in pages] == [True, True, False]
    last = _timeline(pages[-1])["nodes"][-1]
    assert last["__typename"] == "ClosedEvent"
    assert last["actor"] == {"__typename": "Bot", "login": "vergil-project-automation"}
    state = [
        node
        for page in pages
        for node in _timeline(page)["nodes"]
        if node["__typename"] in ("ClosedEvent", "ReopenedEvent")
    ]
    assert state == [last]


def test_guard_sees_a_close_actor_on_the_last_recorded_page() -> None:
    """The guard reads every page and judges the close at the end of the last one."""
    fake = _RecordedGitHub(_recorded_pages())
    with (
        patch("vergil_tooling.lib.github.graphql", side_effect=fake),
        pytest.raises(
            TrackingIssueBreach,
            match=r"vergil-project-automation closed the issue \(not-accepted\)",
        ) as info,
    ):
        check_tracking_issue(_RECORDED_REPO, _RECORDED_NUMBER, authors=_RECORDED_AUTHORS)
    assert info.value.number == _RECORDED_NUMBER
    assert len(fake.afters) == 3


def test_guard_reads_every_recorded_event() -> None:
    """With the close by the human, all 243 events are read and judged, no page missed."""
    pages = _recorded_pages()
    _timeline(pages[-1])["nodes"][-1]["actor"] = _RECORDED_HUMAN
    fake, warnings = _check_recorded(pages)
    assert fake.afters == [None] + [_timeline(page)["pageInfo"]["endCursor"] for page in pages[:2]]
    # Every agent trace on every page is a warning: 171 cross-references, 2 labels.
    agent = [
        node
        for page in pages
        for node in _timeline(page)["nodes"]
        if node["actor"]["login"] == "wphillipmoore-vergil-user"
    ]
    assert len(agent) == len(warnings) == 173
    assert warnings.count("wphillipmoore-vergil-user cross-referenced the issue") == 171
    assert warnings.count("wphillipmoore-vergil-user added a label") == 2


def test_events_query_asks_for_no_count() -> None:
    """Paging follows hasNextPage alone: a per-page count is not a total (see above)."""
    assert "hasNextPage" in trust._EVENTS_QUERY  # noqa: SLF001
    assert "Count" not in trust._EVENTS_QUERY  # noqa: SLF001


# --- ensure_locked ---------------------------------------------------------------


def _locked(value: object) -> dict[str, Any]:
    return {"repository": {"issue": {"locked": value}}}


def test_ensure_locked_locks_unlocked_issue() -> None:
    with (
        patch(_MOD + ".github.graphql", return_value=_locked(False)) as gql,
        patch(_MOD + ".github.run") as run,
    ):
        ensure_locked(_REPO, 42)
    assert gql.call_args.kwargs == {"owner": "owner", "name": "repo", "number": 42}
    run.assert_called_once_with("issue", "lock", "42", "--repo", _REPO)


def test_ensure_locked_is_noop_when_locked() -> None:
    with (
        patch(_MOD + ".github.graphql", return_value=_locked(True)),
        patch(_MOD + ".github.run") as run,
    ):
        ensure_locked(_REPO, 42)
    run.assert_not_called()


@pytest.mark.parametrize("data", [{}, _locked(None), _locked("false")])
def test_ensure_locked_unreadable_flag_raises(data: dict[str, Any]) -> None:
    with (
        patch(_MOD + ".github.graphql", return_value=data),
        patch(_MOD + ".github.run") as run,
        pytest.raises(github.GitHubAPIError, match="cannot read the lock"),
    ):
        ensure_locked(_REPO, 42)
    run.assert_not_called()


def test_ensure_locked_lock_failure_raises() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403")
    with (
        patch(_MOD + ".github.graphql", return_value=_locked(False)),
        patch(_MOD + ".github.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        ensure_locked(_REPO, 42)
