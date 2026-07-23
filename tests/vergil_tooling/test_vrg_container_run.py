"""Tests for vergil_tooling.bin.vrg_container_run."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_container_run import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


# -- help output --------------------------------------------------------------


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: vrg-container-run" in out
    assert "GH_TOKEN" not in out


def test_h_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-h"]) == 0
    assert "usage: vrg-container-run" in capsys.readouterr().out


def test_help_after_separator_not_intercepted(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image") as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
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
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image") as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "img:1"
        main(["--", "--help"])
    args = mock_exec.call_args[0][1]
    assert args[-1] == "--help"


# -- argument parsing ---------------------------------------------------------


def test_no_args() -> None:
    with patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"):
        assert main([]) == 1


def test_no_command_after_separator() -> None:
    with patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"):
        assert main(["--"]) == 1


# -- GH_TOKEN assertion -------------------------------------------------------


def test_launches_without_gh_token(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image") as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_cache.return_value = "ghcr.io/vergil-project/prod-python:3.14"
        main(["--", "uv", "run", "vrg-validate"])
    mock_exec.assert_called_once()


# -- image selection ----------------------------------------------------------


def test_fallback_image_no_language(tmp_path: Path) -> None:
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image") as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "ghcr.io/vergil-project/prod-base:latest"
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert "ghcr.io/vergil-project/prod-base:latest" in args


def test_language_detected_image(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert "ghcr.io/vergil-project/prod-python:3.14" in args


def test_cli_prefix_used(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image") as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "ghcr.io/vergil-project/dev-python:3.14"
        main(["--prefix", "dev", "--", "echo", "hi"])
    mock_cache.assert_called_once()
    assert mock_cache.call_args[0][2] == "ghcr.io/vergil-project/dev-python:3.14"


def test_invalid_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"):
        assert main(["--prefix", "staging", "--", "echo"]) == 1
    assert "invalid prefix" in capsys.readouterr().err


def test_prefix_missing_value(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"):
        assert main(["--prefix"]) == 1
    assert "--prefix requires a value" in capsys.readouterr().err


def test_env_image_override(tmp_path: Path) -> None:
    env = {"GH_TOKEN": "tok", "DOCKER_DEV_IMAGE": "custom:img"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
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
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "vrg-prepare-release", "--issue", "42"])
    args = mock_exec.call_args[0][1]
    assert args[-3:] == ["vrg-prepare-release", "--issue", "42"]


def test_command_without_separator(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
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
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    assert "Network:  mynet" in capsys.readouterr().out


# -- argv=None uses sys.argv --------------------------------------------------


def test_argv_none_uses_sys_argv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch(
            "vergil_tooling.bin.vrg_container_run.sys.argv",
            ["vrg-container-run", "--", "echo"],
        ),
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
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0] == "docker"


def test_calls_execvp_nerdctl(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="nerdctl"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0] == "nerdctl"


# -- cache-aware image selection ----------------------------------------------


def test_non_python_uses_cached_image(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    cached = "ghcr.io/vergil-project/dev-go:1.26--feature-42--abcd1234"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "vrg-validate"])
    args = mock_exec.call_args[0][1]
    assert cached in args
    assert args[-1] == "vrg-validate"


def test_non_python_command_not_wrapped(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch(
            "vergil_tooling.bin.vrg_container_run.ensure_cached_image",
            return_value="ghcr.io/vergil-project/dev-go:1.26",
        ),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert args[-2:] == ["echo", "hi"]


def test_cached_image_diagnostic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    cached = "ghcr.io/vergil-project/dev-go:1.26--feature-42--abcd1234"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    out = capsys.readouterr().out
    assert "(cached)" in out


# -- pull policy integration --------------------------------------------------


def test_cached_image_uses_pull_never(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    cached = "ghcr.io/vergil-project/dev-go:1.26--feature-42--abcd1234"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    args = mock_exec.call_args[0][1]
    assert "--pull=always" not in args


def test_registry_image_uses_pull_always(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    base = "ghcr.io/vergil-project/prod-go:1.26"
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value=base),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    args = mock_exec.call_args[0][1]
    assert "--pull=always" in args


# -- [ci].versions authoritative container selection (issue #2468) ------------


def test_declared_ci_version_selects_container(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A repo declaring [ci].versions must drive the container version — not the
    # hardcoded _DEFAULT_VERSIONS — and must not warn.
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.primary_ci_version", return_value="3.12"),
        patch(
            "vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="cached:img"
        ) as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    # ensure_cached_image(repo_root, lang, base, ...) — the base carries 3.12.
    assert mock_cache.call_args[0][2] == "ghcr.io/vergil-project/prod-python:3.12"
    assert "WARNING" not in capsys.readouterr().err


def test_no_declared_version_warns_and_uses_builtin_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No declared version: fall back to the built-in default, but say so loudly.
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.primary_ci_version", return_value=None),
        patch(
            "vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="cached:img"
        ) as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "cmd"])
    assert mock_cache.call_args[0][2] == "ghcr.io/vergil-project/prod-python:3.14"
    err = capsys.readouterr().err
    assert "no [ci].versions" in err


# -- validation-command override ([validation] in vergil.toml) ----------------


def test_validate_command_rewritten_by_override(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="img:1"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_run.validation_container_command",
            return_value="uv run vrg-validate",
        ),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "vrg-validate"])
    args = mock_exec.call_args[0][1]
    assert args[-3:] == ["uv", "run", "vrg-validate"]


def test_validate_override_preserves_trailing_args(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="img:1"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_run.validation_container_command",
            return_value="uv run vrg-validate",
        ),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "vrg-validate", "--check", "common"])
    args = mock_exec.call_args[0][1]
    assert args[-5:] == ["uv", "run", "vrg-validate", "--check", "common"]


def test_validate_override_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="img:1"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_run.validation_container_command",
            return_value="uv run vrg-validate",
        ),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "vrg-validate"])
    assert "Command:  uv run vrg-validate" in capsys.readouterr().out


def test_non_validate_command_not_rewritten(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="img:1"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_run.validation_container_command",
            return_value="uv run vrg-validate",
        ) as mock_override,
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "echo", "hi"])
    args = mock_exec.call_args[0][1]
    assert args[-2:] == ["echo", "hi"]
    mock_override.assert_not_called()


def test_validate_default_no_override_passthrough(tmp_path: Path) -> None:
    # No vergil.toml in tmp_path: validation_container_command falls back to the
    # default "vrg-validate", so the command is unchanged (real-helper path).
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="img:1"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "vrg-validate"])
    args = mock_exec.call_args[0][1]
    assert args[-1] == "vrg-validate"


def test_python_routes_through_cache(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image") as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        mock_cache.return_value = "ghcr.io/vergil-project/dev-python:3.14--develop--aabbccdd"
        main(["--", "uv", "run", "pytest"])
    mock_cache.assert_called_once()
