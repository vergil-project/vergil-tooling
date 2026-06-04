"""Tests for vergil_tooling.bin.vrg_audit_approve."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_audit_approve import CHECK_NAME, main, parse_args

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_audit_approve"
_PR = "https://github.com/pr/1"


def test_check_name_is_pinned_context() -> None:
    # The branch-protection required check is keyed on this exact context name.
    assert CHECK_NAME == "vergil-audit/approved"


def test_parse_args_defaults() -> None:
    args = parse_args([_PR])
    assert args.pr == _PR
    assert args.conclusion == "success"


def test_parse_args_conclusion() -> None:
    args = parse_args([_PR, "--conclusion", "failure"])
    assert args.conclusion == "failure"


def test_main_posts_check_for_resolved_repo_and_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
    with (
        patch(f"{_MOD}.github.current_repo", return_value="owner/repo"),
        patch(f"{_MOD}.github.head_sha", return_value="abc123"),
        patch(f"{_MOD}.github.post_check_run") as mock_post,
    ):
        result = main([_PR])
    assert result == 0
    _, kwargs = mock_post.call_args
    args, _ = mock_post.call_args
    assert args[0] == "owner/repo"
    assert kwargs["name"] == "vergil-audit/approved"
    assert kwargs["head_sha"] == "abc123"
    assert kwargs["conclusion"] == "success"


def test_main_refuses_in_user_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    with patch(f"{_MOD}.github.post_check_run") as mock_post:
        result = main([_PR])
    assert result == 1
    mock_post.assert_not_called()
    assert "user" in capsys.readouterr().err.lower()


def test_main_allows_human_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
    monkeypatch.delenv("VRG_APP_ID", raising=False)
    with (
        patch(f"{_MOD}.github.current_repo", return_value="owner/repo"),
        patch(f"{_MOD}.github.head_sha", return_value="abc123"),
        patch(f"{_MOD}.github.post_check_run") as mock_post,
    ):
        result = main([_PR])
    assert result == 0
    mock_post.assert_called_once()
