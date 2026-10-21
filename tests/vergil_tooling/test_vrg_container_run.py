"""Tests for vergil_tooling.bin.vrg_container_run."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.vergil_tooling.conftest import FIXTURE_WINDOW, window_default_resolution
from vergil_tooling.bin.vrg_container_run import _test_context, _test_tuning_env, main
from vergil_tooling.lib import container
from vergil_tooling.lib.platform_env import Platform
from vergil_tooling.lib.versions.window import WINDOW_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _host_platform() -> Iterator[None]:
    # Platform resolution probes the network and filesystem; pin it so every
    # run is hermetic. Tests that care override it explicitly.
    with patch(
        "vergil_tooling.bin.vrg_container_run.platform_env.current_platform",
        return_value=Platform.PHYSICAL_HOST,
    ):
        yield


@pytest.fixture(autouse=True)
def _marker_language_resolution() -> Iterator[None]:
    # Most tests drop only a language marker (no vergil.toml); resolve the
    # detected language to its newest fixture-window version. The resolution
    # tests below put the real resolver back.
    with patch(
        "vergil_tooling.bin.vrg_container_run.repo_resolution",
        side_effect=window_default_resolution,
    ):
        yield


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


def _write_python_config(root: Path, versions: str) -> None:
    (root / "vergil.toml").write_text(
        "[project]\n"
        'repository-type = "tooling"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        'primary-language = "python"\n'
        "[dependencies]\n"
        'vergil = "v2.1"\n'
        f"[ci]\nversions = {versions}\n"
    )


def _run_resolving(root: Path) -> tuple[int, MagicMock]:
    env = {"GH_TOKEN": "tok", WINDOW_ENV: str(FIXTURE_WINDOW)}
    with (
        patch(
            "vergil_tooling.bin.vrg_container_run.repo_resolution",
            side_effect=container.repo_resolution,
        ),
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=root),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch(
            "vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="cached:img"
        ) as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        rc = main(["--", "cmd"])
    return rc, mock_cache


def test_resolved_version_selects_container(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The resolver's primary version picks the container — the same resolution
    # CI uses, read from the window (epic vergil-project/.github#407).
    _write_python_config(tmp_path, '["3.12"]')
    _rc, mock_cache = _run_resolving(tmp_path)
    # ensure_cached_image(repo_root, lang, base, ...) — the base carries 3.12.
    assert mock_cache.call_args[0][2] == "ghcr.io/vergil-project/prod-python:3.12"
    assert "WARNING" not in capsys.readouterr().err


def test_pin_outside_window_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No fallback container: a pin the window does not build fails loudly.
    _write_python_config(tmp_path, '["3.11"]')
    rc, mock_cache = _run_resolving(tmp_path)
    assert rc == 1
    mock_cache.assert_not_called()
    err = capsys.readouterr().err
    assert "3.11" in err
    assert "DOCKER_DEV_IMAGE" in err


def test_resolution_warning_is_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # An app (repository-type tooling) pinning two versions warns while the
    # fleet sweep runs, and the run goes ahead on the newest.
    _write_python_config(tmp_path, '["3.12", "3.13"]')
    _rc, mock_cache = _run_resolving(tmp_path)
    assert mock_cache.call_args[0][2] == "ghcr.io/vergil-project/prod-python:3.13"
    assert "WARNING: [ci].versions pins 2 versions" in capsys.readouterr().err


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


# -- asserted primary-language drives image selection (issue #2858) -----------

_CPP_VERGIL_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "cpp"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["clang-20"]
"""


def test_asserted_cpp_language_selects_cpp_image_without_markers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The reported bug: a freshly bootstrapped C++ repo declares
    # primary-language = "cpp" but has no CMakeLists.txt/conanfile yet. Image
    # selection must key off the asserted language, not filesystem markers, so
    # the C++ image is chosen instead of the base image (whose missing toolchain
    # produced the misleading `FileNotFoundError: conan`).
    (tmp_path / "vergil.toml").write_text(_CPP_VERGIL_TOML)
    env = {"GH_TOKEN": "tok"}
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch(
            "vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="cached:img"
        ) as mock_cache,
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp"),
        patch.dict("os.environ", env, clear=True),
    ):
        main(["--", "echo", "hi"])
    # ensure_cached_image(repo_root, lang, base, ...) — lang and base both cpp.
    assert mock_cache.call_args[0][1] == "cpp"
    assert mock_cache.call_args[0][2] == "ghcr.io/vergil-project/prod-cpp-clang:20"
    assert "Language: cpp" in capsys.readouterr().out


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


# -- test-tuning env (issue #3322) ------------------------------------------


