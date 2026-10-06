"""Tests for vergil_tooling.lib.package.naming."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.config import PackageConfig, PackagePythonConfig
from vergil_tooling.lib.package import PackageError, naming

if TYPE_CHECKING:
    from pathlib import Path


def _pkg(**kw: Any) -> PackageConfig:
    base: dict[str, Any] = {
        "builder": "python",
        "vendor": "vergil",
        "summary": "s",
        "smoke": "true",
        "python": PackagePythonConfig(runtime="3.14.4"),
    }
    base.update(kw)
    return PackageConfig(**base)


def test_python_name_comes_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    assert naming.package_name(tmp_path, _pkg()) == "vergil-tooling"


def test_explicit_name_wins(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    assert naming.package_name(tmp_path, _pkg(name="x")) == "x"


def test_explicit_name_needs_no_pyproject(tmp_path: Path) -> None:
    assert naming.package_name(tmp_path, _pkg(name="x")) == "x"


@pytest.mark.parametrize("body", ["[project]\n", "", '[project]\nname = ""\n', "project = 1\n"])
def test_python_without_pyproject_name_is_fatal(tmp_path: Path, body: str) -> None:
    (tmp_path / "pyproject.toml").write_text(body)
    with pytest.raises(PackageError, match=r"pyproject.toml has no \[project\]\.name"):
        naming.package_name(tmp_path, _pkg())


def test_missing_pyproject_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match=r"pyproject.toml not found .*set \[package\]\.name"):
        naming.package_name(tmp_path, _pkg())


def test_invalid_pyproject_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\n")
    with pytest.raises(PackageError, match=r"pyproject.toml is not valid TOML"):
        naming.package_name(tmp_path, _pkg())
