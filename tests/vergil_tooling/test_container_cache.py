"""Tests for vergil_tooling.lib.container_cache."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import fixture_tooling_sha, window_default_resolution
from tests.vergil_tooling.tooling_install_stubs import Run, run_lines, stub_bin, write_stub
from vergil_tooling.lib.container_cache import (
    WARMUP_SKIPPED_MARKER,
    CacheImageRef,
    CacheKey,
    _allow_stale_base,
    _build_cached_image,
    _compose_setup,
    _image_is_launchable,
    _is_transient_pull_error,
    _sanitize_branch,
    _warmup_command,
    _warmup_line,
    apt_install_command,
    cache_image_tag,
    cache_repo_name,
    cache_sensitive_files,
    compute_cache_hash,
    current_cache_key,
    ensure_cached_image,
    find_cached_image,
    language_sources,
    missing_warmup_files,
    parse_cache_image_ref,
    provision_dev_image,
    resolve_base_digest,
    sanitize_branch,
    sanitize_repo,
    unbootstrapped_manifests,
    vergil_tooling_setup_command,
)
from vergil_tooling.lib.tooling_install import (
    GIT_URL,
    ResolvedRef,
    ToolingInstallError,
    check_lines,
    install_lines,
    unresolved,
)
from vergil_tooling.lib.vm_packages import classify_ref

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vergil_tooling.lib.versions.resolve import Resolution


# The hermetic listing (conftest) resolves the pin v2.0 in _VALID_TOML to v2.0.7.
_SHA_207 = fixture_tooling_sha("v2.0.7")
_TOOLING = ResolvedRef(ref="v2.0", kind="line", tag="v2.0.7", sha=_SHA_207)


@pytest.fixture(autouse=True)
def _marker_language_resolution() -> Iterator[None]:
    # Tests drop only a language marker (no vergil.toml); resolve the language
    # to its newest fixture-window version instead of reading a vergil.toml.
    with patch(
        "vergil_tooling.lib.container_cache.repo_resolution", side_effect=window_default_resolution
    ):
        yield


@pytest.fixture(autouse=True)
def build_space() -> Iterator[MagicMock]:
    # The low-disk auto-prune reads the real image store; stub it out. Its own
    # behaviour is tested in test_cache_space.py (#3571).
    with patch("vergil_tooling.lib.cache_space.ensure_build_space") as mock_space:
        yield mock_space


@pytest.fixture(autouse=True)
def _repo_name() -> Iterator[None]:
    # The tests' repo roots are bare temp directories, not git repositories, so
    # name each by its directory, as a main clone's name would read. The real
    # lookup is tested directly through the cache_repo_name imported above.
    with patch(
        "vergil_tooling.lib.container_cache.cache_repo_name", side_effect=lambda root: root.name
    ):
        yield


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


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


def _bootstrap_go(root: Path) -> None:
    """Create the manifests the go warmup requires."""
    (root / "go.mod").write_text("module example.com/x\n")


def _bootstrap_python(root: Path) -> None:
    """Create the manifests the python warmup requires."""
    (root / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (root / "uv.lock").write_text("")


def _bootstrap_cpp(root: Path) -> None:
    """Create the manifests the cpp warmup requires.

    Includes conan.lock: the cpp warmup runs `conan install --lockfile=conan.lock`
    (#3021), so the lock is a warmup prerequisite exactly like python's uv.lock.
    """
    (root / "conanfile.txt").write_text("[generators]\nCMakeToolchain\n")
    (root / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")
    (root / "conan.lock").write_text("{}")


# -- cache_sensitive_files ----------------------------------------------------


def test_cache_files_python(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("lock\n")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "python")
    names = [f.name for f in files]
    assert "uv.lock" in names
    assert "vergil.toml" in names


def test_cache_files_go(tmp_path: Path) -> None:
    (tmp_path / "go.sum").write_text("sum\n")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "go")
    names = [f.name for f in files]
    assert "go.sum" in names
    assert "vergil.toml" in names


def test_cache_files_unknown_language(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "")
    assert len(files) == 1
    assert files[0].name == "vergil.toml"


def test_cache_files_missing_lockfile(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "go")
    assert len(files) == 1
    assert files[0].name == "vergil.toml"


# -- compute_cache_hash -------------------------------------------------------


def test_same_content_same_hash(tmp_path: Path) -> None:
    (tmp_path / "a.toml").write_text("x")
    (tmp_path / "b.toml").write_text("y")
    h1 = compute_cache_hash([tmp_path / "a.toml", tmp_path / "b.toml"])
    h2 = compute_cache_hash([tmp_path / "a.toml", tmp_path / "b.toml"])
    assert h1 == h2


def test_different_content_different_hash(tmp_path: Path) -> None:
    (tmp_path / "a.toml").write_text("x")
    h1 = compute_cache_hash([tmp_path / "a.toml"])
    (tmp_path / "a.toml").write_text("y")
    h2 = compute_cache_hash([tmp_path / "a.toml"])
    assert h1 != h2


def test_hash_is_8_chars(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("content")
    h = compute_cache_hash([tmp_path / "f"])
    assert len(h) == 8


# -- _sanitize_branch ---------------------------------------------------------


def test_sanitize_branch_slashes() -> None:
    assert _sanitize_branch("feature/362-decouple") == "feature-362-decouple"


def test_sanitize_branch_special_chars() -> None:
    assert _sanitize_branch("fix/a@b#c") == "fix-a-b-c"


# -- cache_image_tag ----------------------------------------------------------


def test_cache_image_tag_format() -> None:
    tag = cache_image_tag(
        "ghcr.io/vergil-project/dev-go:1.26",
        "feature/42-thing",
        "abcd1234",
        repo="vergil-tooling",
    )
    assert tag == "ghcr.io/vergil-project/dev-go:1.26--feature-42-thing--vergil-tooling.abcd1234"


def test_cache_image_tag_refuses_a_tag_too_long_for_the_runtime() -> None:
    # The part after ":" may hold at most 128 characters; say so before the build.
    with pytest.raises(ValueError, match="over the 128 a container runtime accepts"):
        cache_image_tag("ghcr.io/vergil-project/dev-go:1.26", "f" * 120, "abcd1234", repo="r")


def test_cache_image_tag_at_the_runtime_limit() -> None:
    # 4 + 2 + branch + 2 + 1 + 1 + 8 = 128 with a 110-character branch.
    tag = cache_image_tag("ghcr.io/vergil-project/dev-go:1.26", "f" * 110, "abcd1234", repo="r")
    assert len(tag.split(":", 1)[1]) == 128


# -- find_cached_image --------------------------------------------------------

_GO = "ghcr.io/vergil-project/dev-go"


def _find(stdout: str, repo: str = "repo-a", branch: str = "feature/42-thing") -> object:
    mock_result = MagicMock(returncode=0, stdout=stdout)
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        return find_cached_image(f"{_GO}:1.26", branch, repo=repo, runtime="docker")


def test_find_cached_image_hit() -> None:
    docker_output = f"{_GO}:1.26--feature-42-thing--repo-a.abcd1234\n{_GO}:1.26\n"
    assert _find(docker_output) == (f"{_GO}:1.26--feature-42-thing--repo-a.abcd1234", "abcd1234")


def test_find_cached_image_miss() -> None:
    assert _find("ghcr.io/vergil-project/dev-python:3.14\n") is None


def test_find_cached_image_returns_only_the_current_repos_image() -> None:
    # Another repository's image on the same base and branch comes first in the
    # listing; it is never returned, whatever the order (#4162).
    docker_output = (
        f"{_GO}:1.26--feature-42-thing--repo-b.11111111\n"
        f"{_GO}:1.26--feature-42-thing--repo-a.22222222\n"
        f"{_GO}:1.26--feature-42-thing--repo-ab.33333333\n"
    )
    assert _find(docker_output, repo="repo-a") == (
        f"{_GO}:1.26--feature-42-thing--repo-a.22222222",
        "22222222",
    )
    assert _find(docker_output, repo="repo-b") == (
        f"{_GO}:1.26--feature-42-thing--repo-b.11111111",
        "11111111",
    )
    assert _find(docker_output, repo="repo-c") is None


def test_find_cached_image_ignores_an_old_format_image() -> None:
    # An image from before #4162 names no repository, so it is no one's: it is
    # never returned (and so never removed by the build), whichever repo asks.
    assert _find(f"{_GO}:1.26--feature-42-thing--abcd1234\n") is None


def test_find_cached_image_matches_the_whole_branch() -> None:
    # "feature/42" must not match the image of "feature/42--thing".
    docker_output = f"{_GO}:1.26--feature-42--thing--repo-a.abcd1234\n"
    assert _find(docker_output, branch="feature/42") is None


def test_find_cached_image_sanitizes_the_repo_name() -> None:
    docker_output = f"{_GO}:1.26--feature-42-thing--odd-name.abcd1234\n"
    assert _find(docker_output, repo="odd--name") == (
        f"{_GO}:1.26--feature-42-thing--odd-name.abcd1234",
        "abcd1234",
    )


def test_find_cached_image_docker_error() -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        assert find_cached_image("img:1", "branch", repo="r", runtime="docker") is None


# -- cache_repo_name ----------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True
    )


def test_cache_repo_name_is_the_clone_from_every_worktree(tmp_path: Path) -> None:
    clone = tmp_path / "my-repo"
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "develop")
    _git(
        clone,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    worktree = clone / ".worktrees" / "issue-1-x"
    _git(clone, "worktree", "add", "-q", "-b", "feature/1-x", str(worktree))
    assert cache_repo_name(clone) == "my-repo"
    assert cache_repo_name(worktree) == "my-repo"


def test_cache_repo_name_outside_a_repository_raises(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        cache_repo_name(tmp_path)


# -- ensure_cached_image ------------------------------------------------------


def test_ensure_returns_base_for_python(tmp_path: Path) -> None:
    image = ensure_cached_image(tmp_path, "python", "img:1", runtime="docker", build_env={})
    assert image == "img:1"


def test_ensure_returns_base_when_no_files(tmp_path: Path) -> None:
    with patch("vergil_tooling.lib.git.current_branch", return_value="feature/42"):
        image = ensure_cached_image(tmp_path, "go", "img:1", runtime="docker", build_env={})
    assert image == "img:1"


def test_ensure_reuses_cache_when_hash_matches_and_launchable(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cached_tag = "ghcr.io/r/dev-go:1.26--feature-42--"
    files = cache_sensitive_files(tmp_path, "go")
    expected_hash = compute_cache_hash(
        files, base_digest="sha256:abc", salt=tmp_path.name, tooling="v2.0.7", tooling_sha=_SHA_207
    )
    full_tag = cached_tag + expected_hash

    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="feature/42"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch(
            "vergil_tooling.lib.container_cache.find_cached_image",
            return_value=(full_tag, expected_hash),
        ),
        patch(
            "vergil_tooling.lib.container_cache._image_is_launchable",
            return_value=True,
        ) as probe,
        patch("vergil_tooling.lib.container_cache._build_cached_image") as build,
    ):
        result = ensure_cached_image(
            tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker", build_env={}
        )
    assert result == full_tag
    # A metadata hit is reused only after the image is confirmed launchable.
    probe.assert_called_once()
    build.assert_not_called()


def test_ensure_rebuilds_when_cached_image_unlaunchable(tmp_path: Path) -> None:
    # A metadata cache hit whose underlying snapshot is orphaned: docker lists
    # the tag and the hash matches, but the image can no longer start a
    # container. The launch probe must catch it, force-remove the tag, and
    # rebuild rather than hand back an image that fails at run (issue #3016).
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cached_tag = "ghcr.io/r/dev-go:1.26--feature-42--"
    files = cache_sensitive_files(tmp_path, "go")
    expected_hash = compute_cache_hash(
        files, base_digest="sha256:abc", salt=tmp_path.name, tooling="v2.0.7", tooling_sha=_SHA_207
    )
    full_tag = cached_tag + expected_hash

    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="feature/42"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch(
            "vergil_tooling.lib.container_cache.find_cached_image",
            return_value=(full_tag, expected_hash),
        ),
        patch(
            "vergil_tooling.lib.container_cache._image_is_launchable",
            return_value=False,
        ),
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            return_value=_completed(0),
        ) as mock_run,
        patch(
            "vergil_tooling.lib.container_cache._build_cached_image",
            return_value="rebuilt:tag",
        ) as build,
    ):
        result = ensure_cached_image(
            tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker", build_env={}
        )
    assert result == "rebuilt:tag"
    build.assert_called_once()
    # The unlaunchable image was force-removed before rebuilding.
    assert mock_run.call_count == 1
    rmi_cmd = mock_run.call_args[0][0]
    assert rmi_cmd[:3] == ["docker", "rmi", "-f"]
    assert full_tag in rmi_cmd


def _ensure_cold_build(tmp_path: Path, build: MagicMock) -> str:
    """Run ensure_cached_image with no cached image, so it goes to build."""
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="feature/42"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.lib.container_cache.find_cached_image", return_value=None),
        patch("vergil_tooling.lib.container_cache._build_cached_image", build),
    ):
        return ensure_cached_image(
            tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker", build_env={}
        )


def test_ensure_checksbuild_space_before_building(tmp_path: Path, build_space: MagicMock) -> None:
    # The low-disk auto-prune runs just before a cache build (#3571).
    build = MagicMock(return_value="built:tag")
    build_space.side_effect = lambda _rt: build.assert_not_called()
    assert _ensure_cold_build(tmp_path, build) == "built:tag"
    build_space.assert_called_once_with("docker")
    build.assert_called_once()


def test_ensure_does_not_build_without_space(tmp_path: Path, build_space: MagicMock) -> None:
    from vergil_tooling.lib.cache_space import CacheSpaceError

    build = MagicMock()
    build_space.side_effect = CacheSpaceError("not enough disk space")
    with pytest.raises(CacheSpaceError, match="not enough disk space"):
        _ensure_cold_build(tmp_path, build)
    build.assert_not_called()


def test_ensure_skips_space_check_on_cache_hit(tmp_path: Path, build_space: MagicMock) -> None:
    test_ensure_reuses_cache_when_hash_matches_and_launchable(tmp_path)
    build_space.assert_not_called()


def test_image_is_launchable_true_when_create_succeeds() -> None:
    created = _completed(0, stdout="cid123\n")
    removed = _completed(0)
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[created, removed],
    ) as mock_run:
        assert _image_is_launchable("img:tag", runtime="docker") is True
    # Probes by *creating* (not running) a throwaway container, then removes it.
    assert mock_run.call_args_list[0][0][0][:2] == ["docker", "create"]
    assert mock_run.call_args_list[1][0][0][:3] == ["docker", "rm", "-f"]


def test_image_is_launchable_false_when_create_fails() -> None:
    failed = _completed(125, stderr="parent snapshot sha256:abc does not exist: not found")
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[failed],
    ) as mock_run:
        assert _image_is_launchable("img:tag", runtime="docker") is False
    # Nothing to remove when creation itself failed.
    assert mock_run.call_count == 1


def test_ensure_rebuilds_on_hash_mismatch(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    stale_tag = "ghcr.io/r/dev-go:1.26--feature-42--oldold00"
    new_tag = "ghcr.io/r/dev-go:1.26--feature-42--"

    with (
        patch("vergil_tooling.lib.git.current_branch") as mock_branch,
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch(
            "vergil_tooling.lib.container_cache.find_cached_image",
            return_value=(stale_tag, "oldold00"),
        ),
        patch("vergil_tooling.lib.container_cache.subprocess.run") as mock_run,
        patch(
            "vergil_tooling.lib.container_cache._build_cached_image",
        ) as mock_build,
    ):
        mock_branch.return_value = "feature/42"
        files = cache_sensitive_files(tmp_path, "go")
        expected_hash = compute_cache_hash(files)
        expected_tag = new_tag + expected_hash
        mock_build.return_value = expected_tag

        result = ensure_cached_image(
            tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker", build_env={}
        )
    assert result == expected_tag
    # Stale image should have been removed.
    mock_run.assert_called_once()
    assert stale_tag in mock_run.call_args[0][0]


def test_ensure_builds_on_cache_miss(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)

    with (
        patch("vergil_tooling.lib.git.current_branch") as mock_branch,
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch(
            "vergil_tooling.lib.container_cache.find_cached_image",
            return_value=None,
        ),
        patch(
            "vergil_tooling.lib.container_cache._build_cached_image",
            return_value="new:tag",
        ) as mock_build,
    ):
        mock_branch.return_value = "feature/42"
        result = ensure_cached_image(
            tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker", build_env={}
        )
    assert result == "new:tag"
    mock_build.assert_called_once()


def test_ensure_rebuilds_when_base_digest_changes(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "go")
    # The on-disk image was cached before digest-awareness: its hash was computed
    # WITHOUT any base digest. With the same dep files, the pre-digest code would
    # recompute that exact hash and reuse it. Once the base digest is keyed in, the
    # hash differs and the stale image must be rebuilt instead.
    legacy_hash = compute_cache_hash(files, salt=tmp_path.name)
    stale_tag = f"ghcr.io/r/dev-go:1.26--feature-42--{legacy_hash}"

    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="feature/42"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:NEW", True),
        ),
        patch(
            "vergil_tooling.lib.container_cache.find_cached_image",
            return_value=(stale_tag, legacy_hash),
        ),
        patch("vergil_tooling.lib.container_cache.subprocess.run") as mock_run,
        patch(
            "vergil_tooling.lib.container_cache._build_cached_image",
            return_value="rebuilt:tag",
        ) as mock_build,
    ):
        result = ensure_cached_image(
            tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker", build_env={}
        )

    assert result == "rebuilt:tag"
    mock_build.assert_called_once()
    # The stale image was removed.
    assert stale_tag in mock_run.call_args[0][0]


class _ImageStore:
    """A fake local image store for ensure_cached_image across runs (#4162).

    Answers ``images`` from its refs and ``rmi -f`` by dropping one, and records
    every removal; a build adds the tag it was asked for.
    """

    def __init__(self, refs: list[str]) -> None:
        self.refs = list(refs)
        self.removed: list[str] = []
        self.built: list[str] = []
        self.tooling: list[object] = []

    def run(self, cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "images":
            return _completed(0, stdout="".join(f"{ref}\n" for ref in self.refs))
        if cmd[1:3] == ["rmi", "-f"]:
            self.removed.append(cmd[3])
            self.refs.remove(cmd[3])
            return _completed(0)
        msg = f"unexpected runtime call: {cmd}"
        raise AssertionError(msg)

    def build(self, _root: Path, _lang: str, _base: str, target_tag: str, **kw: object) -> str:
        self.built.append(target_tag)
        resolved = kw.get("tooling")
        self.tooling.append(resolved.tag if isinstance(resolved, ResolvedRef) else resolved)
        self.refs.append(target_tag)
        return target_tag


def _run_in(store: _ImageStore, repo_root: Path, branch: str = "develop") -> str:
    """One ensure_cached_image run of *repo_root* against *store*."""
    with (
        patch("vergil_tooling.lib.git.current_branch", return_value=branch),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=store.run),
        patch("vergil_tooling.lib.container_cache._image_is_launchable", return_value=True),
        patch("vergil_tooling.lib.container_cache._build_cached_image", side_effect=store.build),
    ):
        return ensure_cached_image(
            repo_root,
            "go",
            "ghcr.io/vergil-project/prod-base:latest",
            runtime="docker",
            build_env={},
        )


def _two_repos(tmp_path: Path) -> tuple[Path, Path]:
    repo_a, repo_b = tmp_path / "repo-a", tmp_path / "repo-b"
    for repo in (repo_a, repo_b):
        repo.mkdir()
        (repo / "vergil.toml").write_text(_VALID_TOML)
    return repo_a, repo_b


def test_two_repos_on_one_base_and_branch_keep_their_own_images(tmp_path: Path) -> None:
    # Both repos build on prod-base:latest on develop. Before #4162 each run
    # found the other's image, saw a hash mismatch, removed it and rebuilt.
    repo_a, repo_b = _two_repos(tmp_path)
    store = _ImageStore([])

    image_a = _run_in(store, repo_a)
    image_b = _run_in(store, repo_b)
    assert image_a != image_b
    assert image_a.endswith(f"--develop--repo-a.{image_a[-8:]}")
    assert image_b.endswith(f"--develop--repo-b.{image_b[-8:]}")

    # Run each again, in both orders: every run reuses its own image.
    assert _run_in(store, repo_a) == image_a
    assert _run_in(store, repo_b) == image_b
    assert _run_in(store, repo_b) == image_b
    assert _run_in(store, repo_a) == image_a

    assert store.built == [image_a, image_b]
    assert store.removed == []
    assert sorted(store.refs) == sorted([image_a, image_b])


def test_a_stale_image_is_replaced_without_touching_the_other_repos(tmp_path: Path) -> None:
    # repo-a's cache key moves: the build removes repo-a's old image only.
    repo_a, repo_b = _two_repos(tmp_path)
    store = _ImageStore([])
    image_a = _run_in(store, repo_a)
    image_b = _run_in(store, repo_b)

    (repo_a / "go.sum").write_text("changed\n")
    new_a = _run_in(store, repo_a)

    assert new_a != image_a
    assert store.removed == [image_a]
    assert sorted(store.refs) == sorted([new_a, image_b])
    assert _run_in(store, repo_b) == image_b


def test_an_old_format_image_is_left_alone_and_not_reused(tmp_path: Path) -> None:
    # An image from before #4162 (no repository in its tag) is never found, so
    # the repo builds its own and neither run removes the old one: that is
    # plan_prune's to remove (see test_cache_prune.py).
    repo_a, repo_b = _two_repos(tmp_path)
    legacy = "ghcr.io/vergil-project/prod-base:latest--develop--2fb5421a"
    store = _ImageStore([legacy])

    image_a = _run_in(store, repo_a)
    image_b = _run_in(store, repo_b)

    assert legacy not in (image_a, image_b)
    assert store.removed == []
    assert sorted(store.refs) == sorted([legacy, image_a, image_b])


# -- provision_dev_image ------------------------------------------------------


def test_provision_uses_env_override(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {"DOCKER_DEV_IMAGE": "custom:img"}, clear=True),
        patch("vergil_tooling.lib.container_cache.ensure_cached_image") as ensure,
    ):
        image, source = provision_dev_image(tmp_path, "python")
    assert (image, source) == ("custom:img", "env")
    # The env override short-circuits — no image is built.
    ensure.assert_not_called()


def test_provision_returns_cached_when_built(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container_cache.default_image", return_value="base:1"),
        patch(
            "vergil_tooling.lib.container_cache.ensure_cached_image",
            return_value="base:1--develop--abcd1234",
        ),
    ):
        image, source = provision_dev_image(tmp_path, "python", runtime="docker")
    assert (image, source) == ("base:1--develop--abcd1234", "cached")


def test_provision_uses_the_resolved_image(tmp_path: Path) -> None:
    # The resolver's answer picks the image, so provisioning warms the same
    # image vrg-container-run selects (epic vergil-project/.github#407).
    resolution = window_default_resolution(tmp_path, "python")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(
            "vergil_tooling.lib.container_cache.repo_resolution", return_value=resolution
        ) as resolve,
        patch(
            "vergil_tooling.lib.container_cache.default_image", return_value="base:1"
        ) as default_img,
        patch("vergil_tooling.lib.container_cache.ensure_cached_image", return_value="base:1"),
    ):
        provision_dev_image(tmp_path, "python", runtime="docker")
    resolve.assert_called_once_with(tmp_path, "python")
    default_img.assert_called_once_with(resolution, fallback=True, prefix="prod")


def test_provision_returns_default_when_no_cache_files(tmp_path: Path) -> None:
    # ensure_cached_image returns the base unchanged when the repo declares no
    # cache-sensitive files, so the source is the plain base image.
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container_cache.default_image", return_value="base:1"),
        patch(
            "vergil_tooling.lib.container_cache.ensure_cached_image",
            return_value="base:1",
        ),
    ):
        image, source = provision_dev_image(tmp_path, "python")
    assert (image, source) == ("base:1", "default")


# -- _build_cached_image ------------------------------------------------------


def test_build_cached_image_success(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    start_result = MagicMock(returncode=0)
    commit_result = MagicMock(returncode=0)
    rm_result = MagicMock(returncode=0)

    calls: list[list[str]] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        calls.append(cmd)
        if cmd[1] == "create":
            return create_result
        if cmd[1] == "start":
            return start_result
        if cmd[1] == "commit":
            return commit_result
        if cmd[1] == "rm":
            return rm_result
        return MagicMock(returncode=0)

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        result = _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    assert result == "img:1--branch--hash"


def test_build_cached_image_cleanup_reclaims_anonymous_volumes(tmp_path: Path) -> None:
    """Cleanup `rm` must pass `-v` so the anonymous venv mask volume is reclaimed.

    Without `-v`, `nerdctl volume ls` grows by one per cold build because the
    `/workspace/.venv` mask volume orphans (issue #2500).
    """
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    rm_cmd: list[str] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return create_result
        if cmd[1] == "rm":
            rm_cmd.extend(cmd)
        return MagicMock(returncode=0)

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )

    assert rm_cmd[:3] == ["docker", "rm", "-v"]
    assert "abc123" in rm_cmd


def test_build_cached_image_includes_platform(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)
    create_cmd: list[str] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            create_cmd.extend(cmd)
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    assert any(a.startswith("--platform=linux/") for a in create_cmd)


def _capture_create_cmd(tmp_path: Path, lang: str) -> list[str]:
    """Run _build_cached_image and return the `create` command it issued."""
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)
    create_cmd: list[str] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            create_cmd.extend(cmd)
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            lang,
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    return create_cmd


def test_build_cached_image_masks_venv_for_python(tmp_path: Path) -> None:
    # The cache-build (cold-rebuild) path masks the bind-mounted host `.venv`
    # for a Python repo, so its `setup` step can never corrupt the host venv —
    # the second mount site the run-path mask (#2486) missed (#2495). The mask
    # keys off the asserted primary-language (#2858), so the repo declares python.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML.replace('"go"', '"python"'))
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    create_cmd = _capture_create_cmd(tmp_path, "python")
    assert "/workspace/.venv" in create_cmd
    idx = create_cmd.index("/workspace/.venv")
    assert create_cmd[idx - 1] == "-v"


def test_build_cached_image_omits_venv_mask_for_non_python(tmp_path: Path) -> None:
    # A non-Python repo has no host `.venv`, so the cache-build create args
    # add no mask (#2495).
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "go.mod").write_text("module example\n")
    create_cmd = _capture_create_cmd(tmp_path, "go")
    assert "/workspace/.venv" not in create_cmd


def test_build_cached_image_create_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=1, stderr="no space")
    with (
        patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=create_result),
        pytest.raises(RuntimeError, match="Failed to create container"),
    ):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )


def test_build_cached_image_start_fails(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    start_result = MagicMock(returncode=1)
    rm_result = MagicMock(returncode=0)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return create_result
        if cmd[1] == "start":
            return start_result
        return rm_result

    with (
        patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run),
        pytest.raises(RuntimeError, match="Cache build failed"),
    ):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )


def test_build_cached_image_streams_warmup_by_default(tmp_path: Path) -> None:
    """The default (interactive) path streams warmup output — no capture_output.

    A cold rebuild under ``vrg-container-run`` must stream so a multi-minute
    build never looks hung; only the finalize path quiets it (#2906).
    """
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    start_kwargs: dict[str, object] = {}

    def mock_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return MagicMock(returncode=0, stdout="abc123\n")
        if cmd[1] == "start":
            start_kwargs.update(kwargs)
        return MagicMock(returncode=0)

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )

    # Not captured → streamed to the inherited terminal.
    assert start_kwargs.get("capture_output") is False


def test_build_cached_image_quiet_warmup_captures(tmp_path: Path) -> None:
    """quiet_warmup captures the warmup subprocess instead of streaming it."""
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    start_kwargs: dict[str, object] = {}

    def mock_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return MagicMock(returncode=0, stdout="abc123\n")
        if cmd[1] == "start":
            start_kwargs.update(kwargs)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            quiet_warmup=True,
            build_env={},
            tooling=_TOOLING,
        )

    assert start_kwargs.get("capture_output") is True


def test_build_cached_image_quiet_warmup_surfaces_output_on_failure(tmp_path: Path) -> None:
    """A captured warmup failure folds its output into the error (#2906).

    Otherwise a provisioning failure surfaces with no diagnostic under the
    finalize progress display, which does not stream the warmup.
    """
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return MagicMock(returncode=0, stdout="abc123\n")
        if cmd[1] == "start":
            return MagicMock(returncode=1, stdout="conan: package not found", stderr="boom")
        return MagicMock(returncode=0)

    with (
        patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run),
        pytest.raises(RuntimeError, match="conan: package not found"),
    ):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            quiet_warmup=True,
            build_env={},
            tooling=_TOOLING,
        )


def test_build_cached_image_quiet_warmup_failure_without_output(tmp_path: Path) -> None:
    """A captured failure with no output falls back to the bare message (#2906)."""
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return MagicMock(returncode=0, stdout="abc123\n")
        if cmd[1] == "start":
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0)

    with (
        patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run),
        pytest.raises(RuntimeError, match=r"^Cache build failed$"),
    ):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            quiet_warmup=True,
            build_env={},
            tooling=_TOOLING,
        )


def test_build_cached_image_warmup_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    out = capsys.readouterr().out
    assert "Warmup:" in out


def test_build_cached_image_no_warmup_for_unknown_lang(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "unknown",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    out = capsys.readouterr().out
    assert "Warmup:" not in out


def test_build_cached_image_uses_the_uv_install(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)
    create_cmd: list[str] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            create_cmd.extend(cmd)
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    setup_cmd = create_cmd[-1]
    assert setup_cmd == vergil_tooling_setup_command(_TOOLING)
    assert "uv tool install --force --no-config --python 3.15 --managed-python" in setup_cmd
    assert "apt" not in setup_cmd
    assert "pip install" not in setup_cmd


# -- compute_cache_hash salt --------------------------------------------------


def test_compute_cache_hash_differs_with_different_salt(tmp_path: Path) -> None:
    (tmp_path / "f.toml").write_text("same content")
    h1 = compute_cache_hash([tmp_path / "f.toml"], salt="repo-a")
    h2 = compute_cache_hash([tmp_path / "f.toml"], salt="repo-b")
    assert h1 != h2


def test_compute_cache_hash_same_salt_is_stable(tmp_path: Path) -> None:
    (tmp_path / "f.toml").write_text("content")
    h1 = compute_cache_hash([tmp_path / "f.toml"], salt="my-repo")
    h2 = compute_cache_hash([tmp_path / "f.toml"], salt="my-repo")
    assert h1 == h2


def test_compute_cache_hash_no_salt_matches_empty_salt(tmp_path: Path) -> None:
    (tmp_path / "f.toml").write_text("content")
    assert compute_cache_hash([tmp_path / "f.toml"]) == compute_cache_hash(
        [tmp_path / "f.toml"], salt=""
    )


# -- Python caching -----------------------------------------------------------


def test_ensure_python_builds_cached_image(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("lock\n")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)

    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="develop"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.lib.container_cache.find_cached_image", return_value=None),
        patch(
            "vergil_tooling.lib.container_cache._build_cached_image",
            return_value="img:1--develop--hash",
        ) as mock_build,
    ):
        result = ensure_cached_image(tmp_path, "python", "img:1", runtime="docker", build_env={})
    mock_build.assert_called_once()
    assert result != "img:1"


def test_build_cached_image_python_uses_the_uv_install(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_python(tmp_path)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)
    create_cmd: list[str] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            create_cmd.extend(cmd)
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "python",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    setup_cmd = create_cmd[-1]
    assert setup_cmd.startswith(vergil_tooling_setup_command(_TOOLING) + " && ")
    assert "apt-get" not in setup_cmd
    assert "uv sync --frozen --group dev" in setup_cmd


def test_ensure_repo_name_included_in_hash(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-alpha"
    repo_b = tmp_path / "repo-beta"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "vergil.toml").write_text(_VALID_TOML)
    (repo_b / "vergil.toml").write_text(_VALID_TOML)

    built_tags: list[str] = []

    def capture_build(
        repo_root: Path,
        lang: str,
        base_image: str,
        target_tag: str,
        *,
        runtime: str = "",
        quiet_warmup: bool = False,
        build_env: dict[str, str],
        tooling: ResolvedRef | None,
    ) -> str:
        built_tags.append(target_tag)
        return target_tag

    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="develop"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.lib.container_cache.find_cached_image", return_value=None),
        patch("vergil_tooling.lib.container_cache._build_cached_image", side_effect=capture_build),
    ):
        ensure_cached_image(repo_a, "go", "img:1", runtime="docker", build_env={})
        ensure_cached_image(repo_b, "go", "img:1", runtime="docker", build_env={})

    assert len(built_tags) == 2
    assert built_tags[0] != built_tags[1], "repos with identical files must get distinct image tags"


# -- _build_cached_image self-repo skip ----------------------------------------


def test_build_cached_image_self_repo_skips_uv_install(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    (tmp_path / "uv.lock").write_text("")
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)
    create_cmd: list[str] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            create_cmd.extend(cmd)
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "python",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    setup_cmd = create_cmd[-1]
    assert "uv tool install" not in setup_cmd
    assert "uv sync --frozen --group dev" in setup_cmd


# -- _build_cached_image pull policy ------------------------------------------


def test_build_cached_image_uses_pull_missing(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create = MagicMock(returncode=0, stdout="cid123\n")
    start = MagicMock(returncode=0)
    commit = MagicMock(returncode=0)
    rm = MagicMock(returncode=0)
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[create, start, commit, rm],
    ) as mock_run:
        _build_cached_image(
            tmp_path,
            "go",
            "ghcr.io/r/dev-go:1.26",
            "target:tag",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    create_argv = mock_run.call_args_list[0][0][0]
    assert "create" in create_argv
    assert "--pull=missing" in create_argv


# -- compute_cache_hash base digest -------------------------------------------


def test_compute_cache_hash_changes_with_base_digest(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "go")
    h1 = compute_cache_hash(files, base_digest="sha256:aaa", salt="r")
    h2 = compute_cache_hash(files, base_digest="sha256:bbb", salt="r")
    assert h1 != h2


def test_compute_cache_hash_stable_for_same_inputs(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "go")
    h1 = compute_cache_hash(files, base_digest="sha256:aaa", salt="r")
    h2 = compute_cache_hash(files, base_digest="sha256:aaa", salt="r")
    assert h1 == h2


# -- apt_install_command speller (epic vergil-project/.github#272) -------------


def test_apt_install_command_empty_is_blank() -> None:
    assert apt_install_command([], "linux/arm64") == ""


def test_apt_install_command_updates_then_installs_each_package() -> None:
    cmd = apt_install_command(["lilypond", "fluidsynth"], "linux/arm64")
    assert "apt-get update" in cmd
    assert "--no-install-recommends" in cmd
    # per-package install so the failing package is named
    assert "lilypond" in cmd
    assert "fluidsynth" in cmd


def test_apt_install_command_fail_closed_names_package_and_arch() -> None:
    cmd = apt_install_command(["boguspkg"], "linux/arm64")
    assert "boguspkg" in cmd
    assert "linux/arm64" in cmd
    assert "not installable" in cmd
    assert "exit 1" in cmd


def test_cache_hash_changes_when_system_packages_change(tmp_path: Path) -> None:
    # The §3.6 invariant: vergil.toml is cache-sensitive, so editing the package
    # list changes the hash and forces a rebuild.
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "vergil.toml").write_text(
        _VALID_TOML + '[container]\nenv-prefixes = []\nsystem-packages = ["lilypond"]\n'
    )
    (b / "vergil.toml").write_text(
        _VALID_TOML
        + '[container]\nenv-prefixes = []\nsystem-packages = ["lilypond", "fluidsynth"]\n'
    )
    assert compute_cache_hash(cache_sensitive_files(a, "go")) != compute_cache_hash(
        cache_sensitive_files(b, "go")
    )


# -- _build_cached_image bakes declared system-packages -----------------------


def test_build_cached_image_prepends_apt_install_to_setup(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + '[container]\nenv-prefixes = []\nsystem-packages = ["lilypond"]\n'
    )
    create_cmd = _capture_create_cmd(tmp_path, "go")
    setup_cmd = create_cmd[-1]
    assert "apt-get update" in setup_cmd
    assert "lilypond" in setup_cmd
    # the apt snippet precedes the vergil-tooling install / warmup
    assert setup_cmd.index("apt-get update") < setup_cmd.index("vergil-tooling: installed v2.0.7")


def test_build_cached_image_no_apt_when_no_system_packages(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_cmd = _capture_create_cmd(tmp_path, "go")
    # No system-packages fragment, and the vergil-tooling install reads no apt.
    assert "apt-get update" not in create_cmd[-1]
    assert "--no-install-recommends" not in create_cmd[-1]


def test_build_cached_image_prints_packages_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + '[container]\nenv-prefixes = []\nsystem-packages = ["lilypond"]\n'
    )
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    out = capsys.readouterr().out
    assert "Packages:" in out
    assert "lilypond" in out


# -- resolve_base_digest ------------------------------------------------------


def test_resolve_base_digest_pull_ok() -> None:
    pull = _completed(0)
    inspect = _completed(0, "sha256:abc123\n")
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[pull, inspect],
    ):
        digest, verified = resolve_base_digest("img:1", runtime="docker")
    assert digest == "sha256:abc123"
    assert verified is True


def test_resolve_base_digest_pull_failure_is_hard_error() -> None:
    """By default a failed pull is a hard error, even with a local copy present."""
    pull = _completed(1, stderr="unauthorized: stale credential")
    inspect = _completed(0, "sha256:local9\n")  # local copy present
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[pull, inspect],
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        resolve_base_digest("img:1", runtime="docker")
    message = str(exc.value)
    # Names the real cause and the stale-cache risk, and points at the opt-in.
    assert "unauthorized: stale credential" in message
    assert "possibly-stale local cache" in message
    assert "VRG_ALLOW_STALE_BASE" in message


def test_resolve_base_digest_pull_failure_no_stderr_uses_fallback() -> None:
    pull = _completed(7, stderr="")
    inspect = _completed(0, "sha256:local9\n")
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[pull, inspect],
        ),
        pytest.raises(RuntimeError, match="unknown error"),
    ):
        resolve_base_digest("img:1", runtime="docker")


