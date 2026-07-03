"""Tests for vergil_tooling.lib.linkage."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.linkage import (
    extract_tracking_issue,
    extract_tracking_ref,
    normalize_linkage,
)


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


def test_banned_autoclose_keyword_not_matched() -> None:
    assert extract_tracking_issue("Fixes #42") is None
    assert extract_tracking_issue("Resolves #42") is None


def test_closes_keyword_matched() -> None:
    assert extract_tracking_issue("Closes #42") == 42
    assert extract_tracking_issue("Close #7") == 7
    assert extract_tracking_issue("Closed #9") == 9
    assert extract_tracking_issue("closes #5") == 5


def test_closes_cross_repo() -> None:
    assert extract_tracking_issue("Closes org/repo#42") == 42


def test_extract_tracking_ref_closes() -> None:
    assert extract_tracking_ref("Closes org/.github#40") == "org/.github#40"
    assert extract_tracking_ref("Closes #42") == "#42"


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


def test_normalize_accepts_closes() -> None:
    # T3 added Closes to ALLOWED_LINKAGES; vrg-submit-pr auto-selects it for
    # managed tasks so it closes the task on merge.
    assert normalize_linkage("Closes") == ("Closes", None)


def test_normalize_rejects_banned_autoclose_keyword() -> None:
    with pytest.raises(ValueError, match="bare keyword"):
        normalize_linkage("Fixes")


def test_normalize_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="bare keyword"):
        normalize_linkage("")


def test_normalize_rejects_trailing_garbage() -> None:
    with pytest.raises(ValueError, match="bare keyword"):
        normalize_linkage("Ref #1761 extra")


def test_extract_tracking_ref_same_repo() -> None:
    assert extract_tracking_ref("Ref #42") == "#42"


def test_extract_tracking_ref_cross_repo() -> None:
    assert extract_tracking_ref("Ref org/.github#40") == "org/.github#40"


def test_extract_tracking_ref_none() -> None:
    assert extract_tracking_ref("no linkage here") is None


def test_extract_tracking_ref_multiple_raises() -> None:
    with pytest.raises(ValueError, match="multiple"):
        extract_tracking_ref("Ref #1\nRef #2")


from vergil_tooling.lib.linkage import find_linkage_keyword


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Ref #157", "Ref #157"),
        ("Ref: #157", "Ref: #157"),
        ("Closes #42", "Closes #42"),
        ("- Fixes #9", "Fixes #9"),
        ("this also Resolves #9 in passing", "Resolves #9"),
        ("Ref owner/repo#3", "Ref owner/repo#3"),
        ("Closes vergil-project/.github#82", "Closes vergil-project/.github#82"),
    ],
)
def test_find_linkage_keyword_matches(text: str, expected: str) -> None:
    assert find_linkage_keyword(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "See #200 for background",
        "Part of epic vergil-project/.github#82",
        "referenced the earlier work",
        "no linkage here",
        "",
    ],
)
def test_find_linkage_keyword_ignores_bare_and_plain(text: str) -> None:
    assert find_linkage_keyword(text) is None


from vergil_tooling.lib.linkage import freetext_linkage_error


def test_freetext_linkage_error_names_offender_and_redirects() -> None:
    msg = freetext_linkage_error("Ref #157", "83")
    assert "Ref #157" in msg
    assert "vrg-gh issue comment 83" in msg
    assert "added for you" in msg
