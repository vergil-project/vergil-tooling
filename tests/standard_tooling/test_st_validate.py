"""Tests for standard_tooling.bin.st_validate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from standard_tooling.bin.st_validate import main


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
    calls: list[str] = []

    def mock_run_commands(cmds: list[str], label: str) -> int:
        calls.extend(cmds)
        return 0

    with (
        patch("standard_tooling.bin.st_validate.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_validate._run_commands", side_effect=mock_run_commands),
    ):
        result = main(["--check", "lint"])
    assert result == 0
    assert "uv sync --frozen --group dev" in calls
    assert "ruff check src/ tests/" in calls


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

    def mock_commands(cmds: list[str], label: str) -> int:
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