def test_resolve_base_digest_allow_stale_uses_local(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The opt-in degrades to the local base, warning with the real cause."""
    pull = _completed(1, stderr="dial tcp 140.82.112.33:443: connect: connection refused")
    inspect = _completed(0, "sha256:local9\n")
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[pull, pull, pull, inspect],
    ):
        digest, verified = resolve_base_digest(
            "img:1", runtime="docker", allow_stale=True, sleep=lambda _s: None
        )
    assert digest == "sha256:local9"
    assert verified is False
    err = capsys.readouterr().err
    assert "could not verify base image freshness" in err
    assert "connection refused" in err


def test_resolve_base_digest_allow_stale_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VRG_ALLOW_STALE_BASE", "1")
    pull = _completed(1, stderr="offline")
    inspect = _completed(0, "sha256:local9\n")
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[pull, inspect],
    ):
        digest, verified = resolve_base_digest("img:1", runtime="docker")
    assert digest == "sha256:local9"
    assert verified is False


def test_resolve_base_digest_pull_failure_reports_real_error() -> None:
    """The hard error surfaces the real pull cause, not a guessed '(offline?)'."""
    pull = _completed(
        1,
        stderr="Error response from daemon: error from registry: denied\n",
    )
    inspect = _completed(0, "sha256:local9\n")  # local copy present
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[pull, inspect],
        ),
        pytest.raises(RuntimeError) as exc,
    ):
        resolve_base_digest("img:1", runtime="docker")
    message = str(exc.value)
    assert "denied" in message  # the real cause, surfaced
    assert "(offline?)" not in message  # not a misleading guess


def test_resolve_base_digest_pull_timeout_is_hard_error() -> None:
    """A pull timing out is transient: retried, then refused once every attempt timed out."""
    timeout = subprocess.TimeoutExpired(cmd="pull", timeout=1)
    inspect = _completed(0, "sha256:local9\n")
    waits: list[float] = []
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[timeout, timeout, timeout, inspect],
        ) as run,
        pytest.raises(RuntimeError, match="timed out after") as exc,
    ):
        resolve_base_digest("img:1", runtime="docker", sleep=waits.append)
    assert run.call_count == 4  # three pulls, then the inspect
    assert waits == [2.0, 5.0]
    assert "3 attempts" in str(exc.value)


def test_resolve_base_digest_pull_timeout_then_success() -> None:
    timeout = subprocess.TimeoutExpired(cmd="pull", timeout=1)
    inspect = _completed(0, "sha256:fresh1\n")
    waits: list[float] = []
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[timeout, _completed(0), inspect],
    ):
        digest, verified = resolve_base_digest("img:1", runtime="docker", sleep=waits.append)
    assert (digest, verified) == ("sha256:fresh1", True)
    assert waits == [2.0]


def test_resolve_base_digest_pull_timeout_allow_stale_uses_local() -> None:
    timeout = subprocess.TimeoutExpired(cmd="pull", timeout=1)
    inspect = _completed(0, "sha256:local9\n")
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[timeout, timeout, timeout, inspect],
    ):
        digest, verified = resolve_base_digest(
            "img:1", runtime="docker", allow_stale=True, sleep=lambda _s: None
        )
    assert digest == "sha256:local9"
    assert verified is False


# The failure #4209 hit: docker's containerd resolver could not fetch ghcr.io's
# anonymous token because the token endpoint did not answer in time.
_TOKEN_TIMEOUT_STDERR = (
    'Error response from daemon: failed to resolve reference "ghcr.io/vergil-project/'
    'prod-python:3.15": failed to fetch anonymous token: Get "https://ghcr.io/token?'
    'scope=repository%3Avergil-project%2Fprod-python%3Apull&service=ghcr.io": '
    "net/http: timeout awaiting response headers\n"
)


def test_resolve_base_digest_transient_retried_then_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect = _completed(0, "sha256:fresh1\n")
    waits: list[float] = []
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[
            _completed(1, stderr=_TOKEN_TIMEOUT_STDERR),
            _completed(1, stderr=_TOKEN_TIMEOUT_STDERR),
            _completed(0),
            inspect,
        ],
    ) as run:
        digest, verified = resolve_base_digest("img:1", runtime="docker", sleep=waits.append)
    assert (digest, verified) == ("sha256:fresh1", True)
    assert waits == [2.0, 5.0]
    pulls = [c.args[0] for c in run.call_args_list[:3]]
    assert pulls == [["docker", "pull", "img:1"]] * 3
    err = capsys.readouterr().err
    assert "attempt 1/3" in err
    assert "attempt 2/3" in err
    assert "retrying in 2s" in err
    assert "retrying in 5s" in err


def test_resolve_base_digest_transient_exhausted_names_every_attempt() -> None:
    inspect = _completed(0, "sha256:local9\n")
    waits: list[float] = []
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[
                _completed(1, stderr=_TOKEN_TIMEOUT_STDERR),
                _completed(1, stderr="dial tcp: lookup ghcr.io: no such host\n"),
                _completed(
                    1,
                    stderr="unexpected status from HEAD request to https://ghcr.io/v2/x/"
                    "manifests/1: 503 Service Unavailable\n",
                ),
                inspect,
            ],
        ) as run,
        pytest.raises(RuntimeError) as exc,
    ):
        resolve_base_digest("img:1", runtime="docker", sleep=waits.append)
    assert run.call_count == 4
    assert waits == [2.0, 5.0]
    message = str(exc.value)
    assert "base pull failed (3 attempts:" in message
    assert "attempt 1: " in message
    assert "timeout awaiting response headers" in message
    assert "attempt 2: dial tcp: lookup ghcr.io: no such host" in message
    assert "attempt 3: " in message
    assert "503 Service Unavailable" in message
    assert "possibly-stale local cache" in message
    assert "VRG_ALLOW_STALE_BASE" in message


@pytest.mark.parametrize(
    "stderr",
    [
        "Error response from daemon: manifest for ghcr.io/x/y:1 not found: "
        "manifest unknown: manifest unknown",
        "Error response from daemon: pull access denied for x/y, repository does not "
        "exist or may require 'docker login': denied: requested access to the resource "
        "is denied",
        'Error response from daemon: Head "https://ghcr.io/v2/x/y/manifests/1": '
        "unauthorized: authentication required",
        'failed to resolve reference "ghcr.io/x/y:1": ghcr.io/x/y:1: not found',
        # A definite answer inside a token fetch: a 401 is not transient.
        "failed to fetch anonymous token: unexpected status from GET request to "
        "https://ghcr.io/token?scope=x: 401 Unauthorized",
        # Unrecognised text is not transient either.
        "something nobody has seen before",
        "unexpected status from GET request to https://ghcr.io/v2/: 429 Too Many Requests",
    ],
)
def test_resolve_base_digest_definite_failure_fails_at_once(stderr: str) -> None:
    inspect = _completed(0, "sha256:local9\n")
    waits: list[float] = []
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[_completed(1, stderr=stderr), inspect],
        ) as run,
        pytest.raises(RuntimeError, match=r"base pull failed \(1 attempt: "),
    ):
        resolve_base_digest("img:1", runtime="docker", sleep=waits.append)
    assert run.call_count == 2  # one pull, then the inspect
    assert waits == []


def test_resolve_base_digest_default_sleep_is_time_sleep() -> None:
    timeout = subprocess.TimeoutExpired(cmd="pull", timeout=1)
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[timeout, _completed(0), _completed(0, "sha256:fresh1\n")],
        ),
        patch("vergil_tooling.lib.container_cache.time.sleep") as sleep,
    ):
        resolve_base_digest("img:1", runtime="docker")
    sleep.assert_called_once_with(2.0)


# -- _is_transient_pull_error -------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "transient"),
    [
        (_TOKEN_TIMEOUT_STDERR, True),
        ('Get "https://ghcr.io/v2/": net/http: TLS handshake timeout', True),
        ("net/http: request canceled while waiting for connection", True),
        ("dial tcp 140.82.112.33:443: i/o timeout", True),
        ("failed to do request: context deadline exceeded", True),
        ("dial tcp: lookup ghcr.io on 127.0.0.53:53: no such host", True),
        ("dial tcp: lookup ghcr.io on 127.0.0.11:53: server misbehaving", True),
        ("dial tcp: lookup ghcr.io: Temporary failure in name resolution", True),
        ("dial tcp 140.82.112.33:443: connect: connection refused", True),
        ("read tcp 10.0.0.2:5000->140.82.112.33:443: read: connection reset by peer", True),
        ("dial tcp 140.82.112.33:443: connect: network is unreachable", True),
        ("dial tcp 140.82.112.33:443: connect: no route to host", True),
        ("httpReadSeeker: failed open: unexpected EOF", True),
        ("unexpected status from HEAD request to https://ghcr.io/v2/x: 500 Server Error", True),
        ("unexpected status from GET request to https://ghcr.io/token: 502 Bad Gateway", True),
        ("received unexpected HTTP status: 504 Gateway Timeout", True),
        ("unavailable: service unavailable", True),
        ("manifest unknown", False),
        ("denied: requested access to the resource is denied", False),
        ("unauthorized: authentication required", False),
        ("ghcr.io/x/y:1: not found", False),
        (
            "pull access denied, repository does not exist or may require authorization: "
            "authorization failed",
            False,
        ),
        # A definite answer wins over a transient-looking wrapper.
        (
            "failed to fetch oauth token: unexpected status from POST request to "
            "https://ghcr.io/token: 401 Unauthorized",
            False,
        ),
        ("received unexpected HTTP status: 429 Too Many Requests", False),
        ("x509: certificate signed by unknown authority", False),
        ("", False),
    ],
)
def test_is_transient_pull_error(stderr: str, transient: bool) -> None:
    assert _is_transient_pull_error(stderr) is transient


def test_resolve_base_digest_no_image_raises_names_cause() -> None:
    pull = _completed(1, stderr="manifest unknown")
    inspect = _completed(1, "")  # no local copy either
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[pull, inspect],
        ),
        pytest.raises(RuntimeError, match="manifest unknown"),
    ):
        resolve_base_digest("img:1", runtime="docker")


def test_resolve_base_digest_no_image_pull_ok_raises() -> None:
    pull = _completed(0)
    inspect = _completed(1, "")  # pull "succeeded" but nothing local
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[pull, inspect],
        ),
        pytest.raises(RuntimeError, match="pull succeeded"),
    ):
        resolve_base_digest("img:1", runtime="docker")


# -- _allow_stale_base --------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_allow_stale_base_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("VRG_ALLOW_STALE_BASE", value)
    assert _allow_stale_base() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_allow_stale_base_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("VRG_ALLOW_STALE_BASE", value)
    assert _allow_stale_base() is False


def test_allow_stale_base_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VRG_ALLOW_STALE_BASE", raising=False)
    assert _allow_stale_base() is False


# -- _compose_setup bakes [container].build-command (epic .github#291) ---------

_BUILD_CMD_TOML = _VALID_TOML + '[container]\nenv-prefixes = []\nbuild-command = "make deps"\n'


def test_compose_setup_slots_build_after_install_before_warmup(tmp_path: Path) -> None:
    # For a non-self repo the composed setup is
    # `<vergil-tooling uv install …> && <build-command> && <warmup>`.
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={}, tooling=_TOOLING)
    warmup = _warmup_command("go", tmp_path)
    assert "vergil-tooling: installed v2.0.7" in setup
    assert "make deps" in setup
    assert (
        setup.index("vergil-tooling: installed v2.0.7")
        < setup.index("make deps")
        < setup.index(warmup)
    )


def test_compose_setup_absent_build_is_identical_to_install_and_warmup(tmp_path: Path) -> None:
    # Absent build-command ⇒ byte-identical to the pre-existing composition.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={}, tooling=_TOOLING)
    install = vergil_tooling_setup_command(_TOOLING)
    warmup = _warmup_command("go", tmp_path)
    assert setup == f"{install} && {warmup}"


def test_compose_setup_self_repo_slots_build_after_apt_before_warmup(tmp_path: Path) -> None:
    # Self-repo skips the uv install; the build-command still runs after apt and
    # before warmup, the same relative slot.
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    (tmp_path / "uv.lock").write_text("")
    setup = _compose_setup(tmp_path, "python", build_env={}, tooling=_TOOLING)
    warmup = _warmup_command("python", tmp_path)
    assert "uv tool install" not in setup
    assert setup.index("make deps") < setup.index(warmup)


def test_build_cached_image_bakes_build_command_into_setup(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    create_cmd = _capture_create_cmd(tmp_path, "go")
    setup_cmd = create_cmd[-1]
    assert "make deps" in setup_cmd
    assert setup_cmd.index("vergil-tooling: installed v2.0.7") < setup_cmd.index("make deps")


def test_build_cached_image_prints_build_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    out = capsys.readouterr().out
    assert "Build:" in out
    assert "make deps" in out


def test_build_cached_image_no_build_banner_when_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return create_result
        return ok

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    out = capsys.readouterr().out
    assert "Build:" not in out


# -- cache_sensitive_files folds [container].build-cache-files ----------------


def test_cache_sensitive_files_includes_declared_build_cache_file(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML
        + "[container]\nenv-prefixes = []\n"
        + 'build-cache-files = ["deps.txt", "missing.txt"]\n'
    )
    (tmp_path / "deps.txt").write_text("x\n")
    files = cache_sensitive_files(tmp_path, "go")
    assert (tmp_path / "deps.txt") in files
    assert (tmp_path / "missing.txt") not in files


def test_cache_sensitive_files_no_duplicate_build_cache_file(tmp_path: Path) -> None:
    # A build-cache-file that is already a default (vergil.toml) is not doubled.
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + '[container]\nenv-prefixes = []\nbuild-cache-files = ["vergil.toml"]\n'
    )
    files = cache_sensitive_files(tmp_path, "go")
    assert files.count(tmp_path / "vergil.toml") == 1


def test_cache_hash_changes_when_build_cache_file_changes(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + '[container]\nenv-prefixes = []\nbuild-cache-files = ["deps.txt"]\n'
    )
    (tmp_path / "deps.txt").write_text("v1\n")
    h1 = compute_cache_hash(cache_sensitive_files(tmp_path, "go"))
    (tmp_path / "deps.txt").write_text("v2\n")
    h2 = compute_cache_hash(cache_sensitive_files(tmp_path, "go"))
    assert h1 != h2


# -- conditional warmup (issue #2881) -----------------------------------------


def test_cpp_warmup_skips_during_born_green_window(tmp_path: Path) -> None:
    # Born-green scaffolding (epic #342) renders the cpp skeleton
    # (conanfile.txt + CMakeLists.txt) but NOT conan.lock — the lock is
    # produced by the scaffold's own `conan lock create` step. During that
    # transient no-lock window the cpp warmup, which runs
    # `conan install --lockfile=conan.lock` (#3021), would fail on the missing
    # lock (issue #3049). missing_warmup_files must therefore report conan.lock
    # so warmup SKIPS, leaving an unwarmed-but-usable container for the scaffold
    # step to create the lock in.
    (tmp_path / "conanfile.txt").write_text("[generators]\n")
    (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")
    assert missing_warmup_files("cpp", tmp_path) == ["conan.lock"]


def test_cpp_warmup_runs_when_lock_present(tmp_path: Path) -> None:
    # Once conan.lock is resolved and committed the cpp repo is fully
    # bootstrapped, so every warmup prerequisite is satisfied and warmup runs.
    _bootstrap_cpp(tmp_path)
    assert missing_warmup_files("cpp", tmp_path) == []


def test_cpp_warmup_reports_each_unsatisfied_group(tmp_path: Path) -> None:
    # An empty tree is missing every cpp prerequisite group: the conanfile
    # alternation renders as "conanfile.txt or conanfile.py", plus the two
    # single-name groups.
    assert missing_warmup_files("cpp", tmp_path) == [
        "conanfile.txt or conanfile.py",
        "CMakeLists.txt",
        "conan.lock",
    ]


def test_missing_warmup_files_empty_when_bootstrapped(tmp_path: Path) -> None:
    _bootstrap_python(tmp_path)
    assert missing_warmup_files("python", tmp_path) == []


def test_missing_warmup_files_reports_each_unsatisfied_group(tmp_path: Path) -> None:
    assert missing_warmup_files("python", tmp_path) == [
        "pyproject.toml",
        "uv.lock",
    ]


def test_missing_warmup_files_partial_bootstrap_reports_only_the_gap(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert missing_warmup_files("python", tmp_path) == ["uv.lock"]


def test_missing_warmup_files_unknown_language_has_no_requirements(tmp_path: Path) -> None:
    assert missing_warmup_files("cobol", tmp_path) == []


# -- unbootstrapped_manifests (vrg-validate's skip test, issue #4384) ----------


def test_unbootstrapped_rust_with_no_cargo_toml(tmp_path: Path) -> None:
    assert unbootstrapped_manifests("rust", tmp_path) == ["Cargo.toml", "Cargo.lock"]


def test_unbootstrapped_rust_with_cargo_toml_is_bootstrapped(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    assert unbootstrapped_manifests("rust", tmp_path) == []


def test_unbootstrapped_python_with_no_manifest(tmp_path: Path) -> None:
    assert unbootstrapped_manifests("python", tmp_path) == ["pyproject.toml", "uv.lock"]


def test_unbootstrapped_partial_bootstrap_is_not_unbootstrapped(tmp_path: Path) -> None:
    # The warmup skips a pyproject.toml with no uv.lock, but validation must run
    # on it and fail: a half-made project is broken, not unstarted.
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert missing_warmup_files("python", tmp_path) == ["uv.lock"]
    assert unbootstrapped_manifests("python", tmp_path) == []


def test_unbootstrapped_cpp_needs_every_group_missing(tmp_path: Path) -> None:
    assert unbootstrapped_manifests("cpp", tmp_path) == [
        "conanfile.txt or conanfile.py",
        "CMakeLists.txt",
        "conan.lock",
    ]
    (tmp_path / "conanfile.py").write_text("")
    assert unbootstrapped_manifests("cpp", tmp_path) == []


@pytest.mark.parametrize(
    ("lang", "manifest"),
    [("go", "go.mod"), ("typescript", "package.json")],
)
def test_unbootstrapped_other_languages(tmp_path: Path, lang: str, manifest: str) -> None:
    assert manifest in unbootstrapped_manifests(lang, tmp_path)
    (tmp_path / manifest).write_text("")
    assert unbootstrapped_manifests(lang, tmp_path) == []


@pytest.mark.parametrize(
    "manifest",
    [
        '{\n  "name": "demo",\n  "version": "0.1.0",\n  "private": true\n}\n',
        '{"name": "demo", "dependencies": {}, "devDependencies": {}, '
        '"optionalDependencies": {}, "peerDependencies": {}}',
    ],
    ids=["repo-init", "empty-fields"],
)
def test_unbootstrapped_typescript_dependency_free_package_json(
    tmp_path: Path, manifest: str
) -> None:
    # The package.json repo-init writes declares no dependencies; with no
    # lockfile the repo is not bootstrapped yet (issue #4624).
    (tmp_path / "package.json").write_text(manifest, encoding="utf-8")
    assert unbootstrapped_manifests("typescript", tmp_path) == ["package-lock.json"]


@pytest.mark.parametrize(
    "field",
    ["dependencies", "devDependencies", "optionalDependencies", "peerDependencies"],
)
def test_unbootstrapped_typescript_dependencies_without_lockfile_is_bootstrapped(
    tmp_path: Path, field: str
) -> None:
    # Declared dependencies with no lockfile is a real error: validation runs and fails.
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", field: {"typescript": "^6.0.3"}}), encoding="utf-8"
    )
    assert unbootstrapped_manifests("typescript", tmp_path) == []


@pytest.mark.parametrize(
    "manifest",
    ["", "[]", '{"name": "demo", "dependencies": []}', "\xff"],
    ids=["empty", "not-an-object", "field-not-an-object", "not-utf8"],
)
def test_unbootstrapped_typescript_unreadable_package_json_is_bootstrapped(
    tmp_path: Path, manifest: str
) -> None:
    # A package.json that is not a readable object never passes as unstarted.
    (tmp_path / "package.json").write_bytes(manifest.encode("latin-1"))
    assert unbootstrapped_manifests("typescript", tmp_path) == []


def test_unbootstrapped_typescript_with_lockfile_is_bootstrapped(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert unbootstrapped_manifests("typescript", tmp_path) == []


def test_unbootstrapped_unknown_language_is_never_unbootstrapped(tmp_path: Path) -> None:
    assert unbootstrapped_manifests("cobol", tmp_path) == []


@pytest.mark.parametrize(
    ("lang", "source"),
    [
        ("python", "pkg/mod.py"),
        ("rust", "src/lib.rs"),
        ("go", "main.go"),
        ("cpp", "include/x.hpp"),
        ("typescript", "src/index.ts"),
    ],
)
def test_language_sources_picks_the_language_files(lang: str, source: str) -> None:
    paths = ["README.md", "vergil.toml", "scripts/bin/validate-local", source]
    assert language_sources(lang, paths) == [source]


def test_language_sources_is_sorted_and_language_specific() -> None:
    paths = ["src/main.rs", "build.rs", "tool.py"]
    assert language_sources("rust", paths) == ["build.rs", "src/main.rs"]
    assert language_sources("python", paths) == ["tool.py"]


def test_language_sources_unknown_language_has_none() -> None:
    assert language_sources("cobol", ["x.cbl"]) == []


def test_warmup_command_skipped_when_manifests_absent(tmp_path: Path) -> None:
    # The bootstrap case: no go.mod means no warmup, rather than a command
    # guaranteed to fail and abort the image build (issue #2871).
    assert _warmup_command("go", tmp_path) == ""


def test_warmup_command_runs_when_manifests_present(tmp_path: Path) -> None:
    _bootstrap_cpp(tmp_path)
    warmup = _warmup_command("cpp", tmp_path)
    # Warmup consumes the committed conan.lock via --lockfile — it never
    # regenerates it with `conan lock create` (#3021).
    assert "conan lock create" not in warmup
    assert "conan install . -s build_type=Debug --build=missing --lockfile=conan.lock" in warmup
    assert "cmake -S . -B build" in warmup


def test_warmup_command_empty_for_language_without_install_commands(tmp_path: Path) -> None:
    assert _warmup_command("cobol", tmp_path) == ""


# -- rust warmup and Cargo.lock (epic vergil-project/.github#1165, spec §6.1) --

_CARGO_LOCK = b"# This file is automatically @generated by Cargo.\nversion = 4\n"
# cargo 1.99's stale-lockfile error, as the live check against
# vergil-fixture-rust-lib printed it, and the older wording.
_STALE_LOCK_STDERR = (
    "error: cannot update the lock file /workspace/Cargo.lock because --locked was "
    "passed to prevent this\n"
)
_OLD_STALE_LOCK_STDERR = (
    "error: the lock file /work/Cargo.lock needs to be updated but --locked was "
    "passed to prevent this\n"
)


def _bootstrap_rust(root: Path) -> None:
    (root / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    (root / "Cargo.lock").write_bytes(_CARGO_LOCK)


def test_rust_warmup_requires_lockfile(tmp_path: Path) -> None:
    # Cargo.lock is its own group, so a repo with only Cargo.toml skips the
    # warmup and `cargo generate-lockfile` can still run in the container.
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    assert missing_warmup_files("rust", tmp_path) == ["Cargo.lock"]
    assert _warmup_command("rust", tmp_path) == ""
    assert unbootstrapped_manifests("rust", tmp_path) == []


def test_rust_warmup_is_locked(tmp_path: Path) -> None:
    _bootstrap_rust(tmp_path)
    warmup = _warmup_command("rust", tmp_path)
    assert "cargo fetch --locked 2>&1" in warmup
    assert _warmup_line("rust", tmp_path) == "cargo fetch --locked"


def _run_rust_warmup(root: Path, fake_cargo: str) -> subprocess.CompletedProcess[str]:
    """Run the real rust warmup under bash, then a marker, with a fake ``cargo``.

    The fake stands in for cargo on PATH, so the test exercises the wrapper's
    shell (the stale-lock match, the exit status, the && chain) for real.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text(f"#!/bin/sh\n{fake_cargo}\n")
    cargo.chmod(0o755)
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603
        [bash, "-c", f"{_warmup_command('rust', root)} && echo BUILD-CONTINUED"],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        check=False,
    )


