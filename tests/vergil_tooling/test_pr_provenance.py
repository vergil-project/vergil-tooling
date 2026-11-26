"""Tests for vergil_tooling.lib.pr_provenance."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github, pr_provenance, trust
from vergil_tooling.lib.pr_provenance import Action, Role
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

AUTHORS = TrustedAuthors(
    human=("alice", "wphillipmoore"),
    agents=("a-vergil-user", "a-vergil-audit", "the-agent"),
    orgs={"o": OrgConfig("o-automation")},
)
_AT = "2026-11-22T00:00:00Z"
_SPAM = "SPAM-MARKER-TEXT see https://example.invalid/x"
# The ``gh pr view --json author`` answer for a PR the human opened.
_ALICE_VIEW = json.dumps({"author": {"login": "alice", "is_bot": False}})


@pytest.fixture(autouse=True)
def _trusted_authors() -> Any:
    with patch("vergil_tooling.lib.pr_provenance.load_trusted_authors", return_value=AUTHORS):
        yield


def _bot(login: str) -> dict[str, str]:
    """A REST user object for the App *login*."""
    return {"login": f"{login}[bot]", "type": "Bot"}


def _user(login: str) -> dict[str, str]:
    """A REST user object for the account *login*."""
    return {"login": login, "type": "User"}


def _classify(login: str | None, typename: str | None) -> Role:
    return pr_provenance.classify(trust.Identity(login, typename), org="o", authors=AUTHORS)


def _pr_view(number: int, repo: str = "o/r") -> str:
    """The ``gh pr view --json number,url`` answer for PR *number* of *repo*."""
    return json.dumps({"number": number, "url": f"https://github.com/{repo}/pull/{number}"})


def test_provenance_roles_from_config_not_suffix() -> None:
    # A configured agent is the user identity, or the audit one by its login's
    # ending; the naming convention alone never makes a login an agent.
    assert _classify("a-vergil-user[bot]", "Bot") is Role.USER
    assert _classify("a-vergil-audit", "Bot") is Role.AUDIT
    assert _classify("the-agent", "Bot") is Role.USER
    assert _classify("app/o-automation", "Bot") is Role.AUTOMATION
    assert _classify("alice", "User") is Role.HUMAN
    assert _classify("stranger-vergil-user", "Bot") is Role.UNTRUSTED


def test_provenance_stranger_is_untrusted() -> None:
    # A login with no role is untrusted, never human: nor is a role's login
    # of the wrong account type, nor a deleted account.
    assert _classify("stranger", "User") is Role.UNTRUSTED
    assert _classify("a-vergil-user", "User") is Role.UNTRUSTED
    assert _classify("alice", "Bot") is Role.UNTRUSTED
    assert _classify(None, None) is Role.UNTRUSTED


@pytest.mark.parametrize(
    "action", ["created", "edited", "closed", "reopened", "merged", "auto_merge_enabled"]
)
def test_evaluate_untrusted_forbidden_actions(action: str) -> None:
    result = pr_provenance.evaluate([Action("stranger", Role.UNTRUSTED, action)])
    assert [v.action for v in result.violations] == [action]


def test_evaluate_automation_is_never_forbidden() -> None:
    actions = [Action("o-automation", Role.AUTOMATION, a) for a in ("created", "merged")]
    assert pr_provenance.evaluate(actions).ok


def test_check_pr_stranger_author_is_a_violation() -> None:
    view = json.dumps({"author": {"login": "stranger", "is_bot": False}})
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_paged_fake([[]], [[]], [], author_view=view),
    ):
        result = pr_provenance.check_pr("9")
    assert [(v.login, v.role, v.action) for v in result.violations] == [
        ("stranger", Role.UNTRUSTED, "created")
    ]


def test_check_pr_deleted_author_is_a_violation() -> None:
    view = json.dumps({"author": None})
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_paged_fake([[]], [[]], [], author_view=view),
    ):
        result = pr_provenance.check_pr("9")
    assert [(v.login, v.action) for v in result.violations] == [("(deleted account)", "created")]


def test_review_by_outsider_is_reported_not_a_violation(capsys: pytest.CaptureFixture[str]) -> None:
    """Anyone may review a public PR: an outsider's approval is reported, metadata only."""
    reviews: list[dict[str, object]] = [
        {
            "id": 501,
            "state": "APPROVED",
            "user": _user("stranger"),
            "submitted_at": _AT,
            "body": _SPAM,
        },
        {"id": 502, "state": "APPROVED", "user": None, "body": _SPAM},
    ]
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_paged_fake([reviews], [[]], []),
    ):
        result = pr_provenance.check_pr("9")
    assert result.ok
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: o/r#9 review stranger at {_AT} on review 501 (untrusted)\n"
        "skipped untrusted content: o/r#9 review a deleted account at an unknown time"
        " on review 502 (deleted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_evaluate_human_actions_ignored() -> None:
    actions = [Action("alice", Role.HUMAN, "created"), Action("alice", Role.HUMAN, "merged")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert not result.violations


def test_evaluate_audit_approval_is_allowed() -> None:
    # Two outcomes only (#4695): the audit identity's approval is allowed.
    actions = [Action("a-vergil-audit", Role.AUDIT, "approved")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert not result.violations


@pytest.mark.parametrize(
    "action", ["created", "edited", "closed", "reopened", "merged", "auto_merge_enabled"]
)
def test_evaluate_audit_forbidden_actions_are_kept(action: str) -> None:
    # The audit identity is unused, but its forbidden actions stay enforced.
    result = pr_provenance.evaluate([Action("a-vergil-audit", Role.AUDIT, action)])
    assert [v.action for v in result.violations] == [action]


def test_evaluate_audit_close_is_violation() -> None:
    actions = [Action("a-vergil-audit", Role.AUDIT, "closed")]
    result = pr_provenance.evaluate(actions)
    assert not result.ok
    assert len(result.violations) == 1


def test_evaluate_user_approval_is_violation() -> None:
    actions = [Action("a-vergil-user", Role.USER, "approved")]
    result = pr_provenance.evaluate(actions)
    assert not result.ok
    assert len(result.violations) == 1


def test_evaluate_agent_neutral_action_ignored() -> None:
    # An agent action its role does not forbid is allowed.
    actions = [Action("a-vergil-user", Role.USER, "commented")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert not result.violations


def test_check_pr_collects_reviews_and_skips_unmapped() -> None:
    reviews = json.dumps(
        [
            {"state": "APPROVED", "user": _bot("a-vergil-user")},
            {"state": "COMMENTED", "user": {"login": "x"}},  # not an approval
            {"state": "APPROVED", "user": _user("alice")},  # the human's approval
        ]
    )
    timeline = json.dumps(
        [
            {"event": "labeled", "actor": _bot("a-vergil-audit")},  # unmapped event
            {"event": "closed", "actor": _user("alice")},  # the human's close
        ]
    )

    def fake_read_output(*args: str, **_: object) -> str:
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return reviews
        if args[:2] == ("api", "--paginate") and "/timeline?" in args[2]:
            return timeline
        if args[:2] == ("pr", "view") and "number,url" in args:
            return _pr_view(7)
        if args[:2] == ("pr", "view") and "author" in args:
            return _ALICE_VIEW
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch(
            "vergil_tooling.lib.pr_provenance.github.current_repo",
            side_effect=AssertionError("the repo comes from the PR, not the cwd"),
        ),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=fake_read_output,
        ),
    ):
        result = pr_provenance.check_pr("7")
    # Only the user identity's approval is collected, as its one violation.
    assert [(v.login, v.action) for v in result.violations] == [("a-vergil-user", "approved")]


def test_check_pr_flags_audit_close() -> None:
    reviews = json.dumps([])
    timeline = json.dumps([{"event": "closed", "actor": _bot("a-vergil-audit")}])

    def fake_read_output(*args: str, **_: object) -> str:
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return reviews
        if args[:2] == ("api", "--paginate") and "/timeline?" in args[2]:
            return timeline
        if args[:2] == ("pr", "view") and "number,url" in args:
            return _pr_view(42)
        if args[:2] == ("pr", "view") and "author" in args:
            return _ALICE_VIEW  # human author
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch(
            "vergil_tooling.lib.pr_provenance.github.current_repo",
            side_effect=AssertionError("the repo comes from the PR, not the cwd"),
        ),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=fake_read_output,
        ),
    ):
        result = pr_provenance.check_pr("42")
    assert not result.ok
    assert result.violations[0].action == "closed"


