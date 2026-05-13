"""Tests for standard_tooling.lib.release."""

from __future__ import annotations

import pytest

from standard_tooling.lib.release import is_release_branch


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
