"""Tests for standard_tooling.bin.docker_run."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from standard_tooling.bin.docker_run import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# -- help output --------------------------------------------------------------


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: st-docker-run" in out
    assert "GH_TOKEN" in out


def test_h_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-h"]) == 0
    assert "usage: st-docker-run" in capsys.readouterr().out


def test_help_after_separator_not_intercepted(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image") as mock_cache,
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "img:1"
        main(["--", "some-tool", "--help"])
    args = mock_exec.call_args[0][1]
    assert args[-2:] == ["some-tool", "--help"]


def test_help_alone_after_separator_not_intercepted(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image") as mock_cache,
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "img:1"
        main(["--", "--help"])
    args = mock_exec.call_args[0][1]
    assert args[-1] == "--help"


# -- argument parsing ---------------------------------------------------------


def test_no_args() -> None:
    assert main([]) == 1


def test_no_command_after_separator() -> None:
    assert main(["--"]) == 1


# -- GH_TOKEN assertion -------------------------------------------------------


def test_missing_gh_token(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert main(["--", "echo", "hi"]) == 1


# -- image selection ----------------------------------------------------------


def test_fallback_image_no_language(tmp_path: Path) -> None:
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image") as mock_cache,
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "ghcr.io/wphillipmoore/dev-base:latest"
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert "ghcr.io/wphillipmoore/dev-base:latest" in args


def test_language_detected_image(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert "ghcr.io/wphillipmoore/dev-python:3.14" in args


def test_env_image_override(tmp_path: Path) -> None:
    env = {"GH_TOKEN": "tok", "DOCKER_DEV_IMAGE": "custom:img"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert "custom:img" in args


# -- command passthrough ------------------------------------------------------


def test_command_after_separator(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "st-prepare-release", "--issue", "42"])
    args = mock_exec.call_args[0][1]
    assert args[-3:] == ["st-prepare-release", "--issue", "42"]


def test_command_without_separator(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert args[-2:] == ["echo", "hi"]


# -- network output -----------------------------------------------------------


def test_network_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok", "DOCKER_NETWORK": "mynet"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    assert "Network:  mynet" in capsys.readouterr().out


# -- argv=None uses sys.argv --------------------------------------------------


def test_argv_none_uses_sys_argv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch("standard_tooling.bin.docker_run.sys.argv", ["st-docker-run", "--", "echo"]),
        patch.dict("os.environ", env, clear=True),
    ):
        main()
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][1][-1] == "echo"


# -- execvp call --------------------------------------------------------------


def test_calls_execvp(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0] == "docker"


# -- cache-aware image selection ----------------------------------------------


def test_non_python_uses_cached_image(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    cached = "ghcr.io/wphillipmoore/dev-go:1.26--feature-42--abcd1234"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image", return_value=cached),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "st-validate"])
    args = mock_exec.call_args[0][1]
    assert cached in args
    assert args[-1] == "st-validate"


def test_non_python_command_not_wrapped(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch(
            "standard_tooling.bin.docker_run.ensure_cached_image",
            return_value="ghcr.io/wphillipmoore/dev-go:1.26",
        ),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert args[-2:] == ["echo", "hi"]


def test_cached_image_diagnostic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    cached = "ghcr.io/wphillipmoore/dev-go:1.26--feature-42--abcd1234"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image", return_value=cached),
        patch("standard_tooling.bin.docker_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    out = capsys.readouterr().out
    assert "(cached)" in out


# -- pull policy integration --------------------------------------------------


def test_cached_image_uses_pull_never(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    cached = "ghcr.io/wphillipmoore/dev-go:1.26--feature-42--abcd1234"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image", return_value=cached),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    args = mock_exec.call_args[0][1]
    assert "--pull=always" not in args


def test_registry_image_uses_pull_always(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    base = "ghcr.io/wphillipmoore/dev-go:1.26"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image", return_value=base),
        patch("standard_tooling.bin.docker_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    args = mock_exec.call_args[0][1]
    assert "--pull=always" in args


def test_python_routes_through_cache(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("standard_tooling.bin.docker_run.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.docker_run.assert_docker_available"),
        patch("standard_tooling.bin.docker_run.ensure_cached_image") as mock_cache,
        patch("standard_tooling.bin.docker_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "ghcr.io/wphillipmoore/dev-python:3.14--develop--aabbccdd"
        main(["--", "uv", "run", "pytest"])
    mock_cache.assert_called_once()
