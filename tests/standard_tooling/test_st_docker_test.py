"""Tests for standard_tooling.bin.st_docker_test."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from standard_tooling.bin.st_docker_test import (
    _detect_language,
    _docker_is_available,
    build_test_docker_args,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path


# -- _detect_language ---------------------------------------------------------


def test_detect_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert _detect_language(tmp_path) == "python"


def test_detect_ruby(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    assert _detect_language(tmp_path) == "ruby"


def test_detect_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    assert _detect_language(tmp_path) == "go"


def test_detect_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert _detect_language(tmp_path) == "rust"


def test_detect_java_pom(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>\n")
    assert _detect_language(tmp_path) == "java"


def test_detect_java_mvnw(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/bash\n")
    assert _detect_language(tmp_path) == "java"


def test_detect_none(tmp_path: Path) -> None:
    assert _detect_language(tmp_path) == ""


# -- _detect_language priority: ruby wins over python -------------------------


def test_detect_ruby_priority(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("")
    (tmp_path / "pyproject.toml").write_text("")
    assert _detect_language(tmp_path) == "ruby"


# -- build_test_docker_args ---------------------------------------------------


def test_build_docker_args_python(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_test_docker_args(tmp_path, "python")
    assert "ghcr.io/wphillipmoore/dev-python:3.14" in args
    assert "uv sync && uv run pytest tests/ -v" in args
    assert "-v" in args


def test_build_docker_args_custom_env(tmp_path: Path) -> None:
    env = {
        "DOCKER_DEV_IMAGE": "custom:img",
        "DOCKER_TEST_CMD": "echo ok",
        "DOCKER_NETWORK": "testnet",
        "DOCKER_EXTRA_VOLUMES": "/a:/b:ro;/c:/d",
    }
    with patch.dict("os.environ", env, clear=True):
        args = build_test_docker_args(tmp_path, "")
    assert "custom:img" in args
    assert "echo ok" in args
    assert "--network" in args
    assert "testnet" in args
    assert "/a:/b:ro" in args
    assert "/c:/d" in args


def test_build_docker_args_mq_env(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "MQ_PORT": "1414"}
    with patch.dict("os.environ", env, clear=True):
        args = build_test_docker_args(tmp_path, "python")
    assert "-e" in args
    assert "MQ_HOST" in args
    assert "MQ_PORT" in args


def test_build_docker_args_no_image(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(SystemExit),
    ):
        build_test_docker_args(tmp_path, "")


def test_build_docker_args_no_command(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {"DOCKER_DEV_IMAGE": "img:1"}, clear=True),
        pytest.raises(SystemExit),
    ):
        build_test_docker_args(tmp_path, "")


def test_build_docker_args_empty_extra_volumes(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {"DOCKER_EXTRA_VOLUMES": ";"}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_test_docker_args(tmp_path, "python")
    # Should not have extra -v entries beyond the workspace mount
    v_indices = [i for i, a in enumerate(args) if a == "-v"]
    assert len(v_indices) == 1


# -- main --------------------------------------------------------------------


def test_main_no_language_no_env(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_docker_test.git.repo_root", return_value=tmp_path),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert main() == 1


def test_main_docker_not_available(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("standard_tooling.bin.st_docker_test.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_test._docker_is_available", return_value=False),
        patch("standard_tooling.bin.st_docker_test.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        result = main()
    assert result == 1
    mock_exec.assert_not_called()


def test_main_calls_execvp(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("standard_tooling.bin.st_docker_test.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_test._docker_is_available", return_value=True),
        patch("standard_tooling.bin.st_docker_test.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main()
    mock_exec.assert_called_once()
    call_args = mock_exec.call_args
    assert call_args[0][0] == "docker"
    assert "ghcr.io/wphillipmoore/dev-python:3.14" in call_args[0][1]


# -- _docker_is_available ----------------------------------------------------


def test_docker_is_available_true() -> None:
    mock_result = MagicMock(returncode=0)
    with patch("standard_tooling.bin.st_docker_test.subprocess.run", return_value=mock_result):
        assert _docker_is_available() is True


def test_docker_is_available_false() -> None:
    mock_result = MagicMock(returncode=1)
    with patch("standard_tooling.bin.st_docker_test.subprocess.run", return_value=mock_result):
        assert _docker_is_available() is False


def test_docker_is_available_not_installed() -> None:
    with patch("standard_tooling.bin.st_docker_test.subprocess.run", side_effect=FileNotFoundError):
        assert _docker_is_available() is False


def test_docker_is_available_timeout() -> None:
    with patch(
        "standard_tooling.bin.st_docker_test.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker version", timeout=15),
    ):
        assert _docker_is_available() is False
