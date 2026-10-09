"""Tests for vergil_tooling.lib.pr_provenance."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from vergil_tooling.lib import pr_provenance
from vergil_tooling.lib.pr_provenance import Action, Role


def test_classify_login_user() -> None:
    assert pr_provenance.classify_login("alice-vergil-user") is Role.USER


def test_classify_login_audit() -> None:
    assert pr_provenance.classify_login("alice-vergil-audit") is Role.AUDIT


def test_classify_login_human() -> None:
    assert pr_provenance.classify_login("alice") is Role.HUMAN


def test_evaluate_human_actions_ignored() -> None:
    actions = [Action("alice", Role.HUMAN, "created"), Action("alice", Role.HUMAN, "merged")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert not result.violations
    assert not result.advisories


def test_evaluate_audit_approval_is_advisory() -> None:
    actions = [Action("a-vergil-audit", Role.AUDIT, "approved")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert len(result.advisories) == 1
    assert not result.violations


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
    # An agent action that is neither forbidden nor advisory is ignored.
    actions = [Action("a-vergil-user", Role.USER, "commented")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert not result.violations
    assert not result.advisories


def test_check_pr_collects_reviews_and_skips_unmapped() -> None:
    reviews = json.dumps(
        [
            {"state": "APPROVED", "user": {"login": "a-vergil-audit"}},
            {"state": "COMMENTED", "user": {"login": "x"}},  # not an approval
            {"state": "APPROVED", "user": {}},  # approval with no login
        ]
    )
    timeline = json.dumps(
        [
            {"event": "labeled", "actor": {"login": "alice"}},  # unmapped event
            {"event": "closed", "actor": {}},  # mapped but no actor login
        ]
    )

    def fake_read_output(*args: str, **_: object) -> str:
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return reviews
        if args[:2] == ("api", "--paginate") and "/timeline?" in args[2]:
            return timeline
        if args[:2] == ("pr", "view") and "number" in args:
            return "7"
        if args[:2] == ("pr", "view") and "author" in args:
            return ""  # no author login
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch("vergil_tooling.lib.pr_provenance.github.current_repo", return_value="o/r"),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=fake_read_output,
        ),
    ):
        result = pr_provenance.check_pr("7")
    assert result.ok
    assert len(result.advisories) == 1
    assert result.advisories[0].action == "approved"


def test_check_pr_flags_audit_close() -> None:
    reviews = json.dumps([])
    timeline = json.dumps([{"event": "closed", "actor": {"login": "a-vergil-audit"}}])

    def fake_read_output(*args: str, **_: object) -> str:
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return reviews
        if args[:2] == ("api", "--paginate") and "/timeline?" in args[2]:
            return timeline
        if args[:2] == ("pr", "view") and "number" in args:
            return "42"
        if args[:2] == ("pr", "view") and "author" in args:
            return "alice"  # human author
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch("vergil_tooling.lib.pr_provenance.github.current_repo", return_value="o/r"),
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
) -> object:
    """A ``read_output`` fake that answers the paginated calls the way
    ``gh api --paginate`` does: one JSON document per page, concatenated."""

    def fake_read_output(*args: str, **_: object) -> str:
        calls.append(args)
        if args[:2] == ("api", "--paginate") and "/reviews?" in args[2]:
            return "".join(json.dumps(page) for page in reviews_pages)
        if args[:2] == ("api", "--paginate") and "/timeline?" in args[2]:
            return "".join(json.dumps(page) for page in timeline_pages)
        if args[:2] == ("pr", "view") and "number" in args:
            return "9"
        if args[:2] == ("pr", "view") and "author" in args:
            return "alice"
        raise AssertionError(f"unexpected call: {args}")

    return fake_read_output


def test_check_pr_evaluates_timeline_event_on_second_page() -> None:
    """Issue #3162: a decisive event past the first 30 must not be missed.

    The timeline has 31 events; the only forbidden one (an audit-agent merge)
    is the 31st, on page 2. A single unpaginated call returned only page 1, so
    the check passed when it should have failed.
    """
    page1: list[dict[str, object]] = [
        {"event": "labeled", "actor": {"login": "alice"}} for _ in range(30)
    ]
    page2: list[dict[str, object]] = [{"event": "merged", "actor": {"login": "a-vergil-audit"}}]
    calls: list[tuple[str, ...]] = []
    with (
        patch("vergil_tooling.lib.pr_provenance.github.current_repo", return_value="o/r"),
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
        {"state": "COMMENTED", "user": {"login": "alice"}} for _ in range(30)
    ]
    page2: list[dict[str, object]] = [{"state": "APPROVED", "user": {"login": "a-vergil-user"}}]
    calls: list[tuple[str, ...]] = []
    with (
        patch("vergil_tooling.lib.pr_provenance.github.current_repo", return_value="o/r"),
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
        if args[:2] == ("pr", "view"):
            return "9"
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch("vergil_tooling.lib.pr_provenance.github.current_repo", return_value="o/r"),
        patch(
            "vergil_tooling.lib.pr_provenance.github.read_output",
            side_effect=fake_read_output,
        ),
        pytest.raises(ValueError, match="not a list"),
    ):
        pr_provenance.check_pr("9")
