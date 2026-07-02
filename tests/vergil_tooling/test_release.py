"""Tests for vergil_tooling.lib.release."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.release import is_release_branch, is_release_tracking_issue


@pytest.mark.parametrize(
    "branch",
    [
        "release/1.4.9",
        "release/2.0.0",
        "release/0.1.0",
        "release/bump-version-1.4.10",
        "release/bump-version-0.1.1",
        "release/42-next-cycle-deps-1.4.10",
        "release/99-next-cycle-deps-2.0.1",
        "chore/bump-version-1.4.10",
        "chore/bump-version-0.1.1",
        "chore/42-next-cycle-deps-1.4.10",
        "chore/99-next-cycle-deps-2.0.1",
    ],
)
def test_release_branch_allowed(branch: str) -> None:
    assert is_release_branch(branch) is True


@pytest.mark.parametrize(
    "branch",
    [
        "feature/42-foo",
        "bugfix/99-bar",
        "chore/update-deps",
        "hotfix/critical",
        "main",
        "develop",
        "release",
        "",
    ],
)
def test_non_release_branch_denied(branch: str) -> None:
    assert is_release_branch(branch) is False


_MARKER = "<!-- vrg-release:progress -->"


def test_release_issue_detected_by_body_marker() -> None:
    # The checklist marker is authoritative even if the title was hand-edited.
    assert is_release_tracking_issue(title="anything at all", body=f"intro\n{_MARKER}\n- [ ] x")


def test_release_issue_detected_by_title_when_body_absent() -> None:
    # Secondary signal: a caller with only the title still classifies it.
    assert is_release_tracking_issue(title="release: 2.1.4", body=None)
    assert is_release_tracking_issue(title="release:   0.10.0", body="")


@pytest.mark.parametrize(
    "title",
    [
        "feat(release): overhaul the release pipeline",
        "release: overhaul the pipeline",  # no version -> not a tracking issue
        "chore: release notes",
        "fix(reporting): exclude release-tracking issues",
        "",
    ],
)
def test_non_release_titles_are_not_tracking_issues(title: str) -> None:
    assert is_release_tracking_issue(title=title, body="") is False


def test_no_signals_is_false() -> None:
    assert is_release_tracking_issue() is False
    assert is_release_tracking_issue(title=None, body=None) is False
