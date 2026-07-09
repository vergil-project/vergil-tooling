"""Tests for vergil_tooling.bin.vrg_epic_create."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_epic_create import main, parse_args
from vergil_tooling.lib import github

_MOD = "vergil_tooling.bin.vrg_epic_create"
_URL = "https://github.com/org/.github/issues/50"


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_default_target_is_current_repo_public_goes_to_dotgithub() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github") as home,
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "Epic: X", "--body", "B"])
    assert rc == 0
    home.assert_called_once_with("org", "tooling")
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["title"] == "Epic: X"
    assert mock_create.call_args.kwargs["labels"] == ["epic"]


def test_explicit_private_target_homes_in_self() -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab") as home,
        patch(f"{_MOD}.github.repo_visibility", return_value="PRIVATE"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--repo", "org/lab", "--title", "T"])
    assert rc == 0
    home.assert_called_once_with("org", "lab")
    assert mock_create.call_args.kwargs["repo"] == "org/lab"


def test_prints_resolved_home(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PRIVATE"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
    ):
        main(["--repo", "org/lab", "--title", "T"])
    assert "epic home: org/lab [PRIVATE]" in capsys.readouterr().out


def test_adds_extra_labels() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--label", "ad-hoc"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["epic", "ad-hoc"]


def test_dedups_epic_label() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--label", "epic"])
    assert mock_create.call_args.kwargs["labels"] == ["epic"]


def test_malformed_repo_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.github.create_issue") as mock_create:
        rc = main(["--repo", "noslash", "--title", "T"])
    assert rc == 1
    assert "owner/repo" in capsys.readouterr().err
    mock_create.assert_not_called()


def test_current_repo_error_propagates() -> None:
    # No --repo and current_repo fails (not in a repo) -> fail loud, not silent.
    with (
        patch(
            f"{_MOD}.github.current_repo",
            side_effect=github.GitHubAPIError(1, "gh repo view", "not a repo"),
        ),
        pytest.raises(github.GitHubAPIError),
    ):
        main(["--title", "T"])
