"""Tests for vergil_tooling.lib.linkage."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.linkage import extract_tracking_issue


def test_ref_simple() -> None:
    assert extract_tracking_issue("Ref #123") == 123


def test_ref_with_colon() -> None:
    assert extract_tracking_issue("Ref: #456") == 456


def test_ref_bullet_dash() -> None:
    assert extract_tracking_issue("- Ref #789") == 789


def test_ref_bullet_star() -> None:
    assert extract_tracking_issue("* Ref #789") == 789


def test_ref_indented() -> None:
    assert extract_tracking_issue("  Ref #42") == 42


def test_ref_cross_repo() -> None:
    assert extract_tracking_issue("Ref org/repo#42") == 42


def test_ref_cross_repo_with_colon() -> None:
    assert extract_tracking_issue("Ref: org/repo#42") == 42


def test_no_match_returns_none() -> None:
    assert extract_tracking_issue("No issue reference here.") is None


def test_empty_string_returns_none() -> None:
    assert extract_tracking_issue("") is None


def test_ref_in_multiline_body() -> None:
    body = "## Summary\n\nDoes things.\n\nRef #100\n"
    assert extract_tracking_issue(body) == 100


def test_multiple_refs_raises_value_error() -> None:
    body = "Ref #100\nRef #200\n"
    with pytest.raises(ValueError, match="multiple"):
        extract_tracking_issue(body)


def test_autoclose_keyword_not_matched() -> None:
    assert extract_tracking_issue("Fixes #42") is None
