"""Tests for vergil_tooling.bin.vrg_pr_fix_body."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_pr_fix_body import main, parse_args
from vergil_tooling.lib import identity_mode

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_pr_fix_body"

_PR = "https://github.com/owner/repo/pull/7"
_ARGS = [_PR, "--issue", "42", "--summary", "Corrected summary"]

_EXPECTED_BODY = (
    "# Pull Request\n\n"
    "## Summary\n\n- Corrected summary\n\n"
    "## Issue Linkage\n\n- Ref #42\n\n"
    "## Notes\n\n- -"
)


@pytest.fixture(autouse=True)
def _as_user() -> Iterator[None]:
    """Default every test to the USER identity on the PR's own branch.

    Individual tests override by patching deeper — the innermost
    patch wins.
    """
    with (
        patch(
            _MOD + ".identity_mode.current_mode",
            return_value=identity_mode.IdentityMode.USER,
        ),
        patch(_MOD + ".github.head_ref", return_value="feature/42-fix"),
        patch(_MOD + ".git.current_branch", return_value="feature/42-fix"),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
    ):
        yield


# -- argument parsing ---------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = parse_args(_ARGS)
    assert args.pr == _PR
    assert args.issue == "42"
    assert args.summary == "Corrected summary"
    assert args.linkage == "Ref"
    assert args.notes == ""
    assert args.dry_run is False
    assert args.no_retrigger is False


def test_parse_args_rejects_autoclose_linkage() -> None:
    with pytest.raises(SystemExit):
        parse_args([*_ARGS, "--linkage", "Closes"])


# -- identity gating ----------------------------------------------------------


def test_audit_identity_denied(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        _MOD + ".identity_mode.current_mode",
        return_value=identity_mode.IdentityMode.AUDIT,
    ):
        assert main(_ARGS) == 1
    assert "audit" in capsys.readouterr().err.lower()


def test_user_branch_mismatch_denied(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(_MOD + ".github.head_ref", return_value="feature/42-fix"),
        patch(_MOD + ".git.current_branch", return_value="feature/99-other"),
    ):
        assert main(_ARGS) == 1
    err = capsys.readouterr().err
    assert "feature/42-fix" in err
    assert "feature/99-other" in err


def test_human_skips_branch_scope_check() -> None:
    with (
        patch(
            _MOD + ".identity_mode.current_mode",
            return_value=identity_mode.IdentityMode.HUMAN,
        ),
        patch(_MOD + ".github.head_ref") as mock_head,
        patch(_MOD + ".github.edit_pr_body"),
        patch(_MOD + ".git.run"),
    ):
        assert main(_ARGS) == 0
    mock_head.assert_not_called()


# -- state gating -------------------------------------------------------------


def test_non_open_pr_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_MOD + ".github.pr_state", return_value="MERGED"):
        assert main(_ARGS) == 1
    assert "MERGED" in capsys.readouterr().err


# -- body construction and edit -----------------------------------------------


def test_edits_body_through_validated_builder() -> None:
    with (
        patch(_MOD + ".github.edit_pr_body") as mock_edit,
        patch(_MOD + ".git.run"),
    ):
        assert main(_ARGS) == 0
    mock_edit.assert_called_once_with(_PR, body=_EXPECTED_BODY)


def test_notes_included_in_body() -> None:
    with (
        patch(_MOD + ".github.edit_pr_body") as mock_edit,
        patch(_MOD + ".git.run"),
    ):
        assert main([*_ARGS, "--notes", "Body-only fix"]) == 0
    body = mock_edit.call_args.kwargs["body"]
    assert "## Notes\n\n- Body-only fix" in body


def test_invalid_issue_ref_exits() -> None:
    with pytest.raises(SystemExit, match="must be a number"):
        main([_PR, "--issue", "bad-ref", "--summary", "s"])


# -- CI re-trigger ------------------------------------------------------------


def test_retrigger_pushes_empty_commit_after_edit() -> None:
    calls: list[str] = []
    with (
        patch(
            _MOD + ".github.edit_pr_body",
            side_effect=lambda *a, **k: calls.append("edit"),
        ),
        patch(
            _MOD + ".git.run",
            side_effect=lambda *args: calls.append(" ".join(args)),
        ),
    ):
        assert main(_ARGS) == 0
    assert calls[0] == "edit"
    assert any(c.startswith("commit --allow-empty") for c in calls[1:])
    assert calls[-1] == "push"


def test_no_retrigger_skips_git_operations() -> None:
    with (
        patch(_MOD + ".github.edit_pr_body"),
        patch(_MOD + ".git.run") as mock_git,
    ):
        assert main([*_ARGS, "--no-retrigger"]) == 0
    mock_git.assert_not_called()


# -- dry run ------------------------------------------------------------------


def test_dry_run_prints_body_without_side_effects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(_MOD + ".github.edit_pr_body") as mock_edit,
        patch(_MOD + ".github.pr_state") as mock_state,
        patch(_MOD + ".git.run") as mock_git,
    ):
        assert main([*_ARGS, "--dry-run"]) == 0
    assert _EXPECTED_BODY in capsys.readouterr().out
    mock_edit.assert_not_called()
    mock_state.assert_not_called()
    mock_git.assert_not_called()
