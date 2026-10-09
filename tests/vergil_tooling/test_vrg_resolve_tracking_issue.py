"""Tests for vergil_tooling.bin.vrg_resolve_tracking_issue."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_resolve_tracking_issue import (
    _extract_pr_number,
    main,
    parse_args,
)

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_resolve_tracking_issue"

_PR_BODY_WITH_REF = {"body": "## Summary\n\nRef #100\n"}


# --- parse_args ---


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.commit == "HEAD"
    assert args.pr is None


def test_parse_args_custom_commit() -> None:
    args = parse_args(["--commit", "abc123"])
    assert args.commit == "abc123"


def test_parse_args_pr_flag() -> None:
    args = parse_args(["--pr", "42"])
    assert args.pr == 42


# --- _extract_pr_number ---


def test_extract_merge_commit() -> None:
    assert _extract_pr_number("Merge pull request #42 from org/branch") == 42


def test_extract_squash_merge() -> None:
    assert _extract_pr_number("feat: add widget (#99)") == 99


def test_extract_squash_merge_trailing_whitespace() -> None:
    assert _extract_pr_number("fix: bug (#7) ") == 7


def test_extract_no_match() -> None:
    assert _extract_pr_number("chore: bump version to 2.0.21") is None


def test_extract_merge_takes_priority_over_squash() -> None:
    subject = "Merge pull request #10 from org/feat (#20)"
    assert _extract_pr_number(subject) == 10


# --- main: merge commit (existing happy path) ---


def test_happy_path_merge_commit(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value=_PR_BODY_WITH_REF),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


def test_commit_arg_forwarded() -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ) as mock_git,
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": "Ref #100\n"}),
    ):
        main(["--commit", "abc123"])
    mock_git.assert_called_once_with("log", "-1", "--format=%s", "abc123")


# --- main: squash merge ---


def test_happy_path_squash_merge(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.git.read_output", return_value="feat: add widget (#42)"),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value=_PR_BODY_WITH_REF),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


# --- main: --pr flag ---


def test_pr_flag_bypasses_commit_parsing(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value=_PR_BODY_WITH_REF),
    ):
        result = main(["--pr", "42"])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


# --- main: API fallback ---


def test_api_fallback_when_no_pattern_matches(capsys: pytest.CaptureFixture[str]) -> None:
    api_response = [{"number": 42, "merged_at": "2026-01-01T00:00:00Z"}]
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", "abc123sha"]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json_paginated", return_value=api_response),
        patch(f"{_MOD}.github.read_json", return_value=_PR_BODY_WITH_REF),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


def test_api_fallback_prefers_merged_pr(capsys: pytest.CaptureFixture[str]) -> None:
    api_response = [
        {"number": 10, "merged_at": None},
        {"number": 42, "merged_at": "2026-01-01T00:00:00Z"},
    ]
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", "abc123sha"]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json_paginated", return_value=api_response),
        patch(f"{_MOD}.github.read_json", return_value=_PR_BODY_WITH_REF),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


def test_api_fallback_uses_first_pr_if_none_merged(capsys: pytest.CaptureFixture[str]) -> None:
    api_response = [{"number": 55, "merged_at": None}]
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", "abc123sha"]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json_paginated", return_value=api_response),
        patch(f"{_MOD}.github.read_json", return_value=_PR_BODY_WITH_REF),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


def test_api_fallback_empty_list_reports_all_strategies(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", "abc123sha"]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json_paginated", return_value=[]),
    ):
        result = main([])
    assert result == 1
    err = capsys.readouterr().err
    assert "merge-commit pattern" in err
    assert "squash-merge pattern" in err
    assert "GitHub API lookup" in err
    assert "--pr N" in err


def test_api_fallback_malformed_response_fails_loudly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A page that is not a list is reported, never read as 'no PR' (issue #3162)."""
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", "abc123sha"]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_output", return_value='{"message": "not a list"}'),
    ):
        result = main([])
    assert result == 2
    err = capsys.readouterr().err
    assert "malformed GitHub API response for commit HEAD" in err
    assert "not a list" in err