def test_rust_warmup_success_continues_build(tmp_path: Path) -> None:
    _bootstrap_rust(tmp_path)
    result = _run_rust_warmup(tmp_path, 'echo "fetched $*" >&2')
    assert result.returncode == 0
    assert "fetched fetch --locked" in result.stdout
    assert "BUILD-CONTINUED" in result.stdout
    assert WARMUP_SKIPPED_MARKER not in result.stdout


@pytest.mark.parametrize("stderr", [_STALE_LOCK_STDERR, _OLD_STALE_LOCK_STDERR])
def test_rust_warmup_stale_lock_skips_loudly(tmp_path: Path, stderr: str) -> None:
    _bootstrap_rust(tmp_path)
    result = _run_rust_warmup(tmp_path, f"printf '%s' {shlex.quote(stderr)} >&2; exit 101")
    assert result.returncode == 0
    # cargo's own error is shown, then the skip notice, and the build goes on.
    assert stderr.strip() in result.stdout
    assert (
        f"{WARMUP_SKIPPED_MARKER} warmup skipped: Cargo.lock does not match Cargo.toml"
        in result.stdout
    )
    assert "BUILD-CONTINUED" in result.stdout
    assert (tmp_path / "Cargo.lock").read_bytes() == _CARGO_LOCK


