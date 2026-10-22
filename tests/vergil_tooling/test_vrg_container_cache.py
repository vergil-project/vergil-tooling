"""Tests for vergil_tooling.bin.vrg_container_cache CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import window_default_resolution
from vergil_tooling.bin.vrg_container_cache import main
from vergil_tooling.lib.versions.resolve import ResolutionError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_VALID_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "go"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


@pytest.fixture(autouse=True)
def _marker_language_resolution() -> Iterator[None]:
    # Tests drop only a language marker (no vergil.toml); resolve the language
    # to its newest fixture-window version instead of reading a vergil.toml.
    with patch(
        "vergil_tooling.bin.vrg_container_cache.repo_resolution",
        side_effect=window_default_resolution,
    ):
        yield


# -- no subcommand ------------------------------------------------------------


def test_no_subcommand() -> None:
    assert main([]) == 1


# -- build subcommand ---------------------------------------------------------


def test_build_calls_ensure(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_cache.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.ensure_cached_image",
            return_value="img:cached",
        ),
    ):
        assert main(["build"]) == 0


_PY_ENV = {"UV_PYTHON": "3.14", "UV_PYTHON_PREFERENCE": "only-system"}


def test_build_passes_the_resolved_python_env(tmp_path: Path) -> None:
    # The cache build runs with the resolver's container env, as the run does (#3421).
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_cache.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.resolve_language", return_value="python"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.ensure_cached_image",
            return_value="img:cached",
        ) as ensure,
    ):
        assert main(["build"]) == 0
    assert ensure.call_args.kwargs["build_env"] == _PY_ENV


def test_status_hash_includes_the_resolved_python_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # status keys the cache exactly as ensure_cached_image does, env included.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "uv.lock").write_text("lock\n")
    from vergil_tooling.lib.container_cache import cache_sensitive_files, compute_cache_hash

    files = cache_sensitive_files(tmp_path, "python")
    h = compute_cache_hash(files, base_digest="sha256:abc", salt=tmp_path.name, env=_PY_ENV)
    cached = (f"ghcr.io/r/dev-python:3.14--feature-42--{h}", h)
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_cache.resolve_language", return_value="python"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.default_image",
            return_value="ghcr.io/r/dev-python:3.14",
        ),
    ):
        assert main(["status"]) == 0
    assert "Status:       current" in capsys.readouterr().out


def test_resolution_error_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.repo_resolution",
            side_effect=ResolutionError("pin 3.11 is outside the window"),
        ),
        patch("vergil_tooling.bin.vrg_container_cache.ensure_cached_image") as ensure,
    ):
        assert main(["build"]) == 1
    ensure.assert_not_called()
    assert "error: pin 3.11 is outside the window" in capsys.readouterr().err


# -- status subcommand --------------------------------------------------------


def test_status_no_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=None),
    ):
        assert main(["status"]) == 0
    assert "No cached image" in capsys.readouterr().out


def test_status_with_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cached = ("img:1.26--feature-42--abcd1234", "abcd1234")
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=cached),
    ):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "abcd1234" in out


# -- clean subcommand ---------------------------------------------------------


def test_clean_no_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=None),
    ):
        assert main(["clean"]) == 0
    assert "No cached image" in capsys.readouterr().out


# -- clean-all subcommand -----------------------------------------------------


def test_clean_removes_existing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cached = ("img:1.26--feature-42--abcd1234", "abcd1234")
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_cache.subprocess.run"),
    ):
        assert main(["clean"]) == 0
    assert "Removed:" in capsys.readouterr().out


def test_build_no_caching(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_cache.assert_runtime_available"),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.ensure_cached_image",
            return_value="ghcr.io/r/dev-base:latest",
        ),
        patch(
            "vergil_tooling.bin.vrg_container_cache.default_image",
            return_value="ghcr.io/r/dev-base:latest",
        ),
    ):
        assert main(["build"]) == 0
    assert "No caching applied" in capsys.readouterr().out


# -- status subcommand (branches) --------------------------------------------


def test_status_no_cache_with_expected_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=None),
        patch("vergil_tooling.bin.vrg_container_cache.resolve_language", return_value="go"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.default_image",
            return_value="ghcr.io/r/dev-go:1.26",
        ),
    ):
        assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Expected tag:" in out


def test_status_current(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    from vergil_tooling.lib.container_cache import cache_sensitive_files, compute_cache_hash

    files = cache_sensitive_files(tmp_path, "go")
    # status must compute the hash the same way ensure_cached_image does:
    # salt (repo name) + base digest.
    h = compute_cache_hash(files, base_digest="sha256:abc", salt=tmp_path.name)
    cached = (f"ghcr.io/r/dev-go:1.26--feature-42--{h}", h)
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_cache.resolve_language", return_value="go"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.default_image",
            return_value="ghcr.io/r/dev-go:1.26",
        ),
    ):
        assert main(["status"]) == 0
    assert "current" in capsys.readouterr().out


def test_status_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cached = ("ghcr.io/r/dev-go:1.26--feature-42--oldold00", "oldold00")
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_cache.resolve_language", return_value="go"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.default_image",
            return_value="ghcr.io/r/dev-go:1.26",
        ),
    ):
        assert main(["status"]) == 0
    assert "stale" in capsys.readouterr().out


def test_status_stale_when_base_digest_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    from vergil_tooling.lib.container_cache import cache_sensitive_files, compute_cache_hash

    files = cache_sensitive_files(tmp_path, "go")
    # Cache built against an OLD base digest; the live base now has a NEW digest.
    old_hash = compute_cache_hash(files, base_digest="sha256:OLD", salt=tmp_path.name)
    cached = (f"ghcr.io/r/dev-go:1.26--feature-42--{old_hash}", old_hash)
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.resolve_base_digest",
            return_value=("sha256:NEW", True),
        ),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=cached),
        patch("vergil_tooling.bin.vrg_container_cache.resolve_language", return_value="go"),
        patch(
            "vergil_tooling.bin.vrg_container_cache.default_image",
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
    with (
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.subprocess.run", return_value=mock_result),
    ):
        assert main(["clean-all"]) == 0
    assert "1 cached image" in capsys.readouterr().out


def test_clean_all_docker_error(capsys: pytest.CaptureFixture[str]) -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with (
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.subprocess.run", return_value=mock_result),
    ):
        assert main(["clean-all"]) == 1