def _paged_fake(
    reviews_pages: list[list[dict[str, object]]],
    timeline_pages: list[list[dict[str, object]]],
    calls: list[tuple[str, ...]],
    *,
    author_view: str = _ALICE_VIEW,
) -> object:
    """A ``read_output`` fake that answers the paginated calls the way
    ``gh api --paginate`` does: one JSON document per page, concatenated."""

    def fake_read_output(*args: str, **_: object) -> str:
        calls.append(args)
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return "".join(json.dumps(page) for page in reviews_pages)
        if args[:2] == ("api", "--paginate") and "/timeline?" in args[2]:
            return "".join(json.dumps(page) for page in timeline_pages)
        if args[:2] == ("pr", "view") and "number,url" in args:
            return _pr_view(9)
        if args[:2] == ("pr", "view") and "author" in args:
            return author_view
        raise AssertionError(f"unexpected call: {args}")

    return fake_read_output


def test_check_pr_evaluates_timeline_event_on_second_page() -> None:
    """Issue #3162: a decisive event past the first 30 must not be missed.

    The timeline has 31 events; the only forbidden one (an audit-agent merge)
    is the 31st, on page 2. A single unpaginated call returned only page 1, so
    the check passed when it should have failed.
    """
    page1: list[dict[str, object]] = [
        {"event": "labeled", "actor": _user("alice")} for _ in range(30)
    ]
    page2: list[dict[str, object]] = [{"event": "merged", "actor": _bot("a-vergil-audit")}]
    calls: list[tuple[str, ...]] = []
    with (
        patch(
            "vergil_tooling.lib.pr_provenance.github.current_repo",
            side_effect=AssertionError("the repo comes from the PR, not the cwd"),
        ),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=_paged_fake([[]], [page1, page2], calls),
        ),
    ):
        result = pr_provenance.check_pr("9")
    assert not result.ok
    assert [(v.login, v.action) for v in result.violations] == [("a-vergil-audit", "merged")]
    assert ("api", "--paginate", "repos/o/r/issues/9/timeline?per_page=100") in calls