def test_rust_warmup_other_failure_fails_build(tmp_path: Path) -> None:
    _bootstrap_rust(tmp_path)
    result = _run_rust_warmup(
        tmp_path,
        "echo 'error: failed to get `serde` as a dependency: network failure' >&2; exit 101",
    )
    assert result.returncode == 101
    assert "network failure" in result.stdout
    assert "BUILD-CONTINUED" not in result.stdout
    assert WARMUP_SKIPPED_MARKER not in result.stdout
    assert (tmp_path / "Cargo.lock").read_bytes() == _CARGO_LOCK


def test_rust_warmup_skip_visible_when_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # quiet_warmup captures the warmup's output, so the host prints the notice.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_rust(tmp_path)
    captured = (
        f"{_STALE_LOCK_STDERR}"
        f"{WARMUP_SKIPPED_MARKER} warmup skipped: Cargo.lock does not match Cargo.toml; x\n"
    )

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return MagicMock(returncode=0, stdout="abc123\n")
        if cmd[1] == "start":
            return MagicMock(returncode=0, stdout=captured, stderr="")
        return MagicMock(returncode=0)

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "rust",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            quiet_warmup=True,
            build_env={},
            tooling=_TOOLING,
        )
    out = capsys.readouterr().out
    assert "NOTICE: warmup skipped: Cargo.lock does not match Cargo.toml; x" in out
    # The banner shows the plain locked fetch, not the shell wrapper.
    assert "  Warmup:  cargo fetch --locked\n" in out


