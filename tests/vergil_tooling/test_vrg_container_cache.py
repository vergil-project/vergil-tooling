"""Tests for vergil_tooling.bin.vrg_container_cache CLI."""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import window_default_resolution
from vergil_tooling.bin.vrg_container_cache import main
from vergil_tooling.lib.cache_prune import CacheImage
from vergil_tooling.lib.live_branches import RepoSetError
from vergil_tooling.lib.versions.resolve import ResolutionError

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
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
        patch(_CLI + ".remove_images", return_value=[]) as m_remove,
    ):
        assert main(["clean"]) == 0
    m_remove.assert_called_once_with([cached[0]], "docker")
    assert "Removed:" in capsys.readouterr().out


def test_clean_fails_loudly_on_rmi_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed rmi names the image and the runtime's error and exits 1 (#3566)."""
    cached = ("img:1.26--feature-42--abcd1234", "abcd1234")
    with (
        patch("vergil_tooling.bin.vrg_container_cache.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_container_cache.git.current_branch",
            return_value="feature/42",
        ),
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.find_cached_image", return_value=cached),
        patch(_CLI + ".remove_images", return_value=[(cached[0], "image is in use")]),
    ):
        assert main(["clean"]) == 1
    out = capsys.readouterr()
    assert "Removed:" not in out.out
    assert f"error: could not remove {cached[0]}: image is in use" in out.err


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
        patch(_CLI + ".remove_images", return_value=[]) as m_remove,
    ):
        assert main(["clean-all"]) == 0
    m_remove.assert_called_once_with(["ghcr.io/r/dev-go:1.26--feat-42--abc"], "docker")
    assert "Removed 1 of 1 cached image(s)." in capsys.readouterr().out


