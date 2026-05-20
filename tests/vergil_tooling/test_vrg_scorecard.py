"""Tests for vergil_tooling.bin.vrg_scorecard."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_scorecard import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: vrg-scorecard" in out
    assert "scorecard" in out.lower()


def test_h_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-h"]) == 0
    assert "usage: vrg-scorecard" in capsys.readouterr().out


# -- token injection and docker exec ------------------------------------------


def test_injects_token_and_calls_execvp(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_scorecard.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_scorecard._human_token", return_value="test-token-123"),
        patch("vergil_tooling.bin.vrg_scorecard.assert_docker_available"),
        patch("vergil_tooling.bin.vrg_scorecard.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_scorecard.os.execvp") as mock_exec,
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            "ghcr.io/vergil-project/prod-base:latest",
            "scorecard",
            "--repo=github.com/org/repo",
        ]
        main(["--repo=github.com/org/repo"])

    args = mock_exec.call_args[0][1]
    assert mock_exec.call_args[0][0] == "docker"
    token_idx = args.index("-e")
    assert args[token_idx + 1] == "GH_TOKEN=test-token-123"
    image_idx = args.index("ghcr.io/vergil-project/prod-base:latest")
    assert token_idx < image_idx
    assert args[-2:] == ["scorecard", "--repo=github.com/org/repo"]


def test_build_docker_args_receives_correct_image_and_command(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_scorecard.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_scorecard._human_token", return_value="tok"),
        patch("vergil_tooling.bin.vrg_scorecard.assert_docker_available"),
        patch("vergil_tooling.bin.vrg_scorecard.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_scorecard.os.execvp"),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "scorecard",
            "--repo=github.com/org/repo",
            "--format=json",
        ]
        main(["--repo=github.com/org/repo", "--format=json"])

    call_args = mock_build.call_args
    assert call_args[0][0] == tmp_path
    assert call_args[0][1] == image
    assert call_args[0][2] == ["scorecard", "--repo=github.com/org/repo", "--format=json"]


def test_no_args_still_runs_scorecard(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_scorecard.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_scorecard._human_token", return_value="tok"),
        patch("vergil_tooling.bin.vrg_scorecard.assert_docker_available"),
        patch("vergil_tooling.bin.vrg_scorecard.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_scorecard.os.execvp"),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "scorecard"]
        main([])

    assert mock_build.call_args[0][2] == ["scorecard"]


# -- error handling -----------------------------------------------------------


def test_human_token_failure_propagates(tmp_path: Path) -> None:
    import pytest as _pytest

    with (
        patch("vergil_tooling.bin.vrg_scorecard.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_scorecard._human_token", side_effect=SystemExit(1)),
        _pytest.raises(SystemExit, match="1"),
    ):
        main([])


# -- argv=None default --------------------------------------------------------


def test_argv_none_uses_sys_argv(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_scorecard.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_scorecard._human_token", return_value="tok"),
        patch("vergil_tooling.bin.vrg_scorecard.assert_docker_available"),
        patch("vergil_tooling.bin.vrg_scorecard.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_scorecard.os.execvp"),
        patch(
            "vergil_tooling.bin.vrg_scorecard.sys.argv",
            ["vrg-scorecard", "--repo=github.com/org/repo"],
        ),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "scorecard"]
        main()

    assert mock_build.call_args[0][2] == ["scorecard", "--repo=github.com/org/repo"]