def test_check_pr_evaluates_review_on_second_page() -> None:
    page1: list[dict[str, object]] = [
        {"state": "COMMENTED", "user": _user("alice")} for _ in range(30)
    ]
    page2: list[dict[str, object]] = [{"state": "APPROVED", "user": _bot("a-vergil-user")}]
    calls: list[tuple[str, ...]] = []
    with (
        patch(
            "vergil_tooling.lib.pr_provenance.github.current_repo",
            side_effect=AssertionError("the repo comes from the PR, not the cwd"),
        ),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=_paged_fake([page1, page2], [[]], calls),
        ),
    ):
        result = pr_provenance.check_pr("9")
    assert not result.ok
    assert [(v.login, v.action) for v in result.violations] == [("a-vergil-user", "approved")]
    assert ("api", "--paginate", "repos/o/r/pulls/9/reviews?per_page=100") in calls


def test_check_pr_malformed_page_fails_closed() -> None:
    """A page that is not a list aborts the check rather than reading as empty."""

    def fake_read_output(*args: str, **_: object) -> str:
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return "[]"
        if args[:2] == ("api", "--paginate") and "/timeline?" in args[2]:
            return '[]{"message": "boom"}'
        if args[:2] == ("pr", "view") and "number,url" in args:
            return _pr_view(9)
        if args[:2] == ("pr", "view"):
            return _ALICE_VIEW
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch(
            "vergil_tooling.lib.pr_provenance.github.current_repo",
            side_effect=AssertionError("the repo comes from the PR, not the cwd"),
        ),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=fake_read_output,
        ),
        pytest.raises(ValueError, match="not a list"),
    ):
        pr_provenance.check_pr("9")


def _cross_repo_fake(pr_view: str, calls: list[tuple[str, ...]]) -> object:
    """A ``read_output`` fake whose cwd (``gh repo view``) is a different repo from the PR's."""

    def fake_read_output(*args: str, **_: object) -> str:
        calls.append(args)
        if args[:2] == ("api", "--paginate"):
            return "[]"
        if args[:2] == ("pr", "view") and "number,url" in args:
            return pr_view
        if args[:2] == ("pr", "view"):
            return _ALICE_VIEW
        if args[:2] == ("repo", "view"):
            return "cwd-org/cwd-repo"
        raise AssertionError(f"unexpected call: {args}")

    return fake_read_output