def test_quiet_warmup_without_skip_prints_no_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_rust(tmp_path)

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            return MagicMock(returncode=0, stdout="abc123\n")
        return MagicMock(returncode=0, stdout="Downloaded serde\n", stderr="")

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(
            tmp_path,
            "rust",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            quiet_warmup=True,
            build_env={},
            tooling=_TOOLING,
        )
    assert "NOTICE:" not in capsys.readouterr().out


def test_compose_setup_omits_warmup_on_unbootstrapped_repo(tmp_path: Path) -> None:
    # An unbootstrapped repo still yields a usable setup string: install only.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    setup = _compose_setup(tmp_path, "go", build_env={}, tooling=_TOOLING)
    # No warmup on an unbootstrapped repo: nothing is appended after the install.
    assert setup == vergil_tooling_setup_command(_TOOLING)
    assert "vergil-tooling: installed v2.0.7" in setup


def test_build_cached_image_reports_skipped_warmup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The skip is announced; a silently unwarmed image would be
    # indistinguishable from a warmed one until a dependency turned up missing.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    ok = MagicMock(returncode=0, stdout="abc123\n")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=ok):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--branch--hash",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    out = capsys.readouterr().out
    assert "Warmup:  skipped" in out
    assert "go.mod" in out


def test_cache_files_cpp_tracks_conan_and_cmake_manifests(tmp_path: Path) -> None:
    # Without these the cache key is vergil.toml alone, so a dependency change
    # would silently reuse a stale warmed image.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_cpp(tmp_path)
    (tmp_path / "conan.lock").write_text("{}")
    names = {p.name for p in cache_sensitive_files(tmp_path, "cpp")}
    assert names == {"conanfile.txt", "conan.lock", "CMakeLists.txt", "vergil.toml"}


