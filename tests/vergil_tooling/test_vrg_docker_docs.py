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


def test_cli_prefix_used(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["--prefix", "dev", "serve"])
    args = mock_exec.call_args[0][1]
    assert "ghcr.io/vergil-project/dev-base:latest" in args


def test_invalid_prefix() -> None:
    assert main(["--prefix", "staging", "serve"]) == 1


def test_prefix_missing_value() -> None:
    assert main(["--prefix"]) == 1


def test_build_delegates_to_build_docker_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])

    call_args = mock_build.call_args
    assert call_args[0][0] == tmp_path
    assert call_args[0][1] == image
    assert call_args[0][2] == ["bash", "-c", "mkdocs build -f docs/site/mkdocs.yml"]


def test_serve_splices_port_before_image(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])

    args = mock_exec.call_args[0][1]
    image_idx = args.index(image)
    p_idx = args.index("-p")
    assert p_idx < image_idx
    assert args[p_idx + 1] == "8000:8000"


def test_serve_custom_port(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {"DOCS_PORT": "9000"}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])

    args = mock_exec.call_args[0][1]
    assert "9000:8000" in args


def test_build_no_port_splice(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])

    args = mock_exec.call_args[0][1]
    assert "-p" not in args


def test_python_repo_uv_sync_in_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])

    cmd = mock_build.call_args[0][2]
    assert cmd[0] == "bash"
    assert cmd[1] == "-c"
    assert "uv sync --group docs && uv run" in cmd[2]


def test_prefix_passed_to_image(tmp_path: Path) -> None:
    dev_image = "ghcr.io/vergil-project/dev-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_docker_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_docker_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_docker_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", dev_image, "bash", "-c", "x"]
        main(["--prefix", "dev", "serve"])

    assert mock_build.call_args[0][1] == dev_image
