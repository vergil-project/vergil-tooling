"""Tests for vergil_tooling.lib.self_repo."""

from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.self_repo import is_self_repo


def test_is_self_repo_true(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    assert is_self_repo(tmp_path) is True


def test_is_self_repo_false_different_name(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-app"\n')
    assert is_self_repo(tmp_path) is False


def test_is_self_repo_false_no_pyproject(tmp_path: Path) -> None:
    assert is_self_repo(tmp_path) is False


def test_is_self_repo_false_no_project_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    assert is_self_repo(tmp_path) is False


def test_is_self_repo_false_project_not_a_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('project = "vergil-tooling"\n')
    assert is_self_repo(tmp_path) is False


def test_is_self_repo_false_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("not valid [[[toml content")
    assert is_self_repo(tmp_path) is False


def test_this_checkout_is_the_self_repo() -> None:
    assert is_self_repo(Path(__file__).resolve().parents[2]) is True