def test_check_pr_reads_the_prs_own_repo_not_the_cwds() -> None:
    """#4013: a PR URL from another repo is checked in that repo, not the cwd's."""
    pr = "https://github.com/acme/repo/pull/42"
    calls: list[tuple[str, ...]] = []
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_cross_repo_fake(_pr_view(42, "acme/repo"), calls),
    ):
        result = pr_provenance.check_pr(pr)
    assert result.ok
    assert ("pr", "view", pr, "--json", "number,url") in calls
    assert ("api", "--paginate", "repos/acme/repo/pulls/42/reviews?per_page=100") in calls
    assert ("api", "--paginate", "repos/acme/repo/issues/42/timeline?per_page=100") in calls
    assert not [call for call in calls if call[:2] == ("repo", "view")]


@pytest.mark.parametrize(
    "pr_view",
    [
        "[]",
        json.dumps({"number": 42}),
        json.dumps({"number": 42, "url": "https://example.com/acme/repo/pull/42"}),
        json.dumps({"url": "https://github.com/acme/repo/pull/42"}),
    ],
)
def test_check_pr_fails_loud_without_the_prs_repo_and_number(pr_view: str) -> None:
    calls: list[tuple[str, ...]] = []
    with (
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=_cross_repo_fake(pr_view, calls),
        ),
        pytest.raises(github.GitHubAPIError, match="provenance"),
    ):
        pr_provenance.check_pr("https://github.com/acme/repo/pull/42")
    assert not [call for call in calls if call[0] == "api"]


# Timeline payloads recorded by the human on a throwaway PR (issue #4569): the
# ``gh api repos/<o>/<r>/issues/<n>/timeline --paginate`` output, merged into
# one array. The human enabled auto-merge with SQUASH (``auto_squash_enabled``),
# then disabled it (``auto_merge_disabled``) and enabled it with REBASE
# (``auto_rebase_enabled``). The recordings carry the human as actor; the agent
# variants below change only the actor of the event under test.
_FIXTURES = Path(__file__).parent / "fixtures" / "pr_provenance"
_AGENT = "a-vergil-user"


def _recorded_timeline(name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = json.loads((_FIXTURES / f"{name}.json").read_text("utf-8"))
    return events


def _as_agent(
    timeline: list[dict[str, Any]], event_name: str, login: str = _AGENT
) -> list[dict[str, Any]]:
    """*timeline* with the actor of every *event_name* event replaced by *login*."""
    events = copy.deepcopy(timeline)
    matched = [event for event in events if event.get("event") == event_name]
    assert matched, f"the recording has no {event_name} event"
    for event in matched:
        event["actor"] = {**event["actor"], **_bot(login)}
    return events


def _check_timeline(timeline: list[dict[str, Any]]) -> pr_provenance.ProvenanceResult:
    """Run ``check_pr`` against *timeline*, with a human author and no reviews."""
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_paged_fake([[]], [timeline], []),
    ):
        return pr_provenance.check_pr("9")


@pytest.mark.parametrize("login", ["a-vergil-user", "a-vergil-audit"])
def test_recorded_squash_enable_is_violation_for_agent(login: str) -> None:
    timeline = _as_agent(
        _recorded_timeline("auto_merge_squash_enable"), "auto_squash_enabled", login
    )
    result = _check_timeline(timeline)
    assert [(v.login, v.action) for v in result.violations] == [(login, "auto_merge_enabled")]


def test_recorded_rebase_enable_is_violation_for_agent() -> None:
    # Only the REBASE enable is the agent's: the earlier SQUASH enable and the
    # disable stay the human's, so the violation comes from the rebase event.
    timeline = _as_agent(_recorded_timeline("auto_merge_rebase_enable"), "auto_rebase_enabled")
    result = _check_timeline(timeline)
    assert [(v.login, v.action) for v in result.violations] == [(_AGENT, "auto_merge_enabled")]


def test_disable_is_not_a_violation() -> None:
    # The recorded disable, re-attributed to an agent, with every enable left
    # the human's: an agent turning auto-merge off is not a violation.
    timeline = _as_agent(_recorded_timeline("auto_merge_rebase_enable"), "auto_merge_disabled")
    result = _check_timeline(timeline)
    assert result.ok


