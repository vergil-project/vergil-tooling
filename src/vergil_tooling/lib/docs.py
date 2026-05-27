"""Docs staging and nav patching utilities."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_SEMVER_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_HEADER_RE = re.compile(r"^#\s+Release\s+(\S+)\s+\((\d{4}-\d{2}-\d{2})\)")


def semver_key(name: str) -> tuple[int, ...]:
    """Extract (major, minor, patch) from a version string for sorting."""
    m = _SEMVER_RE.search(name)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    date: str
    filename: str


def collect_releases(releases_dir: Path) -> list[ReleaseEntry]:
    """Collect release note files and extract version/date metadata.

    Returns entries sorted by semver descending (newest first).
    """
    entries: list[ReleaseEntry] = []
    if not releases_dir.is_dir():
        return entries
    for f in releases_dir.glob("v*.md"):
        content = f.read_text(encoding="utf-8")
        first_line = content.lstrip().split("\n", 1)[0]
        m = _HEADER_RE.match(first_line)
        if m:
            entries.append(ReleaseEntry(version=m.group(1), date=m.group(2), filename=f.name))
        else:
            version = f.stem
            entries.append(ReleaseEntry(version=version.lstrip("v"), date="", filename=f.name))
    entries.sort(key=lambda e: semver_key(e.version), reverse=True)
    return entries


def generate_release_index(entries: list[ReleaseEntry]) -> str:
    """Generate releases/index.md content from collected entries."""
    lines = ["# Release Notes", ""]
    if not entries:
        lines.append("No releases yet.")
        return "\n".join(lines) + "\n"
    lines.append("| Version | Date |")
    lines.append("|---------|------|")
    for entry in entries:
        link = f"[{entry.version}]({entry.filename})"
        lines.append(f"| {link} | {entry.date} |")
    return "\n".join(lines) + "\n"


def stage_docs(
    *,
    docs_dir: Path,
    releases_dir: Path,
    changelog: Path | None,
) -> int:
    """Stage changelog and release notes into the docs build directory.

    Returns the number of release notes staged.
    """
    docs_releases = docs_dir / "releases"
    docs_releases.mkdir(parents=True, exist_ok=True)

    if changelog and changelog.is_file():
        shutil.copy2(changelog, docs_dir / "changelog.md")

    entries = collect_releases(releases_dir)
    for entry in entries:
        src = releases_dir / entry.filename
        shutil.copy2(src, docs_releases / entry.filename)

    index_content = generate_release_index(entries)
    (docs_releases / "index.md").write_text(index_content, encoding="utf-8")

    return len(entries)


def patch_nav(mkdocs_yml: Path, releases_dir: Path) -> None:
    """Patch mkdocs.yml nav to include release note version entries.

    Scans the releases directory for version files and rebuilds
    the "Release Notes" nav section with semver-sorted entries.
    """
    content = mkdocs_yml.read_text(encoding="utf-8")
    lines = content.split("\n")

    release_notes_idx = None
    index_md_idx = None
    indent = ""

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("- Release Notes:"):
            release_notes_idx = i
        elif release_notes_idx is not None and "releases/index.md" in line:
            index_md_idx = i
            indent = line[: len(line) - len(stripped)]
            break

    if index_md_idx is None:
        return

    end_idx = index_md_idx + 1
    while end_idx < len(lines):
        line = lines[end_idx]
        if not line.strip():
            end_idx += 1
            continue
        line_indent = len(line) - len(line.lstrip())
        base_indent = len(indent)
        if line_indent < base_indent:
            break
        if line_indent == base_indent and "releases/v" not in line:
            break
        end_idx += 1

    entries = collect_releases(releases_dir)
    new_lines = [f"{indent}- {e.version}: releases/{e.filename}" for e in entries]

    lines[index_md_idx + 1 : end_idx] = new_lines
    mkdocs_yml.write_text("\n".join(lines), encoding="utf-8")