def test_cache_files_typescript_tracks_lockfile(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "package-lock.json").write_text("{}")
    names = {p.name for p in cache_sensitive_files(tmp_path, "typescript")}
    assert names == {"package-lock.json", "vergil.toml"}


def test_cpp_cache_hash_changes_when_conanfile_appears(tmp_path: Path) -> None:
    # The conanfile is part of the cpp cache key: adding it changes the hash,
    # so a dependency change forces a rebuild rather than reusing a stale image.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    before = compute_cache_hash(cache_sensitive_files(tmp_path, "cpp"))
    _bootstrap_cpp(tmp_path)
    after = compute_cache_hash(cache_sensitive_files(tmp_path, "cpp"))
    assert before != after


# -- the resolver's container env reaches the cache build (#3421) --------------

_PY_ENV = {"UV_PYTHON": "3.14", "UV_PYTHON_PREFERENCE": "only-system"}

_ONLY_MANAGED_PYPROJECT = """\
[project]
name = "lab"
requires-python = ">=3.12"

[tool.uv]
python-preference = "only-managed"
"""


def test_compose_setup_exports_build_env_before_every_step(tmp_path: Path) -> None:
    # The env is exported at the head of the setup step, so the vergil-tooling install, the
    # build-command and the warmup all run with it, as the run path does.
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    _bootstrap_python(tmp_path)
    setup = _compose_setup(tmp_path, "python", build_env=_PY_ENV, tooling=_TOOLING)
    prefix = "export UV_PYTHON=3.14 UV_PYTHON_PREFERENCE=only-system && "
    assert setup.startswith(prefix)
    assert setup.index("vergil-tooling: installed v2.0.7") < setup.index("make deps")
    assert setup.index("make deps") < setup.index("uv sync --frozen --group dev")


def test_compose_setup_exports_build_env_before_apt(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + '[container]\nenv-prefixes = []\nsystem-packages = ["libfoo"]\n'
    )
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={"A": "1"}, tooling=_TOOLING)
    assert setup.startswith("export A=1 && apt-get update")


def test_compose_setup_quotes_build_env_values(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={"X": "a b;c"}, tooling=_TOOLING)
    assert setup.startswith("export X='a b;c' && ")


def test_compose_setup_rejects_an_invalid_env_name(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with pytest.raises(ValueError, match="not a valid environment variable name"):
        _compose_setup(tmp_path, "go", build_env={"BAD NAME": "x"}, tooling=_TOOLING)


def test_compose_setup_empty_build_env_adds_nothing(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_go(tmp_path)
    assert not _compose_setup(tmp_path, "go", build_env={}, tooling=_TOOLING).startswith("export")


def test_compute_cache_hash_covers_env(tmp_path: Path) -> None:
    (tmp_path / "f.toml").write_text("x")
    files = [tmp_path / "f.toml"]
    base = compute_cache_hash(files, base_digest="sha256:a", salt="r")
    py314 = compute_cache_hash(files, base_digest="sha256:a", salt="r", env=_PY_ENV)
    py312 = compute_cache_hash(
        files, base_digest="sha256:a", salt="r", env={**_PY_ENV, "UV_PYTHON": "3.12"}
    )
    managed = compute_cache_hash(
        files, base_digest="sha256:a", salt="r", env={**_PY_ENV, "UV_PYTHON_PREFERENCE": "x"}
    )
    assert len({base, py314, py312, managed}) == 4


def test_compute_cache_hash_empty_env_keeps_the_existing_key(tmp_path: Path) -> None:
    # A language with no container env keeps its existing key, so its caches are
    # not invalidated by the env fold.
    (tmp_path / "f.toml").write_text("x")
    files = [tmp_path / "f.toml"]
    assert compute_cache_hash(files, base_digest="d", salt="r", env={}) == compute_cache_hash(
        files, base_digest="d", salt="r"
    )


def test_compute_cache_hash_env_order_does_not_matter(tmp_path: Path) -> None:
    (tmp_path / "f.toml").write_text("x")
    files = [tmp_path / "f.toml"]
    forward = compute_cache_hash(files, env={"A": "1", "B": "2"})
    backward = compute_cache_hash(files, env={"B": "2", "A": "1"})
    assert forward == backward


def test_build_cached_image_prints_env_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_python(tmp_path)
    ok = MagicMock(returncode=0, stdout="abc123\n")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=ok):
        _build_cached_image(
            tmp_path,
            "python",
            "img:1",
            "img:1--b--h",
            runtime="docker",
            build_env=_PY_ENV,
            tooling=_TOOLING,
        )
    assert "  Env:     UV_PYTHON=3.14 UV_PYTHON_PREFERENCE=only-system" in capsys.readouterr().out


def test_build_cached_image_no_env_banner_without_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    ok = MagicMock(returncode=0, stdout="abc123\n")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=ok):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--b--h",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    assert "Env:" not in capsys.readouterr().out


def _provision_only_managed_repo(root: Path, primary: str) -> tuple[str, str]:
    """Provision an only-managed, no-.python-version repo; return (tag, setup)."""
    root.mkdir(exist_ok=True)
    (root / "vergil.toml").write_text(_VALID_TOML.replace('"go"', '"python"'))
    (root / "pyproject.toml").write_text(_ONLY_MANAGED_PYPROJECT)
    (root / "uv.lock").write_text("lock\n")
    assert not (root / ".python-version").exists()

    def resolve(repo_root: Path, lang: str) -> Resolution:
        resolution = window_default_resolution(repo_root, lang)
        assert resolution is not None
        return dataclasses.replace(
            resolution,
            versions=(primary,),
            primary=primary,
            container_tags={primary: primary},
            container_suffixes={primary: "python"},
        )

    create_cmd: list[str] = []

    def mock_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "create":
            create_cmd.extend(cmd)
        return MagicMock(returncode=0, stdout="abc123\n")

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container_cache.repo_resolution", side_effect=resolve),
        patch("vergil_tooling.lib.container_cache.default_image", return_value="base:1"),
        patch("vergil_tooling.lib.git.current_branch", return_value="develop"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.lib.container_cache.find_cached_image", return_value=None),
        patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=mock_run),
    ):
        tag, source = provision_dev_image(root, "python", runtime="docker")
    assert source == "cached"
    return tag, create_cmd[-1]


def test_provision_warmup_runs_with_the_resolved_interpreter(tmp_path: Path) -> None:
    # Regression for #3421: a repo with python-preference = "only-managed" and no
    # .python-version warmed its cache with uv's defaults plus the repo's own
    # preference, so uv downloaded a managed interpreter in place of the image's.
    _tag, setup = _provision_only_managed_repo(tmp_path / "lab", "3.14")
    head, _, rest = setup.partition(" && ")
    assert head == "export UV_PYTHON=3.14 UV_PYTHON_PREFERENCE=only-system"
    assert "uv sync --frozen --group dev" in rest


def test_provision_cache_key_follows_the_resolved_version(tmp_path: Path) -> None:
    # Same repo, same files, same base digest: only the resolved version moves,
    # and the cache key moves with it.
    tag_314, _ = _provision_only_managed_repo(tmp_path / "lab", "3.14")
    tag_312, setup_312 = _provision_only_managed_repo(tmp_path / "lab", "3.12")
    assert tag_314 != tag_312
    assert setup_312.startswith("export UV_PYTHON=3.12 UV_PYTHON_PREFERENCE=only-system && ")


