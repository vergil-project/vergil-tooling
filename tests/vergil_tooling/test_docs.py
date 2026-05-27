"""Tests for vergil_tooling.lib.docs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.docs import (
    ReleaseEntry,
    collect_releases,
    generate_release_index,
    patch_nav,
    semver_key,
    stage_docs,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestSemverKey:
    def test_basic(self) -> None:
        assert semver_key("v1.2.3") == (1, 2, 3)

    def test_without_v(self) -> None:
        assert semver_key("1.2.3") == (1, 2, 3)

    def test_from_filename(self) -> None:
        assert semver_key("v2.0.59.md") == (2, 0, 59)

    def test_no_match(self) -> None:
        assert semver_key("invalid") == (0, 0, 0)


class TestCollectReleases:
    def test_collects_and_sorts(self, tmp_path: Path) -> None:
        (tmp_path / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        (tmp_path / "v2.0.0.md").write_text("# Release 2.0.0 (2026-03-01)\n")
        (tmp_path / "v1.5.0.md").write_text("# Release 1.5.0 (2026-02-01)\n")
        entries = collect_releases(tmp_path)
        versions = [e.version for e in entries]
        assert versions == ["2.0.0", "1.5.0", "1.0.0"]

    def test_extracts_dates(self, tmp_path: Path) -> None:
        (tmp_path / "v1.0.0.md").write_text("# Release 1.0.0 (2026-04-15)\n")
        entries = collect_releases(tmp_path)
        assert entries[0].date == "2026-04-15"

    def test_handles_missing_header(self, tmp_path: Path) -> None:
        (tmp_path / "v1.0.0.md").write_text("No proper header\n")
        entries = collect_releases(tmp_path)
        assert len(entries) == 1
        assert entries[0].version == "1.0.0"
        assert entries[0].date == ""

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert collect_releases(tmp_path) == []

    def test_missing_dir(self, tmp_path: Path) -> None:
        assert collect_releases(tmp_path / "nonexistent") == []

    def test_ignores_non_version_files(self, tmp_path: Path) -> None:
        (tmp_path / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        (tmp_path / "index.md").write_text("# Index\n")
        (tmp_path / "README.md").write_text("# Readme\n")
        entries = collect_releases(tmp_path)
        assert len(entries) == 1

    def test_leading_whitespace_in_header(self, tmp_path: Path) -> None:
        (tmp_path / "v1.0.0.md").write_text("\n# Release 1.0.0 (2026-01-01)\n")
        entries = collect_releases(tmp_path)
        assert entries[0].version == "1.0.0"


class TestGenerateReleaseIndex:
    def test_with_entries(self) -> None:
        entries = [
            ReleaseEntry(version="2.0.0", date="2026-03-01", filename="v2.0.0.md"),
            ReleaseEntry(version="1.0.0", date="2026-01-01", filename="v1.0.0.md"),
        ]
        content = generate_release_index(entries)
        assert "# Release Notes" in content
        assert "[2.0.0](v2.0.0.md)" in content
        assert "[1.0.0](v1.0.0.md)" in content
        assert content.index("2.0.0") < content.index("1.0.0")

    def test_empty(self) -> None:
        content = generate_release_index([])
        assert "No releases yet." in content


class TestStageDocs:
    def test_stages_changelog_and_releases(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("# Changelog\n")
        count = stage_docs(docs_dir=docs_dir, releases_dir=releases, changelog=changelog)
        assert count == 1
        assert (docs_dir / "changelog.md").is_file()
        assert (docs_dir / "releases" / "v1.0.0.md").is_file()
        assert (docs_dir / "releases" / "index.md").is_file()

    def test_no_changelog(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        count = stage_docs(docs_dir=docs_dir, releases_dir=releases, changelog=None)
        assert count == 1
        assert not (docs_dir / "changelog.md").exists()

    def test_empty_releases(self, tmp_path: Path) -> None:
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        releases = tmp_path / "releases"
        releases.mkdir()
        count = stage_docs(docs_dir=docs_dir, releases_dir=releases, changelog=None)
        assert count == 0
        index = (docs_dir / "releases" / "index.md").read_text()
        assert "No releases yet." in index


_NAV_TEMPLATE = """\
nav:
  - Home: index.md
  - Releases:
      - Changelog: changelog.md
      - Release Notes:
          - releases/index.md
  - Getting Started: getting-started.md
"""


class TestPatchNav:
    def test_inserts_version_entries(self, tmp_path: Path) -> None:
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text(_NAV_TEMPLATE)
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v2.0.0.md").write_text("# Release 2.0.0 (2026-03-01)\n")
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert "- 2.0.0: releases/v2.0.0.md" in content
        assert "- 1.0.0: releases/v1.0.0.md" in content
        assert content.index("2.0.0") < content.index("1.0.0")
        assert "releases/index.md" in content

    def test_preserves_other_nav(self, tmp_path: Path) -> None:
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text(_NAV_TEMPLATE)
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert "- Home: index.md" in content
        assert "- Getting Started: getting-started.md" in content

    def test_replaces_existing_entries(self, tmp_path: Path) -> None:
        nav_with_entries = _NAV_TEMPLATE.replace(
            "          - releases/index.md\n",
            "          - releases/index.md\n          - 1.0.0: releases/v1.0.0.md\n",
        )
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text(nav_with_entries)
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v2.0.0.md").write_text("# Release 2.0.0 (2026-03-01)\n")
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert content.count("1.0.0: releases/v1.0.0.md") == 1
        assert "2.0.0: releases/v2.0.0.md" in content

    def test_no_release_notes_section(self, tmp_path: Path) -> None:
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text("nav:\n  - Home: index.md\n")
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert "v1.0.0" not in content

    def test_empty_releases(self, tmp_path: Path) -> None:
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text(_NAV_TEMPLATE)
        releases = tmp_path / "releases"
        releases.mkdir()
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert "releases/index.md" in content

    def test_blank_lines_between_entries(self, tmp_path: Path) -> None:
        nav_with_blanks = _NAV_TEMPLATE.replace(
            "          - releases/index.md\n",
            "          - releases/index.md\n\n          - 1.0.0: releases/v1.0.0.md\n",
        )
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text(nav_with_blanks)
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v2.0.0.md").write_text("# Release 2.0.0 (2026-03-01)\n")
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert "2.0.0: releases/v2.0.0.md" in content
        assert content.count("1.0.0") == 0

    def test_release_notes_at_end_of_nav(self, tmp_path: Path) -> None:
        nav_at_end = (
            "nav:\n"
            "  - Home: index.md\n"
            "  - Releases:\n"
            "      - Release Notes:\n"
            "          - releases/index.md\n"
        )
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text(nav_at_end)
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert "1.0.0: releases/v1.0.0.md" in content

    def test_stops_at_non_version_same_indent(self, tmp_path: Path) -> None:
        nav_with_extra = _NAV_TEMPLATE.replace(
            "          - releases/index.md\n",
            "          - releases/index.md\n          - Other: other.md\n",
        )
        mkdocs = tmp_path / "mkdocs.yml"
        mkdocs.write_text(nav_with_extra)
        releases = tmp_path / "releases"
        releases.mkdir()
        (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
        patch_nav(mkdocs, releases)
        content = mkdocs.read_text()
        assert "1.0.0: releases/v1.0.0.md" in content
        assert "Other: other.md" in content
