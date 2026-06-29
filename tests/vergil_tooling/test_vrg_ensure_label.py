"""Tests for vergil_tooling.bin.vrg_ensure_label."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_ensure_label import main, parse_args

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_parse_args_single_label() -> None:
    args = parse_args(["--repo", "owner/repo", "--label", "bug"])
    assert args.repo == "owner/repo"
    assert args.label == "bug"
    assert args.sync is False


def test_parse_args_single_with_color_description() -> None:
    args = parse_args(
        [
            "--repo",
            "owner/repo",
            "--label",
            "feature",
            "--color",
            "0e8a16",
            "--description",
            "New feature",
        ]
    )
    assert args.color == "0e8a16"
    assert args.description == "New feature"


def test_parse_args_sync_mode() -> None:
    args = parse_args(["--repo", "owner/repo", "--sync"])
    assert args.sync is True
    assert args.repo == "owner/repo"


def test_parse_args_project_mode() -> None:
    args = parse_args(["--owner", "myorg", "--project", "3", "--sync"])
    assert args.owner == "myorg"
    assert args.project == "3"
    assert args.sync is True


def test_parse_args_project_without_sync_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--owner", "myorg", "--project", "3"])


def test_parse_args_sync_without_repo_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--sync"])


def test_parse_args_no_args_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


# ---------------------------------------------------------------------------
# Single-label mode
# ---------------------------------------------------------------------------


def test_main_single_label() -> None:
    with patch("vergil_tooling.bin.vrg_ensure_label.github.run") as mock_run:
        result = main(["--repo", "o/r", "--label", "bug"])
    assert result == 0
    mock_run.assert_called_once_with(
        "label",
        "create",
        "bug",
        "--repo",
        "o/r",
        "--force",
    )


def test_main_single_label_with_color_description() -> None:
    with patch("vergil_tooling.bin.vrg_ensure_label.github.run") as mock_run:
        result = main(
            [
                "--repo",
                "o/r",
                "--label",
                "feature",
                "--color",
                "0e8a16",
                "--description",
                "New feature",
            ]
        )
    assert result == 0
    mock_run.assert_called_once_with(
        "label",
        "create",
        "feature",
        "--repo",
        "o/r",
        "--force",
        "--color",
        "0e8a16",
        "--description",
        "New feature",
    )


# ---------------------------------------------------------------------------
# Sync mode
# ---------------------------------------------------------------------------


def test_main_sync_provisions_all_labels() -> None:
    with patch("vergil_tooling.bin.vrg_ensure_label.github.run") as mock_run:
        result = main(["--repo", "o/r", "--sync"])
    assert result == 0
    # Should have called once per label + once for the delete
    label_calls = [c for c in mock_run.call_args_list if c.args[1] == "create"]
    assert len(label_calls) == 15  # 10 base + 5 epic/task convention labels


def test_main_sync_uses_force_with_color_description() -> None:
    with patch("vergil_tooling.bin.vrg_ensure_label.github.run") as mock_run:
        main(["--repo", "o/r", "--sync"])
    # Check one representative call has --force, --color, --description
    first_create = next(c for c in mock_run.call_args_list if c.args[1] == "create")
    assert "--force" in first_create.args
    assert "--color" in first_create.args
    assert "--description" in first_create.args


def test_main_sync_deletes_deprecated_labels() -> None:
    with patch("vergil_tooling.bin.vrg_ensure_label.github.run") as mock_run:
        main(["--repo", "o/r", "--sync"])
    delete_calls = [c for c in mock_run.call_args_list if c.args[1] == "delete"]
    assert len(delete_calls) == 1
    assert "enhancement" in delete_calls[0].args


def test_main_sync_delete_ignores_missing_label() -> None:
    """Deleting a non-existent label should not raise."""

    def side_effect(*args: str) -> None:
        if args[1] == "delete":
            raise RuntimeError("label not found")

    with patch("vergil_tooling.bin.vrg_ensure_label.github.run", side_effect=side_effect):
        result = main(["--repo", "o/r", "--sync"])
    assert result == 0


# ---------------------------------------------------------------------------
# Project mode
# ---------------------------------------------------------------------------


def test_main_project_mode_discovers_and_syncs() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_ensure_label.list_project_repos",
            return_value=["owner/a", "owner/b"],
        ) as mock_discover,
        patch("vergil_tooling.bin.vrg_ensure_label.github.run"),
    ):
        result = main(["--owner", "myorg", "--project", "3", "--sync"])
    assert result == 0
    mock_discover.assert_called_once_with("myorg", "3")