def test_unknown_auto_enable_fails_closed() -> None:
    # A renamed or new enable event by an agent is a violation, not ignored.
    timeline = [{"event": "auto_frobnicate_enabled", "actor": _bot(_AGENT)}]
    result = _check_timeline(timeline)
    assert [(v.login, v.action) for v in result.violations] == [(_AGENT, "auto_merge_enabled")]


@pytest.mark.parametrize("name", ["auto_merge_squash_enable", "auto_merge_rebase_enable"])
def test_human_auto_merge_enabled_allowed(name: str) -> None:
    result = _check_timeline(_recorded_timeline(name))
    assert result.ok


# The user agent's own PR (epic vergil-project/.github#1384, spec §4.3): it may
# open it, turn auto-merge on and merge it; on anyone else's PR every action
# forbidden before stays a violation, and its approval is one on every PR.
_USER_VIEW = json.dumps({"author": {"login": "app/a-vergil-user", "is_bot": True}})
_OTHER_AGENT_VIEW = json.dumps({"author": {"login": "app/the-agent", "is_bot": True}})


def _check_actions(
    timeline: list[dict[str, Any]],
    *,
    reviews: list[dict[str, object]] | None = None,
    author_view: str = _USER_VIEW,
) -> pr_provenance.ProvenanceResult:
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_paged_fake([reviews or []], [timeline], [], author_view=author_view),
    ):
        return pr_provenance.check_pr("9")


def _violations(result: pr_provenance.ProvenanceResult) -> list[tuple[str, str]]:
    return [(v.login, v.action) for v in result.violations]


@pytest.mark.parametrize("event", ["auto_squash_enabled", "auto_rebase_enabled"])
def test_user_creates_and_automerges_own_pr(event: str) -> None:
    # The author's identity (``app/<slug>`` from ``gh pr view``) and the
    # timeline actor's (``<slug>[bot]``) are one identity once normalised.
    timeline = _as_agent(_recorded_timeline("auto_merge_rebase_enable"), event)
    assert _check_actions(timeline).ok


def test_user_merge_of_own_pr_allowed() -> None:
    # The ``merged_now`` path: GitHub refused auto-merge on a PR that could
    # already merge, so the user agent merged it directly.
    assert _check_actions([{"event": "merged", "actor": _bot(_AGENT)}]).ok


@pytest.mark.parametrize(
    ("event", "action"), [("renamed", "edited"), ("closed", "closed"), ("reopened", "reopened")]
)
def test_user_other_actions_on_own_pr_forbidden(event: str, action: str) -> None:
    # Only create, auto-merge and merge are allowed on its own PR; submit never
    # renames a PR, so ``edited`` stays forbidden there too.
    result = _check_actions([{"event": event, "actor": _bot(_AGENT)}])
    assert _violations(result) == [(_AGENT, action)]


@pytest.mark.parametrize(
    ("event", "action"),
    [
        ("auto_squash_enabled", "auto_merge_enabled"),
        ("merged", "merged"),
        ("closed", "closed"),
        ("reopened", "reopened"),
        ("renamed", "edited"),
    ],
)
def test_user_action_on_human_pr_forbidden(event: str, action: str) -> None:
    result = _check_actions([{"event": event, "actor": _bot(_AGENT)}], author_view=_ALICE_VIEW)
    assert _violations(result) == [(_AGENT, action)]


def test_user_action_on_another_agents_pr_forbidden() -> None:
    # "Its own" is the same identity, not merely the same role: the other
    # agent's opening of its own PR is allowed, this agent's merge of it is not.
    result = _check_actions(
        [{"event": "merged", "actor": _bot(_AGENT)}], author_view=_OTHER_AGENT_VIEW
    )
    assert _violations(result) == [(_AGENT, "merged")]


@pytest.mark.parametrize(
    "view",
    [
        json.dumps({"author": {"login": "a-vergil-user", "is_bot": False}}),
        json.dumps({"author": None}),
    ],
)
def test_user_action_on_untrusted_or_deleted_authors_pr_forbidden(view: str) -> None:
    # A role's login of the wrong account type, or a deleted account, never
    # authors the user agent's own PR.
    result = _check_actions([{"event": "merged", "actor": _bot(_AGENT)}], author_view=view)
    assert (_AGENT, "merged") in _violations(result)


def test_user_approval_forbidden_even_on_own_pr() -> None:
    result = _check_actions([], reviews=[{"state": "APPROVED", "user": _bot(_AGENT)}])
    assert _violations(result) == [(_AGENT, "approved")]


