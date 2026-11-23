"""Tests for vrg-version CLI."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_version import main
from vergil_tooling.lib.version import CargoVersionError, VersionSyncError

if TYPE_CHECKING:
    from pathlib import Path


def _write_toml(tmp_path: Path, language: str = "") -> None:
    lang_line = f'primary-language = "{language}"\n' if language else ""
    (tmp_path / "vergil.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        f'{lang_line}\n[dependencies]\nvergil = "v2.0"\n'
        f'\n[ci]\nversions = ["3.14"]\n'
    )


def test_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("vergil_tooling.bin.vrg_version.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-version", "show"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.2.3"


def test_show_major_minor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("vergil_tooling.bin.vrg_version.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-version", "show", "--major-minor"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.2"


def test_show_ref(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("vergil_tooling.bin.vrg_version.Path.cwd", return_value=tmp_path),
        patch(
            "vergil_tooling.lib.version._read_version_from_ref",
            return_value="1.1.0",
        ),
        patch("sys.argv", ["vrg-version", "show", "--ref", "origin/main"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.1.0"


def test_bump(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("vergil_tooling.bin.vrg_version.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-version", "bump"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.2.4"
    assert (tmp_path / "VERSION").read_text().strip() == "1.2.4"


# -- expected errors are one line, not a traceback (#4484) ------------------------


def _run_main(tmp_path: Path, *argv: str) -> None:
    with (
        patch("vergil_tooling.bin.vrg_version.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-version", *argv]),
    ):
        main()


def test_missing_version_file_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_toml(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        _run_main(tmp_path, "show")
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == f"vrg-version: VERSION file not found at {tmp_path}\n"


def test_cargo_manifest_with_no_version_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_toml(tmp_path, "rust")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n')
    with pytest.raises(SystemExit) as excinfo:
        _run_main(tmp_path, "bump")
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == (
        "vrg-version: Cargo.toml holds no version: [package] has no version string "
        "and does not inherit one from [workspace.package] (version.workspace = true)\n"
    )
    assert (tmp_path / "VERSION").read_text() == "1.2.3\n"


@pytest.mark.parametrize(
    "error", [CargoVersionError("cargo says no"), VersionSyncError("out of step")]
)
def test_version_errors_are_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_version.show", side_effect=error),
        pytest.raises(SystemExit) as excinfo,
    ):
        _run_main(tmp_path, "show")
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == f"vrg-version: {error}\n"


def test_version_mismatch_is_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "1.2.0"\n')
    with pytest.raises(SystemExit) as excinfo:
        _run_main(tmp_path, "show")
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == (
        "vrg-version: VERSION contains 1.2.3 but pyproject.toml contains 1.2.0\n"
    )


def test_failed_command_is_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    error = subprocess.CalledProcessError(128, ["git", "show", "origin/main:VERSION"])
    with (
        patch("vergil_tooling.bin.vrg_version.show", side_effect=error),
        pytest.raises(SystemExit) as excinfo,
    ):
        _run_main(tmp_path, "show", "--ref", "origin/main")
    assert excinfo.value.code == 1
    assert capsys.readouterr().err == "vrg-version: git show origin/main:VERSION exited 128\n"


def test_failed_string_command_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = subprocess.CalledProcessError(1, "uv lock")
    with (
        patch("vergil_tooling.bin.vrg_version.bump", side_effect=error),
        pytest.raises(SystemExit),
    ):
        _run_main(tmp_path, "bump")
    assert capsys.readouterr().err == "vrg-version: uv lock exited 1\n"


def test_unexpected_error_keeps_its_traceback(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_version.show", side_effect=RuntimeError("bug")),
        pytest.raises(RuntimeError, match="bug"),
    ):
        _run_main(tmp_path, "show")


def test_invalid_config_is_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text("[project\n")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with pytest.raises(SystemExit) as excinfo:
        _run_main(tmp_path, "show")
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith(f"vrg-version: {tmp_path / 'vergil.toml'} is not valid TOML: ")
    assert err.count("\n") == 1
