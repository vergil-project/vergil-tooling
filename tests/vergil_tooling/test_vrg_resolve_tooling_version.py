"""Tests for vergil_tooling.bin.vrg_resolve_tooling_version CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_resolve_tooling_version import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_MOD = "vergil_tooling.bin.vrg_resolve_tooling_version"

_VALID_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0.50"

[ci]
versions = ["3.14"]
"""

_MISSING_DEPS_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[ci]
versions = ["3.14"]
"""


def test_resolves_from_toml(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(f"{_MOD}.write_output") as mock_out,
    ):
        rc = main(["--repo-root", str(tmp_path)])
    assert rc == 0
    mock_out.assert_called_once_with("vergil_version", "v2.0.50")


def test_env_override(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with (
        patch.dict("os.environ", {"VRG_DOCKER_INSTALL_TAG": "v9.9.9"}, clear=True),
        patch(f"{_MOD}.write_output") as mock_out,
    ):
        rc = main(["--repo-root", str(tmp_path)])
    assert rc == 0
    mock_out.assert_called_once_with("vergil_version", "v9.9.9")


def test_missing_toml(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(f"{_MOD}.emit_error") as mock_err,
    ):
        rc = main(["--repo-root", str(tmp_path)])
    assert rc == 1
    mock_err.assert_called_once()
    assert "not found" in mock_err.call_args[0][0]


def test_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("not valid [[[toml")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(f"{_MOD}.emit_error") as mock_err,
    ):
        rc = main(["--repo-root", str(tmp_path)])
    assert rc == 1
    mock_err.assert_called_once()


def test_missing_deps_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_MISSING_DEPS_TOML)
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(f"{_MOD}.emit_error") as mock_err,
    ):
        rc = main(["--repo-root", str(tmp_path)])
    assert rc == 1
    mock_err.assert_called_once()


def test_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    monkeypatch.chdir(tmp_path)  # type: ignore[union-attr]
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(f"{_MOD}.write_output") as mock_out,
    ):
        rc = main([])
    assert rc == 0
    mock_out.assert_called_once_with("vergil_version", "v2.0.50")
