"""Tests for standard_tooling.lib.github_config."""

from __future__ import annotations

from standard_tooling.lib.github_config import desired_repo_settings


def test_desired_repo_settings_are_fixed() -> None:
    s = desired_repo_settings()
    assert s.default_branch == "develop"
    assert s.allow_auto_merge is False
    assert s.delete_branch_on_merge is True
    assert s.allow_merge_commit is True
    assert s.allow_squash_merge is True
    assert s.allow_rebase_merge is True
    assert s.has_issues is True
    assert s.has_projects is True
    assert s.has_wiki is True