def test_api_fallback_reads_every_page(capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #3162: the merged PR on page 2 of ``/commits/{sha}/pulls`` is found."""
    page1 = [{"number": n, "merged_at": None} for n in range(1, 31)]
    page2 = [{"number": 42, "merged_at": "2026-01-01T00:00:00Z"}]
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", "abc123sha"]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_output", return_value=json.dumps(page1) + json.dumps(page2)
        ) as mock_gh,
        patch(f"{_MOD}.github.read_json", return_value=_PR_BODY_WITH_REF) as mock_json,
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"
    mock_gh.assert_called_once_with(
        "api", "--paginate", "repos/org/repo/commits/abc123sha/pulls?per_page=100"
    )
    mock_json.assert_called_once_with("api", "repos/org/repo/pulls/42")


def test_api_fallback_non_int_number(capsys: pytest.CaptureFixture[str]) -> None:
    api_response = [{"number": "not-an-int", "merged_at": "2026-01-01T00:00:00Z"}]
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", "abc123sha"]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json_paginated", return_value=api_response),
    ):
        result = main([])
    assert result == 1
    assert "cannot determine PR" in capsys.readouterr().err


def test_api_fallback_error_still_reports_strategies(
    capsys: pytest.CaptureFixture[str],
) -> None:
    api_err = subprocess.CalledProcessError(returncode=1, cmd=["gh"], stderr="fail")
    with (
        patch(f"{_MOD}.git.read_output", side_effect=["chore: bump version", api_err]),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
    ):
        result = main([])
    assert result == 1
    err = capsys.readouterr().err
    assert "cannot determine PR" in err


# --- main: error paths ---


def test_empty_pr_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": ""}),
    ):
        result = main([])
    assert result == 1
    assert "has no body" in capsys.readouterr().err


def test_null_pr_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": None}),
    ):
        result = main([])
    assert result == 1
    assert "has no body" in capsys.readouterr().err


def test_no_ref_in_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "No linkage here.\n"},
        ),
    ):
        result = main([])
    assert result == 1
    assert "no tracking issue linkage" in capsys.readouterr().err


def test_multiple_refs_in_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "Ref #100\nRef #200\n"},
        ),
    ):
        result = main([])
    assert result == 1
    assert "multiple tracking issue references" in capsys.readouterr().err


def test_gh_api_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(returncode=1, cmd=["gh", "api"], stderr="Not Found")
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", side_effect=err),
    ):
        result = main([])
    assert result == 2
    assert "failed to" in capsys.readouterr().err.lower()


def test_integration_fixture_merge_commit(capsys: pytest.CaptureFixture[str]) -> None:
    commit_subject = "Merge pull request #856 from vergil-project/release/2.0.18"
    pr_body = (
        "## Release 2.0.18\n\n"
        "### Changes\n\n"
        "- fix(repo-config): skip local checks when --repo targets a different repository\n\n"
        "Ref #830\n"
    )
    with (
        patch(f"{_MOD}.git.read_output", return_value=commit_subject),
        patch(
            f"{_MOD}.github.current_repo",
            return_value="vergil-project/vergil-tooling",
        ),
        patch(f"{_MOD}.github.read_json", return_value={"body": pr_body}),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "830"


def test_unexpected_api_response(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value=[]),
    ):
        result = main([])
    assert result == 2
    assert "unexpected API response" in capsys.readouterr().err


def test_git_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(returncode=128, cmd=["git", "log"], stderr="bad revision")
    with patch(f"{_MOD}.git.read_output", side_effect=err):
        result = main([])
    assert result == 2
    assert "failed to" in capsys.readouterr().err.lower()


def test_pr_flag_api_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(returncode=1, cmd=["gh", "api"], stderr="Not Found")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", side_effect=err),
    ):
        result = main(["--pr", "42"])
    assert result == 2
    assert "failed to" in capsys.readouterr().err.lower()
