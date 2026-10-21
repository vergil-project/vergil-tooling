"""Tests for vergil_tooling.lib.commit_message."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.commit_message import (
    ALLOWED_TYPES,
    build_commit_message,
    contains_autoclose,
    find_autoclose,
)


def test_allowed_types_includes_conventional_set() -> None:
    assert "feat" in ALLOWED_TYPES
    assert "revert" in ALLOWED_TYPES


def test_build_subject_only() -> None:
    msg = build_commit_message(commit_type="feat", scope="core", message="add thing")
    assert msg == "feat(core): add thing\n"


def test_build_with_body() -> None:
    msg = build_commit_message(
        commit_type="fix", scope="lint", message="correct regex", body="Edge case."
    )
    assert msg == "fix(lint): correct regex\n\nEdge case.\n"


def test_build_never_adds_co_author_trailer() -> None:
    """AI co-author attribution was dropped (#3200): no trailer, ever."""
    msg = build_commit_message(
        commit_type="feat", scope="core", message="add thing", body="Why it changed."
    )
    assert msg == "feat(core): add thing\n\nWhy it changed.\n"
    assert "Co-Authored-By" not in msg


@pytest.mark.parametrize(
    "body",
    [
        "Closes #42",
        "fixes #42",
        "Resolved owner/repo#42",
        "Some context.\n\nCloses #99",
        "inline resolve #7 here",
    ],
)
def test_contains_autoclose_detects_keywords(body: str) -> None:
    assert contains_autoclose(body) is True


@pytest.mark.parametrize(
    "body",
    ["Ref #42", "This closes the loop.", "Fixed the edge case for input.", ""],
)
def test_contains_autoclose_allows_safe_bodies(body: str) -> None:
    assert contains_autoclose(body) is False


def test_find_autoclose_returns_the_matched_reference() -> None:
    assert find_autoclose("notes begin: Closes #299 and more") == "Closes #299"
    assert find_autoclose("Resolved owner/repo#42") == "Resolved owner/repo#42"


def test_find_autoclose_returns_none_for_safe_text() -> None:
    assert find_autoclose("Ref #42") is None
    assert find_autoclose("fix(rdqm): right-size matrices (#300)") is None
    assert find_autoclose("the fix touches #287 files") is None
