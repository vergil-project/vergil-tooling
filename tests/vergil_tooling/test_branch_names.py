"""Tests for the canonical work-unit identifier helpers (issue #2550)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import branch_names


class TestRequiresIssueNumber:
    @pytest.mark.parametrize(
        "branch",
        ["feature/1-x", "bugfix/2-y", "hotfix/3-z", "chore/4-w", "feature/no-number"],
    )
    def test_issue_prefixes_require(self, branch: str) -> None:
        assert branch_names.requires_issue_number(branch) is True

    @pytest.mark.parametrize(
        "branch",
        ["develop", "main", "release/2.1.180", "promotion/main", "gh-pages", ""],
    )
    def test_non_issue_prefixes_exempt(self, branch: str) -> None:
        assert branch_names.requires_issue_number(branch) is False


class TestIsValidIssueBranch:
    @pytest.mark.parametrize(
        "branch",
        [
            "feature/42-add-caching",
            "bugfix/99-fix-parsing",
            "hotfix/7-patch",
            "chore/5-update-deps",
            "feature/1-a",
            "feature/12-a.b-c",
        ],
    )
    def test_valid(self, branch: str) -> None:
        assert branch_names.is_valid_issue_branch(branch) is True

    @pytest.mark.parametrize(
        "branch",
        [
            "feature/no-number",
            "feature/-leading-dash",
            "feature/42",
            "feature/42-",
            "feature/42-Upper",
            "feature/42-has space",
            "release/2.1.180",
            "develop",
            "feature//double",
        ],
    )
    def test_invalid(self, branch: str) -> None:
        assert branch_names.is_valid_issue_branch(branch) is False


class TestParseIssueBranch:
    def test_parses_components(self) -> None:
        parsed = branch_names.parse_issue_branch("feature/911-authz-apply-tag")
        assert parsed == branch_names.ParsedBranch(
            type="feature", number="911", slug="authz-apply-tag"
        )

    def test_parses_each_type(self) -> None:
        parsed = branch_names.parse_issue_branch("chore/5-x")
        assert parsed is not None
        assert parsed.type == "chore"

    def test_returns_none_for_non_issue_branch(self) -> None:
        assert branch_names.parse_issue_branch("release/2.1.180") is None

    def test_returns_none_for_malformed(self) -> None:
        assert branch_names.parse_issue_branch("feature/no-number") is None


class TestExpectedWorktreeDirname:
    def test_derives_issue_dir(self) -> None:
        assert (
            branch_names.expected_worktree_dirname("feature/911-authz-apply-tag")
            == "issue-911-authz-apply-tag"
        )

    def test_type_agnostic_prefix(self) -> None:
        # The directory prefix is always ``issue-`` regardless of branch type.
        assert branch_names.expected_worktree_dirname("bugfix/5-fix") == "issue-5-fix"

    def test_none_for_non_issue_branch(self) -> None:
        assert branch_names.expected_worktree_dirname("release/2.1.180") is None

    def test_none_for_malformed(self) -> None:
        assert branch_names.expected_worktree_dirname("feature/no-number") is None
