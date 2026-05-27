"""Tests for vergil_tooling.bin.vrg_docs_patch_nav CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_docs_patch_nav import main

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_docs_patch_nav"

_NAV = """\
nav:
  - Home: index.md
  - Releases:
      - Changelog: changelog.md
      - Release Notes:
          - releases/index.md
  - Getting Started: getting-started.md
"""


def test_patches_nav(tmp_path: Path) -> None:
    mkdocs = tmp_path / "mkdocs.yml"
    mkdocs.write_text(_NAV)
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
    rc = main(["--mkdocs-yml", str(mkdocs), "--releases-dir", str(releases)])
    assert rc == 0
    assert "1.0.0: releases/v1.0.0.md" in mkdocs.read_text()


def test_missing_mkdocs(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    releases.mkdir()
    with patch(f"{_MOD}.emit_error") as mock_err:
        rc = main(["--mkdocs-yml", str(tmp_path / "missing.yml"), "--releases-dir", str(releases)])
    assert rc == 1
    mock_err.assert_called_once()


def test_missing_releases_dir(tmp_path: Path) -> None:
    mkdocs = tmp_path / "mkdocs.yml"
    mkdocs.write_text(_NAV)
    with patch(f"{_MOD}.emit_error") as mock_err:
        rc = main(["--mkdocs-yml", str(mkdocs), "--releases-dir", str(tmp_path / "missing")])
    assert rc == 1
    mock_err.assert_called_once()