def test_evaluate_without_author_grants_no_own_pr() -> None:
    # No author named: nothing is the user agent's own PR (fails closed).
    actor = trust.Identity("a-vergil-user[bot]", "Bot")
    actions = [Action("a-vergil-user", Role.USER, "merged", actor)]
    assert _violations(pr_provenance.evaluate(actions)) == [(_AGENT, "merged")]
    assert pr_provenance.evaluate(actions, author=trust.Identity("app/a-vergil-user", "Bot")).ok


def test_evaluate_action_without_identity_grants_no_own_pr() -> None:
    actions = [Action("a-vergil-user", Role.USER, "merged")]
    author = trust.Identity("app/a-vergil-user", "Bot")
    assert _violations(pr_provenance.evaluate(actions, author=author)) == [(_AGENT, "merged")]


@pytest.mark.parametrize(
    "action", ["created", "edited", "closed", "reopened", "merged", "auto_merge_enabled"]
)
def test_audit_rules_unchanged(action: str) -> None:
    # The audit identity's own PR grants it nothing; its approval stays allowed.
    actor = trust.Identity("a-vergil-audit[bot]", "Bot")
    author = trust.Identity("app/a-vergil-audit", "Bot")
    result = pr_provenance.evaluate(
        [Action("a-vergil-audit", Role.AUDIT, action, actor)], author=author
    )
    assert [v.action for v in result.violations] == [action]
    approval = [Action("a-vergil-audit", Role.AUDIT, "approved", actor)]
    assert pr_provenance.evaluate(approval, author=author).ok


@pytest.mark.parametrize(
    "action", ["created", "edited", "closed", "reopened", "merged", "auto_merge_enabled"]
)
def test_untrusted_rules_unchanged(action: str) -> None:
    # An untrusted identity's "own" PR grants it nothing either.
    actor = trust.Identity("stranger", "User")
    result = pr_provenance.evaluate(
        [Action("stranger", Role.UNTRUSTED, action, actor)], author=actor
    )
    assert [v.action for v in result.violations] == [action]


# --- the closing that accompanies the user agent's merge of its own PR (#5140) ---------
#
# GitHub records a ``closed`` event, by the merging actor, for every merge.
# ``own_pr_squash_merge.json`` is the recorded timeline of
# vergil-project/vergil-fixture-python-lib PR #87 (``gh api
# repos/<o>/<r>/issues/87/timeline --paginate``, its commit author anonymised),
# the user agent's first own submit: auto_squash_enabled, then ``merged`` and
# ``closed`` by the same App, at the same second, ``closed`` right after
# ``merged`` in the timeline's order. ``own_pr_merge_close_next_second.json``
# is vergil-project/vergil-claude-plugin PR #1375's, read the same way (through
# ``github.read_json_paginated``) and anonymised the same way: GitHub recorded
# its ``closed`` one second after its ``merged`` (vergil-tooling#5178).

_RECORDED_AGENT = "wphillipmoore-vergil-user"
_RECORDED_AUTHORS = TrustedAuthors(
    human=("wphillipmoore",),
    agents=(_RECORDED_AGENT,),
    orgs={"vergil-project": OrgConfig("vergil-project-automation")},
)
_MERGED_AT = "2026-11-24T19:58:07Z"


def _event(name: str, login: str = _AGENT, at: str = _MERGED_AT) -> dict[str, object]:
    return {"event": name, "actor": _bot(login), "created_at": at}


def _after_merge(seconds: int) -> str:
    """The timestamp *seconds* after ``_MERGED_AT``, as GitHub writes one."""
    at = datetime.fromisoformat(_MERGED_AT) + timedelta(seconds=seconds)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize(
    ("fixture", "seconds"),
    [("own_pr_squash_merge", 0), ("own_pr_merge_close_next_second", 1)],
)
def test_recorded_own_pr_merge_close_is_allowed(fixture: str, seconds: int) -> None:
    timeline = _recorded_timeline(fixture)
    names = [event.get("event") for event in timeline]
    merge = names.index("merged")
    assert names[merge + 1] == "closed"
    merged_at = datetime.fromisoformat(timeline[merge]["created_at"])
    closed_at = datetime.fromisoformat(timeline[merge + 1]["created_at"])
    assert closed_at - merged_at == timedelta(seconds=seconds)
    view = json.dumps({"author": {"login": f"app/{_RECORDED_AGENT}", "is_bot": True}})
    with (
        patch(
            "vergil_tooling.lib.pr_provenance.load_trusted_authors",
            return_value=_RECORDED_AUTHORS,
        ),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=_paged_fake([[]], [timeline], [], author_view=view),
        ),
    ):
        result = pr_provenance.check_pr("87")
    assert result.ok, _violations(result)


