"""Tests for vrg-changelog CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from vergil_tooling.bin.vrg_changelog import main


def _write_toml(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        '\n[dependencies]\nvergil = "v2.0"\n'
        '\n[ci]\nversions = ["3.14"]\n'
    )


def test_default_generates_both(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with (
        patch("vergil_tooling.bin.vrg_changelog.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-changelog"]),
        patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run,
    ):
        import subprocess as _sp

        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        (tmp_path / "CHANGELOG.md").write_text("log\n")
        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()
        (releases_dir / "v1.0.0.md").write_text("notes\n")
        result = main()
    assert result == 0
    out = capsys.readouterr().out
    assert "CHANGELOG.md" in out


def test_changelog_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with (
        patch("vergil_tooling.bin.vrg_changelog.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-changelog", "--changelog-only"]),
        patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run,
    ):
        import subprocess as _sp

        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        (tmp_path / "CHANGELOG.md").write_text("log\n")
        result = main()
    assert result == 0
    out = capsys.readouterr().out
    assert "CHANGELOG.md" in out


def test_notes_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with (
        patch("vergil_tooling.bin.vrg_changelog.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-changelog", "--notes-only"]),
        patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run,
    ):
        import subprocess as _sp

        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        releases_dir = tmp_path / "releases"
        releases_dir.mkdir()
        (releases_dir / "v1.0.0.md").write_text("notes\n")
        result = main()
    assert result == 0
    out = capsys.readouterr().out
    assert "Generated" in out


def test_missing_version_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    with (
        patch("vergil_tooling.bin.vrg_changelog.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-changelog"]),
    ):
        result = main()
    assert result == 1
    err = capsys.readouterr().err
    assert "VERSION" in err
