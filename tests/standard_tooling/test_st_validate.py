"""Tests for standard_tooling.bin.st_validate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Iterator

import pytest

from standard_tooling.bin.st_validate import (
    _find_custom_validator,
    _in_dev_container,
    _run_commands,
    _run_common_checks,
    _run_custom_validator,
    main,
)
from standard_tooling.lib.validate_commands import CheckKind


@pytest.fixture(autouse=True)
def _container_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ST_IN_DEV_CONTAINER", "1")


def _write_config(tmp_path: Path, language: str) -> None:
    (tmp_path / "standard-tooling.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        f'primary-language = "{language}"\n\n[dependencies]\nstandard-tooling = "v1.4"\n'
    )


# -- Container guard ----------------------------------------------------------


def test_rejects_host_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ST_IN_DEV_CONTAINER", raising=False)
    with patch("standard_tooling.bin.st_validate._in_dev_container", return_value=False):
        assert main([]) == 1


# -- --check common -----------------------------------------------------------


def test_check_common_runs_common_checks(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", return_value=0) as mock,
    ):
        result = main(["--check", "common"])
    assert result == 0
    mock.assert_called_once()


# -- --check lint (language-specific) -----------------------------------------


def test_check_lint_runs_install_then_lint(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    calls: list[list[str]] = []

    def mock_run_commands(cmds: list[list[str]], label: str, **kwargs: bool) -> int:
        calls.extend(cmds)
        return 0

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_commands", side_effect=mock_run_commands),
    ):
        result = main(["--check", "lint"])
    assert result == 0
    joined = [" ".join(c) for c in calls]
    assert "uv sync --frozen --group dev" in joined
    assert "ruff check src/ tests/" in joined


def test_check_lint_no_commands_for_shell(tmp_path: Path) -> None:
    _write_config(tmp_path, "shell")
    with patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path):
        result = main(["--check", "lint"])
    assert result == 0


# -- No --check (run all) ----------------------------------------------------


def test_run_all_calls_common_then_language_checks(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    order: list[str] = []

    def mock_common(repo_root: Path) -> int:
        order.append("common")
        return 0

    def mock_commands(cmds: list[list[str]], label: str, **kwargs: bool) -> int:
        order.append(label)
        return 0

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", side_effect=mock_common),
        patch("standard_tooling.bin.st_validate._run_commands", side_effect=mock_commands),
        patch("standard_tooling.bin.st_validate._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 0
    assert order[0] == "common"
    assert "install" in order
    assert "lint" in order
    assert "typecheck" in order
    assert "test" in order
    assert "audit" in order


def test_run_all_stops_on_failure(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")

    def mock_common(repo_root: Path) -> int:
        return 1

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", side_effect=mock_common),
    ):
        result = main([])
    assert result == 1


def test_run_all_includes_custom_validator(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", return_value=0),
        patch("standard_tooling.bin.st_validate._run_commands", return_value=0),
        patch(
            "standard_tooling.bin.st_validate._find_custom_validator",
            return_value="/path/to/custom",
        ),
        patch(
            "standard_tooling.bin.st_validate._run_custom_validator",
            return_value=0,
        ) as mock_custom,
    ):
        result = main([])
    assert result == 0
    mock_custom.assert_called_once()


def test_run_all_language_none_skips_language_checks(tmp_path: Path) -> None:
    _write_config(tmp_path, "none")

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", return_value=0),
        patch("standard_tooling.bin.st_validate._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 0


# -- Unit tests for internal functions ----------------------------------------


def test_in_dev_container_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ST_IN_DEV_CONTAINER", "1")
    assert _in_dev_container() is True


def test_in_dev_container_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ST_IN_DEV_CONTAINER", raising=False)
    with patch("standard_tooling.bin.st_validate.Path.exists", return_value=False):
        assert _in_dev_container() is False


def test_run_commands_success() -> None:
    with patch(
        "standard_tooling.bin.st_validate.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    ):
        assert _run_commands([["echo", "hello"]], "test") == 0


def test_run_commands_failure_runs_all_by_default() -> None:
    results: Iterator[subprocess.CompletedProcess[bytes]] = iter(
        [
            subprocess.CompletedProcess(args=[], returncode=1),
            subprocess.CompletedProcess(args=[], returncode=0),
            subprocess.CompletedProcess(args=[], returncode=2),
        ]
    )
    with patch("standard_tooling.bin.st_validate.subprocess.run", side_effect=results) as mock:
        assert _run_commands([["cmd1"], ["cmd2"], ["cmd3"]], "test") == 2
    assert mock.call_count == 3


def test_run_commands_fail_fast_stops_on_first() -> None:
    with patch(
        "standard_tooling.bin.st_validate.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1),
    ) as mock:
        assert _run_commands([["cmd1"], ["cmd2"]], "test", fail_fast=True) == 1
    assert mock.call_count == 1


def test_find_custom_validator_entry_point() -> None:
    with patch("standard_tooling.bin.st_validate.shutil.which", return_value="/usr/bin/custom"):
        assert _find_custom_validator(Path("/fake")) == "/usr/bin/custom"


def test_find_custom_validator_local_script(tmp_path: Path) -> None:
    scripts_bin = tmp_path / "scripts" / "bin"
    scripts_bin.mkdir(parents=True)
    script = scripts_bin / "validate-custom"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    with patch("standard_tooling.bin.st_validate.shutil.which", return_value=None):
        assert _find_custom_validator(tmp_path) == str(script)


def test_find_custom_validator_none(tmp_path: Path) -> None:
    with patch("standard_tooling.bin.st_validate.shutil.which", return_value=None):
        assert _find_custom_validator(tmp_path) is None


def test_run_custom_validator() -> None:
    with patch(
        "standard_tooling.bin.st_validate.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    ):
        assert _run_custom_validator("/path/to/script") == 0


def test_run_custom_validator_failure() -> None:
    with patch(
        "standard_tooling.bin.st_validate.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1),
    ):
        assert _run_custom_validator("/path/to/script") == 1


# -- Config error handling ---------------------------------------------------


def test_missing_config_uses_empty_language(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
    ):
        result = main(["--check", "lint"])
    assert result == 0


def test_config_error_returns_1(tmp_path: Path) -> None:
    from standard_tooling.lib.config import ConfigError

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch(
            "standard_tooling.bin.st_validate.config.read_config",
            side_effect=ConfigError("bad config"),
        ),
    ):
        result = main(["--check", "lint"])
    assert result == 1


# -- Install failure stops single check --------------------------------------


def test_check_lint_install_failure_stops(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    call_count = 0

    def mock_run_commands(cmds: list[list[str]], label: str, **kwargs: bool) -> int:
        nonlocal call_count
        call_count += 1
        if label == "install":
            return 1
        return 0

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_commands", side_effect=mock_run_commands),
    ):
        result = main(["--check", "lint"])
    assert result == 1
    assert call_count == 1


# -- Run all: language check failure stops -----------------------------------


def test_run_all_language_check_failure_stops(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")

    def mock_commands(cmds: list[list[str]], label: str, **kwargs: bool) -> int:
        if label == "lint":
            return 1
        return 0

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", return_value=0),
        patch("standard_tooling.bin.st_validate._run_commands", side_effect=mock_commands),
    ):
        result = main([])
    assert result == 1


# -- Run all: custom validator failure stops ---------------------------------


def test_run_all_custom_validator_failure(tmp_path: Path) -> None:
    _write_config(tmp_path, "none")

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", return_value=0),
        patch(
            "standard_tooling.bin.st_validate._find_custom_validator",
            return_value="/path/to/custom",
        ),
        patch("standard_tooling.bin.st_validate._run_custom_validator", return_value=1),
    ):
        result = main([])
    assert result == 1


# -- Run all: install failure stops ------------------------------------------


def test_run_all_install_failure_stops(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")

    def mock_commands(cmds: list[list[str]], label: str, **kwargs: bool) -> int:
        if label == "install":
            return 1
        return 0

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", return_value=0),
        patch("standard_tooling.bin.st_validate._run_commands", side_effect=mock_commands),
    ):
        result = main([])
    assert result == 1


# -- _run_common_checks body --------------------------------------------------


def test_run_common_checks_calls_common_main() -> None:
    with patch(
        "standard_tooling.bin.validate_common.main",
        return_value=0,
    ) as mock:
        result = _run_common_checks(Path("/fake"))
    assert result == 0
    mock.assert_called_once()


# -- Branch: no install commands for single check -----------------------------


def test_single_check_no_install_commands(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")

    def mock_lang_cmds(lang: str, kind: CheckKind) -> list[list[str]]:
        if kind == CheckKind.INSTALL:
            return []
        if kind == CheckKind.LINT:
            return [["ruff", "check", "src/"]]
        return []

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate.language_commands", side_effect=mock_lang_cmds),
        patch("standard_tooling.bin.st_validate._run_commands", return_value=0) as mock_run,
    ):
        result = main(["--check", "lint"])
    assert result == 0
    mock_run.assert_called_once_with([["ruff", "check", "src/"]], "lint")


# -- Branch: no install commands in run-all mode ------------------------------


def test_venv_bin_prepended_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, "python")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin")

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_commands", return_value=0),
    ):
        result = main(["--check", "lint"])
    assert result == 0
    assert str(venv_bin) in os.environ["PATH"].split(os.pathsep)


def test_venv_bin_not_prepended_when_already_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, "python")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{venv_bin}:/usr/bin")

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_commands", return_value=0),
    ):
        main(["--check", "lint"])
    count = os.environ["PATH"].split(os.pathsep).count(str(venv_bin))
    assert count == 1


def test_run_all_no_install_commands(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")

    def mock_lang_cmds(lang: str, kind: CheckKind) -> list[list[str]]:
        if kind == CheckKind.INSTALL:
            return []
        if kind == CheckKind.LINT:
            return [["ruff", "check", "src/"]]
        return []

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_common_checks", return_value=0),
        patch("standard_tooling.bin.st_validate.language_commands", side_effect=mock_lang_cmds),
        patch("standard_tooling.bin.st_validate._run_commands", return_value=0),
        patch("standard_tooling.bin.st_validate._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 0