def test_own_pr_merge_close_is_allowed() -> None:
    assert _check_actions([_event("merged"), _event("closed")]).ok


def test_close_of_own_pr_that_did_not_merge_is_violation() -> None:
    result = _check_actions([_event("closed")])
    assert _violations(result) == [(_AGENT, "closed")]


def test_close_by_another_actor_than_the_merger_is_violation() -> None:
    # The human merged the agent's PR; a close the agent recorded at the same
    # second is not the merge's own closing.
    timeline = [
        {"event": "merged", "actor": _user("alice"), "created_at": _MERGED_AT},
        _event("closed"),
    ]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed")]


def test_close_and_reopen_before_the_merge_is_violation() -> None:
    timeline = [
        _event("closed", at="2026-11-24T19:50:00Z"),
        _event("reopened", at="2026-11-24T19:51:00Z"),
        _event("merged"),
        _event("closed"),
    ]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed"), (_AGENT, "reopened")]


def test_merge_close_window_is_five_seconds() -> None:
    assert timedelta(seconds=5) == pr_provenance.MERGE_CLOSE_WINDOW


@pytest.mark.parametrize("seconds", [1, 5])
def test_close_within_the_window_after_the_merge_is_allowed(seconds: int) -> None:
    # GitHub sometimes records the merge's own close a second later (#5178).
    timeline = [_event("merged"), _event("closed", at=_after_merge(seconds))]
    assert _check_actions(timeline).ok


@pytest.mark.parametrize("seconds", [6, 3600])
def test_close_outside_the_window_after_the_merge_is_violation(seconds: int) -> None:
    timeline = [_event("merged"), _event("closed", at=_after_merge(seconds))]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed")]


def test_close_timestamped_before_the_merge_is_violation() -> None:
    timeline = [_event("merged"), _event("closed", at=_after_merge(-1))]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed")]


@pytest.mark.parametrize("between", ["head_ref_deleted", "labeled", "commented", "renamed"])
def test_close_with_an_event_between_it_and_the_merge_is_violation(between: str) -> None:
    # Any timeline event between them, one the check otherwise ignores
    # included, means the close is not the one GitHub recorded with the merge.
    timeline = [_event("merged"), _event(between), _event("closed")]
    assert (_AGENT, "closed") in _violations(_check_actions(timeline))


@pytest.mark.parametrize("at", ["not a time", "2026-11-24T19:58:07"])
def test_close_with_an_unreadable_timestamp_is_violation(at: str) -> None:
    # Unparseable, or with no zone to compare it in: it fails closed.
    timeline = [_event("merged"), _event("closed", at=at)]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed")]


def test_merge_close_without_timeline_positions_is_violation() -> None:
    # Actions with no timeline position cannot show the close comes right
    # after the merge: it fails closed.
    actor = trust.Identity("a-vergil-user[bot]", "Bot")
    author = trust.Identity("app/a-vergil-user", "Bot")
    actions = [
        Action(_AGENT, Role.USER, "merged", actor, at=_MERGED_AT),
        Action(_AGENT, Role.USER, "closed", actor, at=_MERGED_AT),
    ]
    assert _violations(pr_provenance.evaluate(actions, author=author)) == [(_AGENT, "closed")]
    adjacent = [
        Action(_AGENT, Role.USER, "merged", actor, at=_MERGED_AT, position=3),
        Action(_AGENT, Role.USER, "closed", actor, at=_MERGED_AT, position=4),
    ]
    assert pr_provenance.evaluate(adjacent, author=author).ok


def test_close_before_the_merge_in_timeline_order_is_violation() -> None:
    timeline = [_event("closed"), _event("merged")]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed")]


def test_only_the_first_close_after_the_merge_accompanies_it() -> None:
    timeline = [_event("merged"), _event("closed"), _event("closed")]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed")]


