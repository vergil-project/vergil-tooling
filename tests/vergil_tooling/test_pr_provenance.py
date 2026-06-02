"""Tests for vergil_tooling.lib.pr_provenance."""

from __future__ import annotations

import json
from unittest.mock import patch

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
        if args[0] == "api" and args[1].endswith("/reviews"):
            return reviews
        if args[0] == "api" and args[1].endswith("/timeline"):
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
        if args[0] == "api" and args[1].endswith("/reviews"):
            return reviews
        if args[0] == "api" and args[1].endswith("/timeline"):
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
