"""Tests for vergil_tooling.lib.changelog."""

from __future__ import annotations

import subprocess as _sp
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.changelog import (
    RELEASE_NOTES_DIR,
    generate_changelog,
    generate_release_notes,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_generate_changelog_calls_git_cliff(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("old content\n")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        generate_changelog(tmp_path, "2.0.34")
        args = mock_run.call_args[0][0]
        assert args[0] == "git-cliff"
        assert "--tag" in args
        assert "develop-v2.0.34" in args
        assert "-o" in args
        assert "CHANGELOG.md" in args[-1]


def test_generate_changelog_normalizes_trailing_newline(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("content\n\n\n")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        generate_changelog(tmp_path, "1.0.0")
    assert changelog.read_text().endswith("\n")
    assert not changelog.read_text().endswith("\n\n")


def test_generate_release_notes_creates_dir_and_file(tmp_path: Path) -> None:
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        releases_dir = tmp_path / RELEASE_NOTES_DIR
        releases_dir.mkdir()
        output = tmp_path / RELEASE_NOTES_DIR / "v2.0.34.md"
        output.write_text("notes\n")
        result = generate_release_notes(tmp_path, "2.0.34")
        assert result == output
        args = mock_run.call_args[0][0]
        assert "--unreleased" in args


def test_generate_changelog_raises_on_cliff_failure(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.side_effect = _sp.CalledProcessError(1, "git-cliff")
        with pytest.raises(_sp.CalledProcessError):
            generate_changelog(tmp_path, "1.0.0")


def test_generate_changelog_prints_stdout_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("old\n")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(
            args=[], returncode=0, stdout="cliff output\n", stderr="cliff warning\n"
        )
        generate_changelog(tmp_path, "1.0.0")
    captured = capsys.readouterr()
    assert "cliff output" in captured.out
    assert "cliff warning" in captured.err


def test_generate_release_notes_prints_stdout_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    releases_dir = tmp_path / RELEASE_NOTES_DIR
    releases_dir.mkdir()
    output = releases_dir / "v1.0.0.md"
    output.write_text("notes\n")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(
            args=[], returncode=0, stdout="notes output\n", stderr="notes warning\n"
        )
        generate_release_notes(tmp_path, "1.0.0")
    captured = capsys.readouterr()
    assert "notes output" in captured.out
    assert "notes warning" in captured.err
