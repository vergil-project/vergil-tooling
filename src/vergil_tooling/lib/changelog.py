"""Changelog and release notes generation via git-cliff."""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

RELEASE_NOTES_DIR = "releases"


def generate_changelog(repo_root: Path, version: str) -> None:
    """Generate CHANGELOG.md using git-cliff."""
    tag = f"develop-v{version}"
    config_path = files("vergil_tooling.configs") / "cliff.toml"
    output = repo_root / "CHANGELOG.md"
    result = subprocess.run(  # noqa: S603
        (  # noqa: S607
            "git-cliff",
            "--config",
            str(config_path),
            "--tag",
            tag,
            "-o",
            str(output),
        ),
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    _normalize_trailing_newline(output)


def generate_release_notes(repo_root: Path, version: str) -> Path:
    """Generate per-release notes file using git-cliff."""
    tag = f"develop-v{version}"
    releases_dir = repo_root / RELEASE_NOTES_DIR
    releases_dir.mkdir(exist_ok=True)
    output = releases_dir / f"v{version}.md"
    config_path = files("vergil_tooling.configs") / "cliff-release-notes.toml"
    result = subprocess.run(  # noqa: S603
        (  # noqa: S607
            "git-cliff",
            "--config",
            str(config_path),
            "--tag",
            tag,
            "--unreleased",
            "-o",
            str(output),
        ),
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    _normalize_trailing_newline(output)
    return output


def _normalize_trailing_newline(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
