"""Tests for vergil_tooling.bin.vrg_docs_stage CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_docs_stage import _has_evidence_asset, main
from vergil_tooling.lib.github import GitHubAPIError

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
    with (
        patch(f"{_MOD}.write_output") as mock_out,
        patch(f"{_MOD}.github.current_repo", return_value="o/r"),
        patch(f"{_MOD}._has_evidence_asset", return_value=False),
    ):
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


def test_links_evidence_with_explicit_repo(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "v2.1.129.md").write_text("# Release 2.1.129 (2026-07-01)\n")
    with (
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}._has_evidence_asset", return_value=True) as mock_has,
        patch(f"{_MOD}.github.current_repo") as mock_current,
    ):
        rc = main(
            [
                "--docs-dir",
                str(docs),
                "--releases-dir",
                str(releases),
                "--changelog",
                str(tmp_path / "nope.md"),
                "--repo",
                "o/r",
            ]
        )
    assert rc == 0
    mock_current.assert_not_called()  # explicit --repo skips remote resolution
    mock_has.assert_called_once_with("o/r", "v2.1.129")
    staged = (docs / "releases" / "v2.1.129.md").read_text()
    assert "**CI Evidence:**" in staged
    assert "v2.1.129-ci-evidence.tar.gz" in staged


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
    with (
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.github.current_repo", return_value="o/r"),
    ):
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
    with (
        patch(f"{_MOD}.write_output") as mock_out,
        patch(f"{_MOD}.github.current_repo", return_value="o/r"),
    ):
        rc = main(["--docs-dir", str(docs), "--releases-dir", str(releases)])
    assert rc == 0
    mock_out.assert_called_once_with("releases_staged", "0")


class TestHasEvidenceAsset:
    def test_asset_present(self) -> None:
        with patch(
            f"{_MOD}.github.read_json",
            return_value={"assets": [{"name": "v2.1.129-ci-evidence.tar.gz"}]},
        ):
            assert _has_evidence_asset("o/r", "v2.1.129") is True

    def test_asset_absent(self) -> None:
        with patch(
            f"{_MOD}.github.read_json",
            return_value={"assets": [{"name": "other.txt"}]},
        ):
            assert _has_evidence_asset("o/r", "v2.1.129") is False

    def test_release_not_found_is_no_asset(self) -> None:
        err = GitHubAPIError(1, "gh release view", stderr="release not found")
        with patch(f"{_MOD}.github.read_json", side_effect=err):
            assert _has_evidence_asset("o/r", "v9.9.9") is False

    def test_other_error_propagates(self) -> None:
        err = GitHubAPIError(1, "gh release view", stderr="HTTP 500 boom")
        with patch(f"{_MOD}.github.read_json", side_effect=err), pytest.raises(GitHubAPIError):
            _has_evidence_asset("o/r", "v2.1.129")

    def test_non_dict_payload_is_no_asset(self) -> None:
        with patch(f"{_MOD}.github.read_json", return_value=[]):
            assert _has_evidence_asset("o/r", "v2.1.129") is False