def test_ensure_folds_build_env_into_the_key_and_the_build(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("lock\n")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    files = cache_sensitive_files(tmp_path, "python")
    expected = compute_cache_hash(
        files,
        base_digest="sha256:abc",
        salt=tmp_path.name,
        env=_PY_ENV,
        tooling="v2.0.7",
        tooling_sha=_SHA_207,
    )
    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="develop"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.lib.container_cache.find_cached_image", return_value=None),
        patch(
            "vergil_tooling.lib.container_cache._build_cached_image", return_value="t"
        ) as mock_build,
    ):
        ensure_cached_image(tmp_path, "python", "img:1", runtime="docker", build_env=_PY_ENV)
    assert mock_build.call_args[0][3] == f"img:1--develop--{tmp_path.name}.{expected}"
    assert mock_build.call_args.kwargs["build_env"] == _PY_ENV


def test_provision_passes_no_env_for_a_language_without_one(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container_cache.default_image", return_value="base:1"),
        patch(
            "vergil_tooling.lib.container_cache.ensure_cached_image", return_value="base:1"
        ) as ensure,
    ):
        provision_dev_image(tmp_path, "go", runtime="docker")
    assert ensure.call_args.kwargs["build_env"] == {}


# -- parse_cache_image_ref (#3481, #4162) -------------------------------------

_PY_BASE = "ghcr.io/vergil-project/prod-python"
_BASE = "ghcr.io/vergil-project/prod-base"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (
            f"{_PY_BASE}:3.14--feature-1-x--vergil-tooling.0123abcd",
            CacheImageRef(_PY_BASE, "3.14", "feature-1-x", "vergil-tooling", "0123abcd"),
        ),
        # A repository whose name starts with "." (the org profile repo).
        (
            f"{_BASE}:latest--develop--.github.2fb5421a",
            CacheImageRef(_BASE, "latest", "develop", ".github", "2fb5421a"),
        ),
        # A dotted repository name keeps every dot but the hash's.
        (
            f"{_BASE}:latest--develop--a.b.0123abcd",
            CacheImageRef(_BASE, "latest", "develop", "a.b", "0123abcd"),
        ),
        # A sanitized branch that itself contains "--" parses whole: the
        # repository never contains "--", so the last "--" is its boundary.
        (
            f"{_BASE}:latest--feature--odd--my-repo.0123abcd",
            CacheImageRef(_BASE, "latest", "feature--odd", "my-repo", "0123abcd"),
        ),
        # The format before #4162: no repository, the hash after "--".
        (
            f"{_PY_BASE}:3.14--feature-1-x--0123abcd",
            CacheImageRef(_PY_BASE, "3.14", "feature-1-x", None, "0123abcd"),
        ),
        (
            f"{_BASE}:latest--feature--odd--0123abcd",
            CacheImageRef(_BASE, "latest", "feature--odd", None, "0123abcd"),
        ),
        (f"{_PY_BASE}:3.14", None),  # base image
        (f"{_PY_BASE}:<none>", None),
        (f"{_PY_BASE}:3.14--x--NOTAHASH", None),
        (f"{_PY_BASE}:3.14--x--repo.NOTAHASH", None),
        ("docker.io/library/redis:7--x--0123abcd", None),  # not ours
        ("docker.io/library/redis:7--x--repo.0123abcd", None),  # not ours
    ],
)
def test_parse_cache_image_ref(ref: str, expected: CacheImageRef | None) -> None:
    assert parse_cache_image_ref(ref) == expected


def test_parsed_ref_says_whether_it_is_legacy() -> None:
    legacy = parse_cache_image_ref(f"{_BASE}:latest--develop--2fb5421a")
    current = parse_cache_image_ref(f"{_BASE}:latest--develop--repo.2fb5421a")
    assert legacy is not None
    assert legacy.legacy
    assert current is not None
    assert not current.legacy


@pytest.mark.parametrize(
    ("branch", "repo"),
    [
        ("feature/12-x", "vergil-tooling"),
        ("develop", ".github"),
        ("feature/a--b", "odd--name-"),
        ("release/1.2", "repo with spaces"),
    ],
)
def test_parse_inverts_cache_image_tag(branch: str, repo: str) -> None:
    ref = cache_image_tag("ghcr.io/vergil-project/prod-go:1.24", branch, "a1b2c3d4", repo=repo)
    assert parse_cache_image_ref(ref) == CacheImageRef(
        "ghcr.io/vergil-project/prod-go",
        "1.24",
        sanitize_branch(branch),
        sanitize_repo(repo),
        "a1b2c3d4",
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("vergil-tooling", "vergil-tooling"),
        (".github", ".github"),
        ("odd--name", "odd-name"),
        ("-lead-and-trail-", "lead-and-trail"),
        ("with spaces/and@marks", "with-spaces-and-marks"),
    ],
)
def test_sanitize_repo(name: str, expected: str) -> None:
    assert sanitize_repo(name) == expected


@pytest.mark.parametrize("name", ["", "-", "@@"])
def test_sanitize_repo_refuses_a_name_that_leaves_nothing(name: str) -> None:
    with pytest.raises(ValueError, match="leaves nothing"):
        sanitize_repo(name)


# -- vergil_tooling_setup_command: uv tool install of one commit (epic -------
# vergil-project/.github#1350). The fragment runs under bash with git, uv and
# vrg-whoami stubbed (tests/vergil_tooling/tooling_install_stubs.py); each stub
# logs its calls. The stub vrg-whoami is not in /usr/local/bin, so a run that
# gets as far as the post-install check fails it, naming the expected path.

_SHA_206 = fixture_tooling_sha("v2.0.6")
_EXACT = unresolved("v2.0.6")
_DEV = ResolvedRef(ref="feature/1-x", kind="dev", tag=None, sha=None)
_CHECK_FAILURE = "vrg-whoami resolves to "


def _setup_run(resolved: ResolvedRef, tmp_path: Path, **env: str) -> Run:
    return run_lines(vergil_tooling_setup_command(resolved), tmp_path, env=env)


def _uv_env(run: Run, index: int) -> dict[str, str]:
    return dict(item.split("=", 1) for item in run.of("uv-env")[index])


def _tool_install(run: Run) -> list[str]:
    (install,) = [c for c in run.of("uv") if c[:2] == ["tool", "install"]]
    return install


@pytest.mark.parametrize("resolved", [_TOOLING, _EXACT, _DEV])
def test_setup_command_has_no_apt(resolved: ResolvedRef) -> None:
    setup = vergil_tooling_setup_command(resolved)
    assert setup.startswith("bash -euo pipefail -c ")
    assert "apt" not in setup
    assert "/opt/vergil/python" not in setup
    assert "/usr/bin/vrg-whoami" not in setup


def test_setup_command_is_the_shared_install_and_check() -> None:
    lines = [
        *install_lines(
            _TOOLING,
            python_dir_env="VERGIL_CORE_PYTHON_DIR",
            tool_dir="/opt/vergil/uv/tools",
            bin_dir="/usr/local/bin",
        ),
        *check_lines("/usr/local/bin"),
    ]
    assert vergil_tooling_setup_command(_TOOLING) == (
        f"bash -euo pipefail -c {shlex.quote(chr(10).join(lines))}"
    )


def test_setup_command_installs_into_usr_local_bin_and_checks_it(tmp_path: Path) -> None:
    run = _setup_run(_TOOLING, tmp_path)
    env = _uv_env(run, -1)
    assert env["UV_TOOL_DIR"] == "/opt/vergil/uv/tools"
    assert env["UV_TOOL_BIN_DIR"] == "/usr/local/bin"
    assert "vergil-tooling: installed v2.0.7 (commit " in run.stderr
    # The check runs after the install and names /usr/local/bin, not the stub.
    assert run.returncode == 1
    assert f"{_CHECK_FAILURE}{tmp_path}/stub-bin/vrg-whoami, not /usr/local/bin/vrg-whoami" in (
        run.stderr
    )


def test_setup_command_line_pin_installs_its_resolved_commit(tmp_path: Path) -> None:
    run = _setup_run(_TOOLING, tmp_path)
    assert [c for c in run.of("git") if c[0] == "ls-remote"] == []
    install = _tool_install(run)
    assert install[-1] == f"vergil-tooling @ git+{GIT_URL}@{_SHA_207}"
    assert "--managed-python" in install
    assert "--no-config" in install
    assert install[install.index("--python") + 1] == "3.15"
    assert "-c" in install
    env = _uv_env(run, -1)
    assert (env["UV_PYTHON"], env["UV_PYTHON_PREFERENCE"], env["UV_NO_MANAGED_PYTHON"]) == (
        "<unset>",
        "<unset>",
        "<unset>",
    )


def test_exact_pin_resolves_sha_inside_the_build(tmp_path: Path) -> None:
    listing = f"{'d' * 40}\trefs/tags/v2.0.6\n{_SHA_206}\trefs/tags/v2.0.6^{{}}\n"
    run = _setup_run(_EXACT, tmp_path, STUB_LS_REMOTE=listing)
    assert [c for c in run.of("git") if c[0] == "ls-remote"] == [
        ["ls-remote", "--tags", GIT_URL, "refs/tags/v2.0.6*"]
    ]
    assert _tool_install(run)[-1] == f"vergil-tooling @ git+{GIT_URL}@{_SHA_206}"
    assert f"vergil-tooling: installed v2.0.6 (commit {_SHA_206})" in run.stderr


def test_exact_pin_with_a_missing_tag_fails_the_build(tmp_path: Path) -> None:
    run = _setup_run(_EXACT, tmp_path, STUB_LS_REMOTE="")
    assert run.returncode == 1
    assert f"no release tag v2.0.6 at {GIT_URL}" in run.stderr
    assert run.of("uv") == []


def test_setup_command_dev_ref_installs_unconstrained(tmp_path: Path) -> None:
    run = _setup_run(_DEV, tmp_path)
    install = _tool_install(run)
    assert install[-1] == f"vergil-tooling @ git+{GIT_URL}@feature/1-x"
    assert "--reinstall" in install
    assert "-c" not in install
    assert run.returncode == 1
    assert _CHECK_FAILURE in run.stderr


@pytest.mark.parametrize("value", ["set", "unset"])
def test_setup_command_reads_image_python_dir(tmp_path: Path, value: str) -> None:
    managed = tmp_path / "managed-python"
    (managed / "cpython-3.15.0-linux-x86_64-gnu").mkdir(parents=True)
    env = {"VERGIL_CORE_PYTHON_DIR": str(managed)} if value == "set" else {}
    run = _setup_run(_TOOLING, tmp_path, **env)
    uv_env = _uv_env(run, -1)
    if value == "set":
        assert uv_env["UV_PYTHON_INSTALL_DIR"] == str(managed)
        assert uv_env["UV_PYTHON_DOWNLOADS"] == "never"
        assert "uv may download" not in run.stderr
    else:
        assert uv_env["UV_PYTHON_INSTALL_DIR"] == "<unset>"
        assert uv_env["UV_PYTHON_DOWNLOADS"] == "<unset>"
        assert "vergil-tooling: no VERGIL_CORE_PYTHON_DIR; uv may download 3.15" in run.stderr


# A uv that behaves as the real one does about interpreters: with downloads
# off it fails when its Python directory holds no 3.15, and otherwise it would
# download one (logged, so the test can see that it did not).
_UV_PYTHON_AWARE = """#!/bin/bash
printf 'uv\\t%s\\n' "$*" >> "$STUB_LOG"
if [ "$1" = export ]; then
  while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
  printf 'rich==15.0.0\\n' > "$out"; exit 0
fi
if [ "$1" = tool ]; then
  shopt -s nullglob
  found=("${UV_PYTHON_INSTALL_DIR:-/nonexistent}"/cpython-3.15*)
  [ ${#found[@]} -gt 0 ] && exit 0
  if [ "${UV_PYTHON_DOWNLOADS:-}" = never ]; then
    echo "error: No interpreter found for Python 3.15 in managed installations" >&2
    exit 2
  fi
  printf 'download\\t3.15\\n' >> "$STUB_LOG"; exit 0
fi
exit 0
"""


def test_setup_command_wrong_python_dir_fails_not_downloads(tmp_path: Path) -> None:
    stubs = stub_bin(tmp_path)
    write_stub(stubs, "uv", _UV_PYTHON_AWARE)
    empty = tmp_path / "empty-python-dir"
    empty.mkdir()
    run = run_lines(
        vergil_tooling_setup_command(_TOOLING),
        tmp_path,
        env={"VERGIL_CORE_PYTHON_DIR": str(empty)},
        path=[stubs],
    )
    assert run.returncode == 1
    assert "No interpreter found for Python 3.15" in run.stderr
    assert f"uv tool install of v2.0.7 (commit {_SHA_207}) failed" in run.stderr
    assert run.of("download") == []
    assert _CHECK_FAILURE not in run.stderr


