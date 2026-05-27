"""Tests for vergil_tooling.bin.vrg_docs_stage CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_docs_stage import main

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_docs_stage"


def test_stages_successfully(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n")
    with patch(f"{_MOD}.write_output") as mock_out:
        rc = main(
            [
                "--docs-dir",
                str(docs),
                "--releases-dir",
                str(releases),
                "--changelog",
                str(changelog),
            ]
        )
    assert rc == 0
    mock_out.assert_called_once_with("releases_staged", "1")
    assert (docs / "changelog.md").is_file()
    assert (docs / "releases" / "index.md").is_file()


def test_missing_docs_dir(tmp_path: Path) -> None:
    with patch(f"{_MOD}.emit_error") as mock_err:
        rc = main(["--docs-dir", str(tmp_path / "missing"), "--releases-dir", str(tmp_path)])
    assert rc == 1
    mock_err.assert_called_once()


def test_missing_changelog(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    with patch(f"{_MOD}.write_output"):
        rc = main(
            [
                "--docs-dir",
                str(docs),
                "--releases-dir",
                str(releases),
                "--changelog",
                str(tmp_path / "nonexistent" / "CHANGELOG.md"),
            ]
        )
    assert rc == 0
    assert not (docs / "changelog.md").exists()


def test_empty_releases(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    with patch(f"{_MOD}.write_output") as mock_out:
        rc = main(["--docs-dir", str(docs), "--releases-dir", str(releases)])
    assert rc == 0
    mock_out.assert_called_once_with("releases_staged", "0")