def test_merge_close_without_timestamps_is_violation() -> None:
    # No timestamp to tie the close to the merge: it fails closed.
    timeline = [
        {"event": "merged", "actor": _bot(_AGENT)},
        {"event": "closed", "actor": _bot(_AGENT)},
    ]
    assert _violations(_check_actions(timeline)) == [(_AGENT, "closed")]


def test_merge_close_without_an_actor_is_violation() -> None:
    # No actor to compare with the merge's: it fails closed.
    actor = trust.Identity("a-vergil-user[bot]", "Bot")
    actions = [
        Action(_AGENT, Role.USER, "merged", actor, at=_MERGED_AT, position=0),
        Action(_AGENT, Role.USER, "closed", None, at=_MERGED_AT, position=1),
    ]
    result = pr_provenance.evaluate(actions, author=trust.Identity("app/a-vergil-user", "Bot"))
    assert _violations(result) == [(_AGENT, "closed")]


def test_merge_close_on_another_identitys_pr_is_violation() -> None:
    timeline = [_event("merged"), _event("closed")]
    result = _check_actions(timeline, author_view=_ALICE_VIEW)
    assert _violations(result) == [(_AGENT, "merged"), (_AGENT, "closed")]


@pytest.mark.parametrize(
    ("login", "author", "role"),
    [
        ("a-vergil-audit", "app/a-vergil-audit", Role.AUDIT),
        ("stranger", "stranger", Role.UNTRUSTED),
    ],
)
def test_merge_close_by_a_role_other_than_the_user_identity_is_violation(
    login: str, author: str, role: Role
) -> None:
    actor = trust.Identity(f"{login}[bot]", "Bot")
    actions = [
        Action(login, role, "merged", actor, at=_MERGED_AT),
        Action(login, role, "closed", actor, at=_MERGED_AT),
    ]
    result = pr_provenance.evaluate(actions, author=trust.Identity(author, "Bot"))
    assert [v.action for v in result.violations] == ["merged", "closed"]


# --- pr_actors: the agent-involvement evidence (epic .github#1384, Task 11) ------------


def test_pr_actors_lists_every_acting_identity() -> None:
    """The author, every review's author whatever its state, every timeline actor or user."""
    reviews: list[dict[str, object]] = [
        {"state": "COMMENTED", "user": _bot("a-vergil-user")},
        {"state": "APPROVED", "user": _user("stranger")},
        {"state": "CHANGES_REQUESTED", "user": None},
    ]
    timeline: list[dict[str, object]] = [
        {"event": "labeled", "actor": _bot("a-vergil-audit")},
        {"event": "reviewed", "user": _user("bob")},
        {"event": "committed", "author": {"name": "Some One"}},
        {"event": "head_ref_force_pushed", "actor": _user("alice")},
    ]
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_paged_fake([reviews], [timeline], []),
    ):
        actors = pr_provenance.pr_actors("9")
    assert actors == (
        trust.Identity("alice", "User"),
        trust.Identity("a-vergil-user[bot]", "Bot"),
        trust.Identity("stranger", "User"),
        trust.Identity(None, None),
        trust.Identity("a-vergil-audit[bot]", "Bot"),
        trust.Identity("bob", "User"),
        trust.Identity("alice", "User"),
    )


def test_pr_actors_read_every_page() -> None:
    page1: list[dict[str, object]] = [
        {"event": "labeled", "actor": _user("alice")} for _ in range(30)
    ]
    page2: list[dict[str, object]] = [{"event": "labeled", "actor": _bot("a-vergil-user")}]
    with patch(
        "vergil_tooling.lib.pr_provenance.github.read_output",
        side_effect=_paged_fake([[]], [page1, page2], []),
    ):
        actors = pr_provenance.pr_actors("9")
    assert actors[-1] == trust.Identity("a-vergil-user[bot]", "Bot")


def test_pr_actors_malformed_page_fails_closed() -> None:
    def fake_read_output(*args: str, **_: object) -> str:
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return '[]{"message": "boom"}'
        if args[:2] == ("api", "--paginate"):
            return "[]"
        if args[:2] == ("pr", "view") and "number,url" in args:
            return _pr_view(9)
        if args[:2] == ("pr", "view"):
            return _ALICE_VIEW
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch("vergil_tooling.lib.pr_provenance.github.read_output", side_effect=fake_read_output),
        pytest.raises(ValueError, match="not a list"),
    ):
        pr_provenance.pr_actors("9")
