"""Tests for standard_tooling.bin.st_docker_cache CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from standard_tooling.bin.st_docker_cache import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_VALID_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "go"

[dependencies]
standard-tooling = "v1.4"

[ci]
versions = ["3.14"]
"""


# -- no subcommand ------------------------------------------------------------


def test_no_subcommand() -> None:
    assert main([]) == 1


# -- build subcommand ---------------------------------------------------------


def test_build_calls_ensure(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.assert_docker_available"),
        patch(
            "standard_tooling.bin.st_docker_cache.ensure_cached_image",
            return_value="img:cached",
        ),
    ):
        assert main(["build"]) == 0


# -- status subcommand --------------------------------------------------------


def test_status_no_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.git.current_branch", return_value="feature/42"),
        patch("standard_tooling.bin.st_docker_cache.find_cached_image", return_value=None),
    ):
        assert main(["status"]) == 0
    assert "No cached image" in capsys.readouterr().out


def test_status_with_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cached = ("img:1.26--feature-42--abcd1234", "abcd1234")
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.git.current_branch", return_value="feature/42"),
        patch("standard_tooling.bin.st_docker_cache.find_cached_image", return_value=cached),
    ):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "abcd1234" in out


# -- clean subcommand ---------------------------------------------------------


def test_clean_no_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.git.current_branch", return_value="feature/42"),
        patch("standard_tooling.bin.st_docker_cache.find_cached_image", return_value=None),
    ):
        assert main(["clean"]) == 0
    assert "No cached image" in capsys.readouterr().out


# -- clean-all subcommand -----------------------------------------------------


def test_clean_removes_existing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cached = ("img:1.26--feature-42--abcd1234", "abcd1234")
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.git.current_branch", return_value="feature/42"),
        patch("standard_tooling.bin.st_docker_cache.find_cached_image", return_value=cached),
        patch("standard_tooling.bin.st_docker_cache.subprocess.run"),
    ):
        assert main(["clean"]) == 0
    assert "Removed:" in capsys.readouterr().out


def test_build_no_caching(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.assert_docker_available"),
        patch(
            "standard_tooling.bin.st_docker_cache.ensure_cached_image",
            return_value="ghcr.io/r/dev-base:latest",
        ),
        patch(
            "standard_tooling.bin.st_docker_cache.default_image",
            return_value="ghcr.io/r/dev-base:latest",
        ),
    ):
        assert main(["build"]) == 0
    assert "No caching applied" in capsys.readouterr().out


# -- status subcommand (branches) --------------------------------------------


def test_status_no_cache_with_expected_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.git.current_branch", return_value="feature/42"),
        patch("standard_tooling.bin.st_docker_cache.find_cached_image", return_value=None),
        patch("standard_tooling.bin.st_docker_cache.detect_language", return_value="go"),
        patch(
            "standard_tooling.bin.st_docker_cache.default_image",
            return_value="ghcr.io/r/dev-go:1.26",
        ),
    ):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Expected tag:" in out


def test_status_current(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    from standard_tooling.lib.docker_cache import cache_sensitive_files, compute_cache_hash

    files = cache_sensitive_files(tmp_path, "go")
    h = compute_cache_hash(files)
    cached = (f"ghcr.io/r/dev-go:1.26--feature-42--{h}", h)
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.git.current_branch", return_value="feature/42"),
        patch("standard_tooling.bin.st_docker_cache.find_cached_image", return_value=cached),
        patch("standard_tooling.bin.st_docker_cache.detect_language", return_value="go"),
        patch(
            "standard_tooling.bin.st_docker_cache.default_image",
            return_value="ghcr.io/r/dev-go:1.26",
        ),
    ):
        assert main(["status"]) == 0
    assert "current" in capsys.readouterr().out


def test_status_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    cached = ("ghcr.io/r/dev-go:1.26--feature-42--oldold00", "oldold00")
    with (
        patch("standard_tooling.bin.st_docker_cache.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_docker_cache.git.current_branch", return_value="feature/42"),
        patch("standard_tooling.bin.st_docker_cache.find_cached_image", return_value=cached),
        patch("standard_tooling.bin.st_docker_cache.detect_language", return_value="go"),
        patch(
            "standard_tooling.bin.st_docker_cache.default_image",
            return_value="ghcr.io/r/dev-go:1.26",
        ),
    ):
        assert main(["status"]) == 0
    assert "stale" in capsys.readouterr().out


# -- clean-all subcommand -----------------------------------------------------


def test_clean_all(capsys: pytest.CaptureFixture[str]) -> None:
    mock_result = MagicMock(
        returncode=0,
        stdout="ghcr.io/r/dev-go:1.26--feat-42--abc\nghcr.io/r/dev-python:3.14\n",
    )
    with patch("standard_tooling.bin.st_docker_cache.subprocess.run", return_value=mock_result):
        assert main(["clean-all"]) == 0
    assert "1 cached image" in capsys.readouterr().out


def test_clean_all_docker_error(capsys: pytest.CaptureFixture[str]) -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("standard_tooling.bin.st_docker_cache.subprocess.run", return_value=mock_result):
        assert main(["clean-all"]) == 1