def test_setup_command_without_python_dir_may_download(tmp_path: Path) -> None:
    # A non-Vergil image (no VERGIL_CORE_PYTHON_DIR): uv downloads 3.15, logged.
    stubs = stub_bin(tmp_path)
    write_stub(stubs, "uv", _UV_PYTHON_AWARE)
    run = run_lines(vergil_tooling_setup_command(_TOOLING), tmp_path, path=[stubs])
    assert run.of("download") == [["3.15"]]
    assert "vergil-tooling: no VERGIL_CORE_PYTHON_DIR; uv may download 3.15" in run.stderr


def test_setup_command_failed_install_fails_loudly(tmp_path: Path) -> None:
    run = _setup_run(_TOOLING, tmp_path, STUB_INSTALL_RC="2")
    assert run.returncode == 1
    assert f"uv tool install of v2.0.7 (commit {_SHA_207}) failed" in run.stderr
    assert _CHECK_FAILURE not in run.stderr


def test_setup_self_repo_unchanged(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    setup = _compose_setup(tmp_path, "go", build_env={}, tooling=_TOOLING)
    assert "vergil-tooling @ git+" not in setup
    assert "uv tool install" not in setup


# -- the vergil-tooling release in the cache key (#4550, #1350) ---------------
#
# The hermetic listing (conftest) holds v2.0.6, v2.0.7 and v2.1.9, so the pin
# v2.0 in _VALID_TOML resolves to v2.0.7 and its commit. A line pin's key folds
# in the tag and the commit; an exact pin's key is the tag alone.


def _go_key(repo_root: Path, tooling: str, sha: str = "") -> str:
    files = cache_sensitive_files(repo_root, "go")
    return compute_cache_hash(
        files, base_digest="sha256:abc", salt=repo_root.name, tooling=tooling, tooling_sha=sha
    )


def _line_key(repo_root: Path, tag: str) -> str:
    return _go_key(repo_root, tag, fixture_tooling_sha(tag))


def _ensure_go(repo_root: Path, store: _ImageStore) -> str:
    with (
        patch("vergil_tooling.lib.git.current_branch", return_value="develop"),
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch("vergil_tooling.lib.container_cache.cache_repo_name", return_value="r"),
        patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=store.run),
        patch("vergil_tooling.lib.container_cache._image_is_launchable", return_value=True),
        patch("vergil_tooling.lib.container_cache._build_cached_image", side_effect=store.build),
    ):
        return ensure_cached_image(repo_root, "go", "img:1", runtime="docker", build_env={})


def _key(repo_root: Path) -> CacheKey:
    files = cache_sensitive_files(repo_root, "go")
    with patch(
        "vergil_tooling.lib.container_cache.resolve_base_digest",
        return_value=("sha256:abc", True),
    ):
        return current_cache_key(
            repo_root, "img:1", files, build_env={}, existing_tag=None, runtime="docker"
        )


def _offline(url: str, pattern: str) -> str:
    msg = f"git ls-remote --tags {url} {pattern} failed (exit 128): no route to host"
    raise ToolingInstallError(msg)


def _no_network(url: str, pattern: str) -> str:
    msg = f"unexpected ls-remote: {url} {pattern}"
    raise AssertionError(msg)


def test_compute_cache_hash_folds_in_the_tooling_release(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    assert _go_key(tmp_path, "v2.0.6") != _go_key(tmp_path, "v2.0.7")
    assert _go_key(tmp_path, "v2.0.7") != _go_key(tmp_path, "")


def test_compute_cache_hash_folds_in_the_sha_as_its_own_input(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    assert _go_key(tmp_path, "v2.0.7", "a" * 40) != _go_key(tmp_path, "v2.0.7", "b" * 40)
    assert _go_key(tmp_path, "v2.0.7", "a" * 40) != _go_key(tmp_path, "v2.0.7")
    # Not concatenated into the tag: the SHA is a separate, delimited input.
    assert _go_key(tmp_path, "v2.0.7", "a" * 40) != _go_key(tmp_path, "v2.0.7" + "a" * 40)


def test_line_pin_cache_hash_includes_sha_but_tooling_stays_the_tag(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    key = _key(tmp_path)
    assert (key.pin, key.tooling) == ("v2.0", "v2.0.7")
    assert key.resolved == _TOOLING
    assert key.cache_hash == _line_key(tmp_path, "v2.0.7")
    assert key.cache_hash != _go_key(tmp_path, "v2.0.7")


def test_exact_pin_key_needs_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vergil_tooling.lib import tooling_release

    monkeypatch.setattr(tooling_release, "_ls_remote", _offline)
    monkeypatch.setenv("VRG_DOCKER_INSTALL_TAG", "v2.0.6")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    key = _key(tmp_path)
    assert (key.pin, key.tooling) == ("v2.0.6", "v2.0.6")
    assert key.resolved == _EXACT
    assert key.cache_hash == _go_key(tmp_path, "v2.0.6")


def test_ensure_rebuilds_a_cache_older_than_the_pinned_lines_newest_release(
    tmp_path: Path,
) -> None:
    # The branch cache was built when v2.0 resolved to v2.0.6. Nothing else has
    # changed, but v2.0.7 is now released: the cache is rebuilt, at v2.0.7.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    old = f"img:1--develop--r.{_line_key(tmp_path, 'v2.0.6')}"
    store = _ImageStore([old])
    image = _ensure_go(tmp_path, store)
    assert image == f"img:1--develop--r.{_line_key(tmp_path, 'v2.0.7')}"
    assert store.removed == [old]
    assert store.built == [image]
    assert store.tooling == ["v2.0.7"]


def test_ensure_reuses_a_cache_at_the_pinned_lines_newest_release(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    current = f"img:1--develop--r.{_line_key(tmp_path, 'v2.0.7')}"
    store = _ImageStore([current])
    assert _ensure_go(tmp_path, store) == current
    assert store.built == []
    assert store.removed == []


def test_ensure_refuses_when_the_release_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline: the cache is neither reused nor rebuilt, nor removed, and the
    # error names the release the cached image holds and the exact pin to use.
    from vergil_tooling.lib import tooling_release
    from vergil_tooling.lib.tooling_release import ToolingResolveError

    monkeypatch.setattr(tooling_release, "_ls_remote", _offline)
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    old = f"img:1--develop--r.{_line_key(tmp_path, 'v2.0.6')}"
    store = _ImageStore([old])
    with (
        patch("vergil_tooling.lib.container_cache.cached_tooling", return_value="v2.0.6"),
        pytest.raises(ToolingResolveError) as info,
    ):
        _ensure_go(tmp_path, store)
    message = str(info.value)
    assert "cannot resolve vergil-tooling v2.0 to a release" in message
    assert GIT_URL in message
    assert "no route to host" in message
    assert "VRG_DOCKER_INSTALL_TAG=v2.0.6" in message
    assert store.built == []
    assert store.removed == []


@pytest.mark.parametrize("held", ["v2.0.6", "develop", "feature/1-x", "v2.0", None])
def test_reuse_advice_suggests_an_exact_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: str | None
) -> None:
    # The VRG_DOCKER_INSTALL_TAG value the error suggests is always an exact
    # release: an image built from a dev ref (or recording no release) gets
    # the generic advice, never "VRG_DOCKER_INSTALL_TAG=develop".
    from vergil_tooling.lib import tooling_release
    from vergil_tooling.lib.tooling_release import ToolingResolveError

    monkeypatch.setattr(tooling_release, "_ls_remote", _offline)
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    store = _ImageStore([f"img:1--develop--r.{_line_key(tmp_path, 'v2.0.6')}"])
    with (
        patch("vergil_tooling.lib.container_cache.cached_tooling", return_value=held),
        pytest.raises(ToolingResolveError) as info,
    ):
        _ensure_go(tmp_path, store)
    suggested = re.findall(r"VRG_DOCKER_INSTALL_TAG=(\S+?)\s", str(info.value))
    if held == "v2.0.6":
        assert suggested == ["v2.0.6"]
    else:
        assert suggested == []
        assert "pin an exact release (vX.Y.Z) with VRG_DOCKER_INSTALL_TAG" in str(info.value)
    assert all(classify_ref(tag) == "exact" for tag in suggested)


def test_resolve_error_message_keeps_exact_pin_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vergil_tooling.lib import tooling_release
    from vergil_tooling.lib.tooling_release import ToolingResolveError

    monkeypatch.setattr(tooling_release, "_ls_remote", lambda _url, _pattern: "")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with pytest.raises(ToolingResolveError) as info:
        _ensure_go(tmp_path, _ImageStore([]))
    message = str(info.value)
    assert f"no release tag for v2.0 at {GIT_URL}" in message
    assert "To work without the network, pin an exact release (vX.Y.Z) with " in message
    assert "VRG_DOCKER_INSTALL_TAG" in message


def test_ensure_with_an_exact_override_reuses_its_cache_with_no_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An exact pin's key is its tag alone: a cache built under it is found
    # again with no listing at all.
    from vergil_tooling.lib import tooling_release

    monkeypatch.setattr(tooling_release, "_ls_remote", _no_network)
    monkeypatch.setenv("VRG_DOCKER_INSTALL_TAG", "v2.0.6")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cached = f"img:1--develop--r.{_go_key(tmp_path, 'v2.0.6')}"
    store = _ImageStore([cached])
    assert _ensure_go(tmp_path, store) == cached
    assert store.built == []


def test_self_repo_key_names_no_tooling_and_makes_no_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vergil_tooling.lib import tooling_release

    monkeypatch.setattr(tooling_release, "_ls_remote", _no_network)
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    key = _key(tmp_path)
    assert (key.pin, key.tooling, key.resolved) == ("", "", None)
    assert key.cache_hash == _go_key(tmp_path, "")


def test_compose_setup_installs_the_resolved_release(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    setup = _compose_setup(tmp_path, "go", build_env={}, tooling=_TOOLING)
    assert setup == vergil_tooling_setup_command(_TOOLING)
    assert f"vt_sha={_SHA_207}" in setup


def test_compose_setup_refuses_no_release_for_a_consumer_repo(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with pytest.raises(ValueError, match="no vergil-tooling release"):
        _compose_setup(tmp_path, "go", build_env={}, tooling=None)


def test_build_records_the_release_in_the_commit_and_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        calls.append(cmd)
        return _completed(0, stdout="cid\n")

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--b--r.h",
            runtime="docker",
            build_env={},
            tooling=_TOOLING,
        )
    commit = next(c for c in calls if c[1] == "commit")
    assert commit == [
        "docker",
        "commit",
        "--message",
        "vergil-tooling v2.0.7",
        "cid",
        "img:1--b--r.h",
    ]
    assert f"Install: vergil-tooling@v2.0.7 (pin v2.0, commit {_SHA_207})\n" in (
        capsys.readouterr().out
    )


def test_build_of_an_exact_pin_records_its_tag_and_no_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The commit is resolved inside the build, so the banner and the image
    # record name the tag alone.
    monkeypatch.setenv("VRG_DOCKER_INSTALL_TAG", "v2.0.6")
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        calls.append(cmd)
        return _completed(0, stdout="cid\n")

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--b--r.h",
            runtime="docker",
            build_env={},
            tooling=_EXACT,
        )
    commit = next(c for c in calls if c[1] == "commit")
    assert commit[2:4] == ["--message", "vergil-tooling v2.0.6"]
    assert "  Install: vergil-tooling@v2.0.6\n" in capsys.readouterr().out


def test_build_for_the_self_repo_records_no_release(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> MagicMock:
        calls.append(cmd)
        return _completed(0, stdout="cid\n")

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=run):
        _build_cached_image(
            tmp_path,
            "go",
            "img:1",
            "img:1--b--r.h",
            runtime="docker",
            build_env={},
            tooling=None,
        )
    assert next(c for c in calls if c[1] == "commit") == [
        "docker",
        "commit",
        "cid",
        "img:1--b--r.h",
    ]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_completed(0, stdout="vergil-tooling v2.1.320\n"), "v2.1.320"),
        (_completed(0, stdout="\n"), None),
        (_completed(0, stdout="buildkit.dockerfile.v0\n"), None),
        (_completed(1, stderr="no such image"), None),
    ],
)
def test_cached_tooling_reads_the_commit_message(result: MagicMock, expected: str | None) -> None:
    from vergil_tooling.lib.container_cache import cached_tooling

    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=result) as run:
        assert cached_tooling("img:1--b--r.h", runtime="docker") == expected
    assert run.call_args[0][0] == [
        "docker",
        "image",
        "inspect",
        "img:1--b--r.h",
        "--format",
        "{{.Comment}}",
    ]
