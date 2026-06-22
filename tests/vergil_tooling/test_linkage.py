"""Tests for vergil_tooling.lib.linkage."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.linkage import extract_tracking_issue, normalize_linkage


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


def test_normalize_bare_keyword_no_warning() -> None:
    assert normalize_linkage("Ref") == ("Ref", None)


def test_normalize_bare_keyword_trims_whitespace() -> None:
    assert normalize_linkage("  Ref  ") == ("Ref", None)


def test_normalize_strips_issue_number_and_warns() -> None:
    canonical, warning = normalize_linkage("Ref #1761")
    assert canonical == "Ref"
    assert warning is not None
    assert "Ref #1761" in warning
    assert "Ref" in warning


def test_normalize_strips_cross_repo_reference() -> None:
    canonical, warning = normalize_linkage("Ref org/repo#42")
    assert canonical == "Ref"
    assert warning is not None


def test_normalize_rejects_wrong_keyword_with_number() -> None:
    """The 'Refs #N' round-trip case: wrong keyword, clear contract message."""
    with pytest.raises(ValueError, match="bare keyword") as exc:
        normalize_linkage("Refs #1761")
    assert "Refs #1761" in str(exc.value)
    assert "Ref" in str(exc.value)


def test_normalize_rejects_autoclose_keyword() -> None:
    with pytest.raises(ValueError, match="bare keyword"):
        normalize_linkage("Closes")


def test_normalize_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="bare keyword"):
        normalize_linkage("")


def test_normalize_rejects_trailing_garbage() -> None:
    with pytest.raises(ValueError, match="bare keyword"):
        normalize_linkage("Ref #1761 extra")