def test_clean_all_fails_loudly_on_rmi_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Every image is attempted; each failure is named and the run exits 1 (#3566)."""
    mock_result = MagicMock(
        returncode=0,
        stdout="ghcr.io/r/dev-go:1.26--feat-1--abc\nghcr.io/r/dev-go:1.26--feat-2--def\n",
    )
    failure = ("ghcr.io/r/dev-go:1.26--feat-2--def", "conflict: image is in use")
    with (
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.subprocess.run", return_value=mock_result),
        patch(_CLI + ".remove_images", return_value=[failure]),
    ):
        assert main(["clean-all"]) == 1
    out = capsys.readouterr()
    assert "Removed 1 of 2 cached image(s)." in out.out
    assert f"error: could not remove {failure[0]}: {failure[1]}" in out.err


def test_clean_all_docker_error(capsys: pytest.CaptureFixture[str]) -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with (
        patch("vergil_tooling.bin.vrg_container_cache.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_cache.subprocess.run", return_value=mock_result),
    ):
        assert main(["clean-all"]) == 1


# -- prune subcommand (#3481) -------------------------------------------------

_CLI = "vergil_tooling.bin.vrg_container_cache"
_PY = "ghcr.io/vergil-project/prod-python"


def _prune_images() -> list[CacheImage]:
    return [
        CacheImage(f"{_PY}:3.14--develop--0123abcd", "id-dev", "develop"),
        CacheImage(f"{_PY}:3.14--feature-1-live--0123abcd", "id-live", "feature-1-live"),
        CacheImage(f"{_PY}:3.14--feature-2-gone--0123abcd", "id-gone", "feature-2-gone"),
        CacheImage(f"{_PY}:3.14--feature-3-used--0123abcd", "id-used", "feature-3-used"),
    ]


def _projects(tmp_path: Path) -> Path:
    (tmp_path / "org" / "a" / ".git").mkdir(parents=True)
    return tmp_path


@contextlib.contextmanager
def _prune_env(
    tmp_path: Path, *, removed: list[tuple[str, str]] | None = None
) -> Generator[MagicMock]:
    repo = (tmp_path / "org" / "a").resolve()
    with (
        patch(_CLI + ".detect_runtime", return_value="nerdctl"),
        patch(_CLI + ".default_repo_roots", return_value=[_projects(tmp_path)]),
        patch(_CLI + ".list_cache_images", return_value=_prune_images()),
        patch(_CLI + ".live_branches", return_value={repo: frozenset({"feature/1-live"})}),
        patch(_CLI + ".images_in_use", return_value={"id-used"}),
        patch(_CLI + ".remove_images", return_value=removed or []) as m_remove,
    ):
        yield m_remove


def test_prune_is_a_dry_run_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with _prune_env(tmp_path) as m_remove:
        assert main(["prune"]) == 0
    m_remove.assert_not_called()
    out = capsys.readouterr().out
    assert f"Repo set: 1 repo(s) under {tmp_path}" in out
    assert "keep    ghcr.io/vergil-project/prod-python:3.14--develop--0123abcd" in out
    assert "(protected branch 'develop')" in out
    assert "(branch 'feature-1-live' live in org/a)" in out
    assert "(in use by a container)" in out
    assert "remove  ghcr.io/vergil-project/prod-python:3.14--feature-2-gone--0123abcd" in out
    assert "Dry run: 1 of 4 cached image(s) would be removed." in out


def test_prune_apply_removes_only_finished_images(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _prune_env(tmp_path) as m_remove:
        assert main(["prune", "--apply"]) == 0
    m_remove.assert_called_once_with([f"{_PY}:3.14--feature-2-gone--0123abcd"], "nerdctl")
    assert "Removed 1 of 1 cached image(s)." in capsys.readouterr().out


def test_prune_apply_fails_loudly_on_rmi_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = f"{_PY}:3.14--feature-2-gone--0123abcd"
    with _prune_env(tmp_path, removed=[(ref, "image is being used")]):
        assert main(["prune", "--apply"]) == 1
    captured = capsys.readouterr()
    assert "Removed 0 of 1 cached image(s)." in captured.out
    assert f"error: could not remove {ref}: image is being used" in captured.err


def test_prune_root_replaces_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _projects(tmp_path / "explicit")
    with (
        _prune_env(tmp_path),
        patch(_CLI + ".default_repo_roots") as m_default,
    ):
        assert main(["prune", "--root", str(root)]) == 0
    m_default.assert_not_called()
    assert f"under {root}" in capsys.readouterr().out


def test_prune_refuses_an_empty_repo_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(_CLI + ".detect_runtime", return_value="docker"),
        patch(_CLI + ".list_cache_images") as m_list,
    ):
        assert main(["prune", "--root", str(tmp_path)]) == 1
    m_list.assert_not_called()
    assert "no git repositories under" in capsys.readouterr().err


def test_prune_reports_an_undeterminable_repo_set(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(_CLI + ".detect_runtime", return_value="docker"),
        patch(_CLI + ".default_repo_roots", side_effect=RepoSetError("pass --root DIR")),
    ):
        assert main(["prune"]) == 1
    assert "error: pass --root DIR" in capsys.readouterr().err


def test_prune_with_no_cached_images(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        _prune_env(tmp_path),
        patch(_CLI + ".list_cache_images", return_value=[]),
        patch(_CLI + ".live_branches") as m_live,
    ):
        assert main(["prune"]) == 0
    m_live.assert_not_called()
    assert "No cached per-branch images." in capsys.readouterr().out


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("'nerdctl ps' failed: boom"),
        subprocess.CalledProcessError(128, ("git", "ls-remote"), stderr="fatal: boom"),
    ],
)
def test_prune_stops_before_removing_when_a_lookup_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    with (
        _prune_env(tmp_path) as m_remove,
        patch(_CLI + ".live_branches", side_effect=error),
    ):
        assert main(["prune", "--apply"]) == 1
    m_remove.assert_not_called()
    assert "boom" in capsys.readouterr().err
