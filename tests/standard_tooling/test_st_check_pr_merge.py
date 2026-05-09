"""Tests for standard_tooling.bin.st_check_pr_merge."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from standard_tooling.bin.st_check_pr_merge import extract_pr_ref, main

_MOD = "standard_tooling.bin.st_check_pr_merge"


class TestExtractPrRef:
    """Tests for extract_pr_ref (Task 2)."""

    def test_simple_number(self) -> None:
        ref, repo = extract_pr_ref("gh pr merge 42")
        assert ref == "42"
        assert repo is None

    def test_url(self) -> None:
        url = "https://github.com/o/r/pull/364"
        ref, repo = extract_pr_ref(f"gh pr merge {url}")
        assert ref == url
        assert repo is None

    def test_flags_before_ref(self) -> None:
        ref, repo = extract_pr_ref("gh pr merge --squash 364")
        assert ref == "364"

    def test_multiple_flags_before_ref(self) -> None:
        url = "https://github.com/o/r/pull/99"
        ref, repo = extract_pr_ref(f"gh pr merge --merge --delete-branch {url}")
        assert ref == url

    def test_review_approve_number(self) -> None:
        ref, repo = extract_pr_ref("gh pr review --approve 42")
        assert ref == "42"

    def test_review_approve_with_body(self) -> None:
        url = "https://github.com/o/r/pull/77"
        ref, repo = extract_pr_ref(f'gh pr review --approve --body "lgtm" {url}')
        assert ref == url

    def test_repo_flag(self) -> None:
        ref, repo = extract_pr_ref("gh pr merge --repo o/r 42")
        assert ref == "42"
        assert repo == "o/r"

    def test_chained_and(self) -> None:
        ref, repo = extract_pr_ref("echo hi && gh pr merge 42")
        assert ref == "42"

    def test_chained_semicolon(self) -> None:
        ref, repo = extract_pr_ref("echo hi; gh pr merge 42")
        assert ref == "42"

    def test_no_match(self) -> None:
        with pytest.raises(ValueError):
            extract_pr_ref("gh issue list")

    def test_piped(self) -> None:
        ref, repo = extract_pr_ref("echo 42 | gh pr merge 42")
        assert ref == "42"

    def test_empty_segment(self) -> None:
        ref, repo = extract_pr_ref("&& gh pr merge 42")
        assert ref == "42"

    def test_malformed_quoting(self) -> None:
        with pytest.raises(ValueError):
            extract_pr_ref("gh pr merge 'unclosed 42")

    def test_review_without_approve(self) -> None:
        with pytest.raises(ValueError):
            extract_pr_ref("gh pr review --comment 42")

    def test_unknown_subcommand(self) -> None:
        with pytest.raises(ValueError):
            extract_pr_ref("gh pr close 42")

    def test_no_pr_ref(self) -> None:
        with pytest.raises(ValueError):
            extract_pr_ref("gh pr merge --squash")


class TestMain:
    """End-to-end tests for main() (Task 3)."""

    @staticmethod
    def _mock_branch(branch: str) -> Any:
        """Return a context manager that mocks gh pr view to return a branch."""
        return patch(
            f"{_MOD}.github.read_output",
            return_value=branch,
        )

    def test_allowed_release_branch(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("release/1.4.9"):
            rc = main(["gh pr merge 42"])
        assert rc == 0

    def test_allowed_bump_branch(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("release/bump-version-1.4.10"):
            rc = main(["gh pr merge 99"])
        assert rc == 0

    def test_blocked_feature_branch(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("feature/42-foo"):
            rc = main(["gh pr merge 42"])
        assert rc == 1
        assert "may not merge non-release PRs" in capsys.readouterr().err

    def test_flags_before_ref(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("feature/1-x"):
            rc = main(["gh pr merge --squash 364"])
        assert rc == 1

    def test_url_format(self) -> None:
        with self._mock_branch("release/2.0.0"):
            rc = main(["gh pr merge https://github.com/o/r/pull/364"])
        assert rc == 0

    def test_repo_flag_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("feature/1-x") as mock:
            rc = main(["gh pr merge --repo o/r 42"])
        assert rc == 1
        mock.assert_called_once_with(
            "pr",
            "view",
            "42",
            "--repo",
            "o/r",
            "--json",
            "headRefName",
            "--jq",
            ".headRefName",
        )

    def test_api_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        err = subprocess.CalledProcessError(returncode=1, cmd=["gh"], stderr="API error")
        with patch(f"{_MOD}.github.read_output", side_effect=err):
            rc = main(["gh pr merge 42"])
        assert rc == 2
        assert "API error" in capsys.readouterr().err or "Could not" in capsys.readouterr().err

    def test_review_approve_denied(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("feature/1-x"):
            rc = main(["gh pr review --approve 42"])
        assert rc == 1

    def test_review_approve_allowed(self) -> None:
        with self._mock_branch("release/1.0.0"):
            rc = main(["gh pr review --approve 42"])
        assert rc == 0

    def test_chained_and(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("feature/1-x"):
            rc = main(["echo hi && gh pr merge 42"])
        assert rc == 1

    def test_chained_semicolon(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("feature/1-x"):
            rc = main(["echo hi; gh pr merge 42"])
        assert rc == 1

    def test_piped(self, capsys: pytest.CaptureFixture[str]) -> None:
        with self._mock_branch("feature/1-x"):
            rc = main(["echo 42 | gh pr merge 42"])
        assert rc == 1

    def test_repo_flag_allowed(self) -> None:
        with self._mock_branch("release/1.0.0"):
            rc = main(["gh pr merge --repo o/r 42"])
        assert rc == 0

    def test_no_match(self) -> None:
        rc = main(["gh issue list"])
        assert rc == 0
