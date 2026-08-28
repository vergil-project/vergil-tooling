"""Tests for the dev-dependency-shape classifier (fleet survey).

``classify_dev_deps`` reports *where* a repo declares its development
dependencies, so the Task-9 xdist fleet sweep can add ``pytest-xdist`` in the
correct shape (or report loudly for an ``UNKNOWN`` shape rather than guessing).
Epic vergil-project/.github#333, Task 3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.dev_deps import DevDepShape, classify_dev_deps

if TYPE_CHECKING:
    from pathlib import Path


def test_uv_dependency_groups_shape(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    assert classify_dev_deps(tmp_path) is DevDepShape.UV_GROUPS


def test_pep621_optional_dependencies_shape(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    )
    assert classify_dev_deps(tmp_path) is DevDepShape.PEP621_OPTIONAL


def test_poetry_dev_dependencies_shape(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n[tool.poetry.dev-dependencies]\npytest = "*"\n'
    )
    assert classify_dev_deps(tmp_path) is DevDepShape.POETRY


def test_poetry_group_shape(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n[tool.poetry.group.dev.dependencies]\npytest = "*"\n'
    )
    assert classify_dev_deps(tmp_path) is DevDepShape.POETRY


def test_requirements_dev_txt_shape(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    assert classify_dev_deps(tmp_path) is DevDepShape.REQUIREMENTS_TXT


def test_requirements_test_txt_shape(tmp_path: Path) -> None:
    (tmp_path / "requirements-test.txt").write_text("pytest\n")
    assert classify_dev_deps(tmp_path) is DevDepShape.REQUIREMENTS_TXT


def test_pyproject_without_dev_shape_falls_back_to_requirements(tmp_path: Path) -> None:
    # A pyproject that declares no dev-dependency shape must not shadow a
    # requirements file that does.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    assert classify_dev_deps(tmp_path) is DevDepShape.REQUIREMENTS_TXT


def test_pyproject_without_dev_shape_and_no_requirements_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    assert classify_dev_deps(tmp_path) is DevDepShape.UNKNOWN


def test_no_recognized_files_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = weird\n")
    assert classify_dev_deps(tmp_path) is DevDepShape.UNKNOWN
