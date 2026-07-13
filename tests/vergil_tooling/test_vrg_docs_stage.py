"""Tests for vergil_tooling.bin.vrg_docs_stage CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_docs_stage import (
    _evidence_asset_tags,
    _evidence_resolver,
    _is_auth_error,
    main,
)
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
        patch(f"{_MOD}._evidence_asset_tags", return_value=frozenset()),
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
        patch(f"{_MOD}._evidence_asset_tags", return_value=frozenset({"v2.1.129"})) as mock_tags,
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
    mock_tags.assert_called_once_with("o/r")  # one batched call, not per release
    staged = (docs / "releases" / "v2.1.129.md").read_text()
    assert "**CI Evidence:**" in staged
    assert "v2.1.129-ci-evidence.tar.gz" in staged


def test_single_api_call_for_many_releases(tmp_path: Path) -> None:
    """The batched lookup makes exactly one API call regardless of release count."""
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    tags = ["v1.0.0", "v1.1.0", "v2.0.0", "v2.1.0"]
    for tag in tags:
        (releases / f"{tag}.md").write_text(f"# Release {tag.lstrip('v')} (2026-01-01)\n")
    payload = [{"tag_name": tag, "assets": [{"name": f"{tag}-ci-evidence.tar.gz"}]} for tag in tags]
    with (
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.github.read_json", return_value=payload) as mock_read,
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
    mock_read.assert_called_once_with("api", "repos/o/r/releases", "--paginate")
    for tag in tags:
        assert "**CI Evidence:**" in (docs / "releases" / f"{tag}.md").read_text()


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
        patch(f"{_MOD}._evidence_asset_tags", return_value=frozenset()),
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


def test_empty_releases_makes_no_api_call(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    with (
        patch(f"{_MOD}.write_output") as mock_out,
        patch(f"{_MOD}.github.current_repo", return_value="o/r"),
        patch(f"{_MOD}.github.read_json") as mock_read,
    ):
        rc = main(["--docs-dir", str(docs), "--releases-dir", str(releases)])
    assert rc == 0
    mock_out.assert_called_once_with("releases_staged", "0")
    mock_read.assert_not_called()  # lazy resolver: zero release pages, zero calls


def test_auth_error_emits_clean_message(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
    err = GitHubAPIError(
        1, "gh api", stderr="gh: To use GitHub CLI, populate the GH_TOKEN environment variable"
    )
    with (
        patch(f"{_MOD}.github.read_json", side_effect=err),
        patch(f"{_MOD}.emit_error") as mock_err,
    ):
        rc = main(
            [
                "--docs-dir",
                str(docs),
                "--releases-dir",
                str(releases),
                "--repo",
                "o/r",
            ]
        )
    assert rc == 1
    mock_err.assert_called_once()
    assert "GH_TOKEN" in mock_err.call_args.args[0]


def test_non_auth_error_propagates(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
    err = GitHubAPIError(1, "gh api", stderr="HTTP 500 boom")
    with (
        patch(f"{_MOD}.github.read_json", side_effect=err),
        pytest.raises(GitHubAPIError),
    ):
        main(
            [
                "--docs-dir",
                str(docs),
                "--releases-dir",
                str(releases),
                "--repo",
                "o/r",
            ]
        )


class TestEvidenceAssetTags:
    def test_asset_present(self) -> None:
        payload = [{"tag_name": "v2.1.129", "assets": [{"name": "v2.1.129-ci-evidence.tar.gz"}]}]
        with patch(f"{_MOD}.github.read_json", return_value=payload):
            assert _evidence_asset_tags("o/r") == frozenset({"v2.1.129"})

    def test_asset_absent(self) -> None:
        payload = [{"tag_name": "v2.1.129", "assets": [{"name": "other.txt"}]}]
        with patch(f"{_MOD}.github.read_json", return_value=payload):
            assert _evidence_asset_tags("o/r") == frozenset()

    def test_mixed_releases_single_call(self) -> None:
        payload = [
            {"tag_name": "v1.0.0", "assets": [{"name": "v1.0.0-ci-evidence.tar.gz"}]},
            {"tag_name": "v1.1.0", "assets": [{"name": "sbom.json"}]},
            {"tag_name": "v1.2.0", "assets": [{"name": "v1.2.0-ci-evidence.tar.gz"}]},
        ]
        with patch(f"{_MOD}.github.read_json", return_value=payload) as mock_read:
            assert _evidence_asset_tags("o/r") == frozenset({"v1.0.0", "v1.2.0"})
        mock_read.assert_called_once_with("api", "repos/o/r/releases", "--paginate")

    def test_non_list_payload_is_empty(self) -> None:
        with patch(f"{_MOD}.github.read_json", return_value={"message": "Not Found"}):
            assert _evidence_asset_tags("o/r") == frozenset()

    def test_non_dict_release_skipped(self) -> None:
        payload = [
            "junk",
            {"tag_name": "v1.0.0", "assets": [{"name": "v1.0.0-ci-evidence.tar.gz"}]},
        ]
        with patch(f"{_MOD}.github.read_json", return_value=payload):
            assert _evidence_asset_tags("o/r") == frozenset({"v1.0.0"})

    def test_non_str_tag_skipped(self) -> None:
        payload = [{"tag_name": None, "assets": [{"name": "x"}]}]
        with patch(f"{_MOD}.github.read_json", return_value=payload):
            assert _evidence_asset_tags("o/r") == frozenset()

    def test_non_list_assets_is_no_asset(self) -> None:
        payload = [{"tag_name": "v1.0.0", "assets": None}]
        with patch(f"{_MOD}.github.read_json", return_value=payload):
            assert _evidence_asset_tags("o/r") == frozenset()

    def test_non_dict_asset_skipped(self) -> None:
        payload = [
            {"tag_name": "v1.0.0", "assets": ["junk", {"name": "v1.0.0-ci-evidence.tar.gz"}]},
        ]
        with patch(f"{_MOD}.github.read_json", return_value=payload):
            assert _evidence_asset_tags("o/r") == frozenset({"v1.0.0"})

    def test_error_propagates(self) -> None:
        err = GitHubAPIError(1, "gh api", stderr="HTTP 500 boom")
        with (
            patch(f"{_MOD}.github.read_json", side_effect=err),
            pytest.raises(GitHubAPIError),
        ):
            _evidence_asset_tags("o/r")


class TestEvidenceResolver:
    def test_resolves_membership(self) -> None:
        with patch(f"{_MOD}._evidence_asset_tags", return_value=frozenset({"v1.0.0"})):
            resolve = _evidence_resolver("o/r")
            assert resolve("v1.0.0") is True
            assert resolve("v2.0.0") is False

    def test_caches_single_call(self) -> None:
        with patch(f"{_MOD}._evidence_asset_tags", return_value=frozenset({"v1.0.0"})) as mock_tags:
            resolve = _evidence_resolver("o/r")
            resolve("v1.0.0")
            resolve("v2.0.0")
            resolve("v1.0.0")
        mock_tags.assert_called_once_with("o/r")

    def test_lazy_no_call_when_unused(self) -> None:
        with patch(f"{_MOD}._evidence_asset_tags") as mock_tags:
            _evidence_resolver("o/r")
        mock_tags.assert_not_called()


class TestIsAuthError:
    def test_gh_token_message(self) -> None:
        exc = GitHubAPIError(1, "gh api", stderr="populate the GH_TOKEN environment variable")
        assert _is_auth_error(exc) is True

    def test_gh_auth_login_message(self) -> None:
        exc = GitHubAPIError(1, "gh api", stderr="please run: gh auth login")
        assert _is_auth_error(exc) is True

    def test_other_message(self) -> None:
        exc = GitHubAPIError(1, "gh api", stderr="HTTP 500 boom")
        assert _is_auth_error(exc) is False

    def test_no_stderr(self) -> None:
        exc = GitHubAPIError(1, "gh api", stderr=None)
        assert _is_auth_error(exc) is False