def test_test_context_explicit_passes_through() -> None:
    env = {"VRG_TEST_CONTEXT": "cloud-vm", "GITHUB_ACTIONS": "true"}
    with patch.dict("os.environ", env, clear=True):
        assert _test_context() == "cloud-vm"


def test_test_context_github_actions_is_ci() -> None:
    with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=True):
        assert _test_context() == "ci"


def test_test_context_falls_back_to_platform() -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(
            "vergil_tooling.bin.vrg_container_run.platform_env.current_platform",
            return_value=Platform.LOCAL_VM,
        ),
    ):
        assert _test_context() == "local-vm"


def test_test_tuning_env_forwards_workers_override() -> None:
    with patch.dict("os.environ", {"VRG_TEST_WORKERS": " 12 "}, clear=True):
        assert _test_tuning_env() == {
            "VRG_TEST_CONTEXT": "physical-host",
            "VRG_TEST_WORKERS": "12",
        }


def test_test_tuning_env_omits_unset_workers() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert _test_tuning_env() == {"VRG_TEST_CONTEXT": "physical-host"}


def test_main_passes_test_tuning_env_to_container(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="img"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}, clear=True),
    ):
        main(["--", "vrg-validate"])
    assert "VRG_TEST_CONTEXT=ci" in mock_exec.call_args[0][1]


# -- Conan audit token injection (issue #2902) ----------------------------------

_TOKEN_ENV = "CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER"  # noqa: S105


def _run_cpp(
    tmp_path: Path, env: dict[str, str], conan_env: str | None = None
) -> tuple[int, list[str], dict[str, str]]:
    """Run main() for a cpp repo; return (rc, argv, environ seen at exec time)."""
    (tmp_path / "vergil.toml").write_text(_CPP_VERGIL_TOML)
    home = tmp_path / "home"
    home.mkdir()
    env_file = home / "conan.env"
    if conan_env is not None:
        env_file.write_text(conan_env)
    seen: dict[str, str] = {}
    argv: list[str] = []

    def _exec(_prog: str, args: list[str]) -> None:
        argv.extend(args)
        seen.update(os.environ)

    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch(
            "vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="cached:img"
        ),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp", side_effect=_exec),
        patch("vergil_tooling.lib.conan_token.conan_env_path", return_value=env_file),
        patch.dict("os.environ", env, clear=True),
    ):
        rc = main(["--", "vrg-validate"])
    return rc, argv, seen


def test_cpp_injects_token_from_conan_env_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No token in the ambient env (a zsh or non-interactive shell): the token is
    # read from conan.env and handed to the runtime through its environment.
    rc, argv, seen = _run_cpp(
        tmp_path, {}, conan_env=f"export {_TOKEN_ENV}=placeholder-from-file\n"
    )
    assert rc == 0
    assert argv[argv.index(_TOKEN_ENV) - 1] == "-e"
    assert not any("placeholder-from-file" in a for a in argv)
    assert seen[_TOKEN_ENV] == "placeholder-from-file"
    out = capsys.readouterr().out
    assert "conan.env" in out
    assert "placeholder-from-file" not in out


def test_cpp_explicit_env_token_wins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, argv, seen = _run_cpp(
        tmp_path,
        {_TOKEN_ENV: "placeholder-from-env"},
        conan_env=f"export {_TOKEN_ENV}=placeholder-from-file\n",
    )
    assert rc == 0
    assert _TOKEN_ENV in argv
    assert seen[_TOKEN_ENV] == "placeholder-from-env"
    assert "environment" in capsys.readouterr().out


def test_cpp_without_token_injects_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Absent everywhere: keep the #2895 behaviour (vrg-validate skips the scan).
    rc, argv, seen = _run_cpp(tmp_path, {})
    assert rc == 0
    assert _TOKEN_ENV not in argv
    assert _TOKEN_ENV not in seen
    assert "no audit token" in capsys.readouterr().out


def test_cpp_malformed_conan_env_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, argv, _ = _run_cpp(tmp_path, {}, conan_env="export do-not-print-me\n")
    assert rc == 1
    assert argv == []
    err = capsys.readouterr().err
    assert "Conan audit token" in err
    assert "conan.env" in err
    assert "do-not-print-me" not in err


def test_non_cpp_repo_never_resolves_token(tmp_path: Path) -> None:
    # The token is only consumed by the cpp audit; other repos never read the
    # files, so a broken token file cannot fail them.
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("vergil_tooling.bin.vrg_container_run.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_run.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_run.ensure_cached_image", return_value="img"),
        patch("vergil_tooling.bin.vrg_container_run.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_run.os.execvp") as mock_exec,
        patch("vergil_tooling.lib.conan_token.resolve_conan_audit_token") as mock_resolve,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["--", "echo", "hi"])
    mock_resolve.assert_not_called()
    assert _TOKEN_ENV not in mock_exec.call_args[0][1]
