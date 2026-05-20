"""Tests for vergil_tooling.bin.vrg_docker_docs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from vergil_tooling.bin.vrg_docker_docs import main


def test_no_args() -> None:
    assert main([]) == 1


def test_unknown_command() -> None:
    with patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=Path("/repo")):
        assert main(["unknown"]) == 1


def test_serve_execvp(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["serve"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert any(a.startswith("--platform=linux/") for a in args)
    assert "-p" in args
    assert "8000:8000" in args
    assert "mkdocs serve" in args[-1]


def test_build_execvp(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert "-p" not in args
    assert "mkdocs build" in args[-1]


def test_serve_with_extra_args(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["serve", "--strict"])
    container_cmd = mock_exec.call_args[0][1][-1]
    assert "--strict" in container_cmd


def test_custom_env_vars(tmp_path: Path) -> None:
    env = {
        "DOCKER_DOCS_IMAGE": "my-docs:1",
        "MKDOCS_CONFIG": "custom.yml",
        "DOCS_PORT": "9000",
    }
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["serve"])
    args = mock_exec.call_args[0][1]
    assert "my-docs:1" in args
    assert "9000:8000" in args
    assert "custom.yml" in args[-1]


def test_python_repo_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])
    container_cmd = mock_exec.call_args[0][1][-1]
    assert "uv sync --group docs && uv run" in container_cmd


def test_non_python_repo_no_uv(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])
    container_cmd = mock_exec.call_args[0][1][-1]
    assert "uv" not in container_cmd


def test_common_sibling_mount(tmp_path: Path) -> None:
    common = tmp_path / ".." / "mq-rest-admin-common"
    common.mkdir(parents=True)
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])
    args = mock_exec.call_args[0][1]
    v_indices = [i for i, a in enumerate(args) if a == "-v"]
    assert len(v_indices) == 2
    mount_arg = args[v_indices[1] + 1]
    assert ".mq-rest-admin-common:ro" in mount_arg
