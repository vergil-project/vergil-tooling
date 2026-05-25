"""Tests for vergil_tooling.bin.vrg_container_docs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from vergil_tooling.bin.vrg_container_docs import main


def test_no_args() -> None:
    assert main([]) == 1


def test_unknown_command() -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=Path("/repo")),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
    ):
        assert main(["unknown"]) == 1


def test_serve_execvp(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert args[0] == "docker"
    assert "-p" in args
    assert "8000:8000" in args
    assert "mkdocs serve" in args[-1]


def test_serve_execvp_nerdctl(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="nerdctl"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "nerdctl",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0] == "nerdctl"


def test_build_execvp(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert "-p" not in args
    assert "mkdocs build" in args[-1]


def test_serve_with_extra_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["serve", "--strict"])
    cmd_passed = mock_build.call_args[0][2]
    assert "--strict" in cmd_passed[2]


def test_custom_env_vars(tmp_path: Path) -> None:
    env = {
        "DOCKER_DOCS_IMAGE": "my-docs:1",
        "MKDOCS_CONFIG": "custom.yml",
        "DOCS_PORT": "9000",
    }
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            "my-docs:1",
            "bash",
            "-c",
            "placeholder",
        ]
        main(["serve"])
    assert mock_build.call_args[0][1] == "my-docs:1"
    cmd = mock_build.call_args[0][2]
    assert "custom.yml" in cmd[2]
    args = mock_exec.call_args[0][1]
    assert "9000:8000" in args


def test_python_repo_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])
    cmd = mock_build.call_args[0][2]
    assert "uv sync --group docs && uv run" in cmd[2]


def test_non_python_repo_no_uv(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])
    cmd = mock_build.call_args[0][2]
    assert "uv" not in cmd[2]


def test_cli_prefix_used(tmp_path: Path) -> None:
    dev_image = "ghcr.io/vergil-project/dev-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", dev_image, "bash", "-c", "x"]
        main(["--prefix", "dev", "serve"])
    assert mock_build.call_args[0][1] == dev_image


def test_invalid_prefix() -> None:
    assert main(["--prefix", "staging", "serve"]) == 1


def test_prefix_missing_value() -> None:
    assert main(["--prefix"]) == 1


def test_build_delegates_to_build_container_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
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
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
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
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {"DOCS_PORT": "9000"}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])

    args = mock_exec.call_args[0][1]
    assert "9000:8000" in args


def test_build_no_port_splice(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])

    args = mock_exec.call_args[0][1]
    assert "-p" not in args


def test_python_repo_uv_sync_in_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
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
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", dev_image, "bash", "-c", "x"]
        main(["--prefix", "dev", "serve"])

    assert mock_build.call_args[0][1] == dev_image


def test_env_prefixes_passed_to_build_container_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch(
            "vergil_tooling.bin.vrg_container_docs.container_env_prefixes",
            return_value=["MQ_"],
        ),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])
    assert mock_build.call_args[1]["env_prefixes"] == ["MQ_"]
