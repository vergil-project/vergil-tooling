"""Tests for vergil_tooling.lib.container_cache."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import window_default_resolution
from vergil_tooling.lib.config import vrg_install_tag
from vergil_tooling.lib.container_cache import (
    _VRG_GIT_URL,
    CacheImageRef,
    _allow_stale_base,
    _build_cached_image,
    _compose_setup,
    _image_is_launchable,
    _is_self_repo,
    _sanitize_branch,
    _warmup_command,
    apt_install_command,
    cache_image_tag,
    cache_repo_name,
    cache_sensitive_files,
    compute_cache_hash,
    ensure_cached_image,
    find_cached_image,
    missing_warmup_files,
    parse_cache_image_ref,
    provision_dev_image,
    resolve_base_digest,
    sanitize_branch,
    sanitize_repo,
    vergil_tooling_setup_command,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from vergil_tooling.lib.versions.resolve import Resolution


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
    expected_hash = compute_cache_hash(files, base_digest="sha256:abc", salt=tmp_path.name)
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
    expected_hash = compute_cache_hash(files, base_digest="sha256:abc", salt=tmp_path.name)
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

    def run(self, cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[1] == "images":
            return _completed(0, stdout="".join(f"{ref}\n" for ref in self.refs))
        if cmd[1:3] == ["rmi", "-f"]:
            self.removed.append(cmd[3])
            self.refs.remove(cmd[3])
            return _completed(0)
        msg = f"unexpected runtime call: {cmd}"
        raise AssertionError(msg)

    def build(self, _root: Path, _lang: str, _base: str, target_tag: str, **_kw: object) -> str:
        self.built.append(target_tag)
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, lang, "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "unknown", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
        )
    out = capsys.readouterr().out
    assert "Warmup:" not in out


def test_build_cached_image_uses_uv_tool_install(tmp_path: Path) -> None:
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
        )
    setup_cmd = create_cmd[-1]
    assert "uv tool install" in setup_cmd
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


def test_build_cached_image_python_includes_uv_install(tmp_path: Path) -> None:
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
            tmp_path, "python", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
        )
    setup_cmd = create_cmd[-1]
    assert "uv tool install" in setup_cmd
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


# -- _is_self_repo ------------------------------------------------------------


def test_is_self_repo_true(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "vergil-tooling"\n')
    assert _is_self_repo(tmp_path) is True


def test_is_self_repo_false_different_name(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "my-app"\n')
    assert _is_self_repo(tmp_path) is False


def test_is_self_repo_false_no_pyproject(tmp_path: Path) -> None:
    assert _is_self_repo(tmp_path) is False


def test_is_self_repo_false_no_project_table(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.ruff]\nline-length = 100\n")
    assert _is_self_repo(tmp_path) is False


def test_is_self_repo_false_invalid_toml(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("not valid [[[toml content")
    assert _is_self_repo(tmp_path) is False


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
            tmp_path, "python", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "ghcr.io/r/dev-go:1.26", "target:tag", runtime="docker", build_env={}
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
    assert setup_cmd.index("apt-get update") < setup_cmd.index("uv tool install")


def test_build_cached_image_no_apt_when_no_system_packages(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    create_cmd = _capture_create_cmd(tmp_path, "go")
    # No system-packages fragment (the vergil-tooling install's own apt calls,
    # on its packaged path, carry the fail-fast options).
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
    pull = _completed(1, stderr="connection refused")
    inspect = _completed(0, "sha256:local9\n")
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[pull, inspect],
    ):
        digest, verified = resolve_base_digest("img:1", runtime="docker", allow_stale=True)
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
    import subprocess as _sp

    inspect = _completed(0, "sha256:local9\n")
    with (
        patch(
            "vergil_tooling.lib.container_cache.subprocess.run",
            side_effect=[_sp.TimeoutExpired(cmd="pull", timeout=1), inspect],
        ),
        pytest.raises(RuntimeError, match="timed out after"),
    ):
        resolve_base_digest("img:1", runtime="docker")


def test_resolve_base_digest_pull_timeout_allow_stale_uses_local() -> None:
    import subprocess as _sp

    inspect = _completed(0, "sha256:local9\n")
    with patch(
        "vergil_tooling.lib.container_cache.subprocess.run",
        side_effect=[_sp.TimeoutExpired(cmd="pull", timeout=1), inspect],
    ):
        digest, verified = resolve_base_digest("img:1", runtime="docker", allow_stale=True)
    assert digest == "sha256:local9"
    assert verified is False


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
    # `<uv tool install …> && <build-command> && <warmup>`.
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={})
    warmup = _warmup_command("go", tmp_path)
    assert "uv tool install" in setup
    assert "make deps" in setup
    assert setup.index("uv tool install") < setup.index("make deps") < setup.index(warmup)


def test_compose_setup_absent_build_is_identical_to_install_and_warmup(tmp_path: Path) -> None:
    # Absent build-command ⇒ byte-identical to the pre-existing composition.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={})
    tag = vrg_install_tag(tmp_path)
    install = vergil_tooling_setup_command(tag)
    warmup = _warmup_command("go", tmp_path)
    assert setup == f"{install} && {warmup}"


def test_compose_setup_self_repo_slots_build_after_apt_before_warmup(tmp_path: Path) -> None:
    # Self-repo skips the uv install; the build-command still runs after apt and
    # before warmup, the same relative slot.
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    (tmp_path / "uv.lock").write_text("")
    setup = _compose_setup(tmp_path, "python", build_env={})
    warmup = _warmup_command("python", tmp_path)
    assert "uv tool install" not in setup
    assert setup.index("make deps") < setup.index(warmup)


def test_build_cached_image_bakes_build_command_into_setup(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    create_cmd = _capture_create_cmd(tmp_path, "go")
    setup_cmd = create_cmd[-1]
    assert "make deps" in setup_cmd
    assert setup_cmd.index("uv tool install") < setup_cmd.index("make deps")


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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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


def test_compose_setup_omits_warmup_on_unbootstrapped_repo(tmp_path: Path) -> None:
    # An unbootstrapped repo still yields a usable setup string: install only.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    setup = _compose_setup(tmp_path, "go", build_env={})
    # No warmup on an unbootstrapped repo: nothing is appended after the install.
    assert setup == vergil_tooling_setup_command(vrg_install_tag(tmp_path))
    assert "uv tool install" in setup


def test_build_cached_image_reports_skipped_warmup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The skip is announced; a silently unwarmed image would be
    # indistinguishable from a warmed one until a dependency turned up missing.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    ok = MagicMock(returncode=0, stdout="abc123\n")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=ok):
        _build_cached_image(
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", build_env={}
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
    # The env is exported at the head of the setup step, so the uv install, the
    # build-command and the warmup all run with it, as the run path does.
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    _bootstrap_python(tmp_path)
    setup = _compose_setup(tmp_path, "python", build_env=_PY_ENV)
    prefix = "export UV_PYTHON=3.14 UV_PYTHON_PREFERENCE=only-system && "
    assert setup.startswith(prefix)
    assert setup.index("uv tool install") < setup.index("make deps")
    assert setup.index("make deps") < setup.index("uv sync --frozen --group dev")


def test_compose_setup_exports_build_env_before_apt(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + '[container]\nenv-prefixes = []\nsystem-packages = ["libfoo"]\n'
    )
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={"A": "1"})
    assert setup.startswith("export A=1 && apt-get update")


def test_compose_setup_quotes_build_env_values(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go", build_env={"X": "a b;c"})
    assert setup.startswith("export X='a b;c' && ")


def test_compose_setup_rejects_an_invalid_env_name(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    with pytest.raises(ValueError, match="not a valid environment variable name"):
        _compose_setup(tmp_path, "go", build_env={"BAD NAME": "x"})


def test_compose_setup_empty_build_env_adds_nothing(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_go(tmp_path)
    assert "export" not in _compose_setup(tmp_path, "go", build_env={})


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
            tmp_path, "python", "img:1", "img:1--b--h", runtime="docker", build_env=_PY_ENV
        )
    assert "  Env:     UV_PYTHON=3.14 UV_PYTHON_PREFERENCE=only-system" in capsys.readouterr().out


def test_build_cached_image_no_env_banner_without_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    ok = MagicMock(returncode=0, stdout="abc123\n")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=ok):
        _build_cached_image(tmp_path, "go", "img:1", "img:1--b--h", runtime="docker", build_env={})
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
    expected = compute_cache_hash(files, base_digest="sha256:abc", salt=tmp_path.name, env=_PY_ENV)
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


# -- vergil_tooling_setup_command: the install the image's environment chooses --
# (epic vergil-project/.github#649, Task 4). The snippet is run with bash against a
# temp root through the VRG_CACHE_ROOT seam, with apt-get, apt-cache and uv stubbed
# first on PATH; each stub logs its argv to a shared log.

_SOURCE_REL = "etc/apt/sources.list.d/vergil.sources"
_CORE_REL = "opt/vergil/python/3.15/bin/python3"

_TOOLS = ("awk", "bash", "cat", "dirname", "mkdir", "readlink")
_BASH = shutil.which("bash") or "/bin/bash"

_APT_GET_STUB = """#!/bin/sh
echo "apt-get $*" >> "$STUB_LOG"
case " $* " in *" update "*) exit "${APT_UPDATE_RC:-0}" ;; esac
exit 0
"""

_APT_CACHE_STUB = """#!/bin/sh
echo "apt-cache $*" >> "$STUB_LOG"
printf 'vergil-tooling:\\n  Installed: (none)\\n  Candidate: %s\\n' "$CANDIDATE"
"""

_UV_STUB = """#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
"""


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def _interpreter(path: Path) -> None:
    """A stand-in Python at *path*: a copy of ``/bin/sh``, so a shebang naming it runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy("/bin/sh", path)


class _Env:
    """A temp image root, the stub directory and the stubs' shared log."""

    def __init__(self, tmp_path: Path, *, source: bool, core: bool) -> None:
        tmp_path = tmp_path.resolve()
        self.root = tmp_path / "root"
        self.stubs = tmp_path / "stubs"
        self.tools = tmp_path / "tools"
        self.log = tmp_path / "stub.log"
        self.log.write_text("")
        self.root.mkdir()
        # Only the utilities the snippet uses, so no host vrg-whoami is ever on PATH.
        self.tools.mkdir()
        for tool in _TOOLS:
            found = shutil.which(tool)
            assert found, tool
            (self.tools / tool).symlink_to(found)
        _write_exec(self.stubs / "apt-get", _APT_GET_STUB)
        _write_exec(self.stubs / "apt-cache", _APT_CACHE_STUB)
        _write_exec(self.stubs / "uv", _UV_STUB)
        if source:
            (self.root / _SOURCE_REL).parent.mkdir(parents=True)
            (self.root / _SOURCE_REL).write_text("Types: deb\n")
        if core:
            _interpreter(self.root / _CORE_REL)

    def packaged_vrg(self, shebang: str | None = None) -> Path:
        """Place the package's ``vrg-whoami`` (venv copy + ``/usr/bin`` symlink)."""
        venv_bin = self.root / "opt/vergil/vergil-tooling/venv/bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        python = venv_bin / "python3"
        if not python.exists():
            python.symlink_to(self.root / _CORE_REL)
        target = venv_bin / "vrg-whoami"
        _write_exec(target, f"#!{shebang or python}\nexit 0\n")
        usr_bin = self.root / "usr/bin"
        usr_bin.mkdir(parents=True, exist_ok=True)
        (usr_bin / "vrg-whoami").symlink_to(target)
        return target

    def run(
        self, tag: str, *, candidate: str = "", extra: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.stubs}:{self.root / 'usr/bin'}:{self.tools}",
            "VRG_CACHE_ROOT": str(self.root),
            "STUB_LOG": str(self.log),
            "CANDIDATE": candidate,
            "HOME": os.environ.get("HOME", "/root"),
            **(extra or {}),
        }
        return subprocess.run(  # noqa: S603
            [_BASH, "-c", vergil_tooling_setup_command(tag)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


_TODAYS_INSTALL = "uv tool install --quiet vergil-tooling @ git+{url}@{tag}"


def test_setup_dev_ref_without_core_is_todays_install(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=False, core=False)
    result = env.run("develop")
    assert result.returncode == 0, result.stderr
    assert env.calls() == [_TODAYS_INSTALL.format(url=_VRG_GIT_URL, tag="develop")]
    assert "uv tool install from git (image without the vergil apt repository)" in result.stderr


def test_setup_dev_ref_binds_core_python(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    result = env.run("feature/1-x", extra={"UV_PYTHON": "/opt/python/3.12/bin/python3"})
    assert result.returncode == 0, result.stderr
    core = env.root / _CORE_REL
    assert env.calls() == [
        f"uv tool install --quiet --python {core} vergil-tooling @ git+{_VRG_GIT_URL}@feature/1-x"
    ]
    assert "uv tool install from git on the core Python (feature/1-x)" in result.stderr


def test_setup_dev_ref_refuses_more_than_one_core_python(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    _interpreter(env.root / "opt/vergil/python/3.14/bin/python3")
    result = env.run("develop")
    assert result.returncode == 1
    assert "more than one core Python" in result.stderr
    assert env.calls() == []


@pytest.mark.parametrize(
    ("source", "core", "has", "lacks"),
    [
        (True, False, "the vergil apt source", "a core vergil-python"),
        (False, True, "a core vergil-python", "the vergil apt source"),
    ],
)
def test_setup_dev_ref_half_environment_refuses(
    tmp_path: Path, source: bool, core: bool, has: str, lacks: str
) -> None:
    env = _Env(tmp_path, source=source, core=core)
    result = env.run("develop")
    assert result.returncode == 1
    assert f"the image has {has}" in result.stderr
    assert f"but not {lacks}" in result.stderr
    assert "refusing to guess" in result.stderr
    assert env.calls() == []


@pytest.mark.parametrize(
    ("source", "core", "has", "lacks"),
    [
        (True, False, "the vergil apt source", "a core vergil-python"),
        (False, True, "a core vergil-python", "the vergil apt source"),
    ],
)
def test_setup_half_environment_refuses(
    tmp_path: Path, source: bool, core: bool, has: str, lacks: str
) -> None:
    env = _Env(tmp_path, source=source, core=core)
    result = env.run("v2.1", candidate="2.1.4-1")
    assert result.returncode == 1
    assert f"the image has {has} (" in result.stderr
    assert f"but not {lacks} (" in result.stderr
    assert "refusing to guess" in result.stderr
    assert env.calls() == []


def test_setup_line_ref_branches_on_environment() -> None:
    setup = vergil_tooling_setup_command("v2.1")
    assert setup.startswith("bash -euo pipefail -c ")
    assert "/etc/apt/sources.list.d/vergil.sources" in setup
    assert "/opt/vergil/python/*/bin/python3" in setup
    assert "vergil-tooling: packaged install (v2.1)" in setup
    assert "uv tool install from git (image without the vergil apt repository)" in setup
    assert "install -y vergil-tooling" in setup
    assert "apt-get -o" in setup


def test_setup_exact_ref_pins_version() -> None:
    setup = vergil_tooling_setup_command("v2.1.3")
    assert "install -y vergil-tooling=2.1.3-1" in setup
    assert "Pin: version 2.1.3-1" in setup


def test_setup_line_ref_packaged_install(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    env.packaged_vrg()
    result = env.run("v2.1", candidate="2.1.4-1")
    assert result.returncode == 0, result.stderr
    assert "vergil-tooling: packaged install (v2.1)" in result.stderr
    pin = env.root / "etc/apt/preferences.d/vergil-tooling"
    assert pin.read_text() == "Package: vergil-tooling\nPin: version 2.1.*\nPin-Priority: 1001\n"
    calls = env.calls()
    assert calls[0].startswith("apt-get -o") and calls[0].endswith(" update")
    assert calls[1] == "apt-cache policy vergil-tooling"
    assert calls[2].endswith(" install -y vergil-tooling")
    assert len(calls) == 3


def test_setup_exact_ref_packaged_install(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    env.packaged_vrg()
    result = env.run("v2.1.3", candidate="2.1.3-1")
    assert result.returncode == 0, result.stderr
    assert env.calls()[-1].endswith(" install -y vergil-tooling=2.1.3-1")


def test_setup_packaged_check_accepts_venv_home(tmp_path: Path) -> None:
    # A venv whose python is a copy, not a symlink: pyvenv.cfg's home decides.
    env = _Env(tmp_path, source=True, core=True)
    venv = env.root / "opt/vergil/vergil-tooling/venv"
    _interpreter(venv / "bin/python3")
    (venv / "pyvenv.cfg").write_text(f"home = {env.root / 'opt/vergil/python/3.15/bin'}\n")
    env.packaged_vrg()
    result = env.run("v2.1", candidate="2.1.4-1")
    assert result.returncode == 0, result.stderr


def test_setup_packaged_check_refuses_wrong_vrg(tmp_path: Path) -> None:
    # The package installed, but the vrg-whoami on PATH is not the /usr/bin one.
    env = _Env(tmp_path, source=True, core=True)
    _write_exec(env.stubs / "vrg-whoami", "#!/bin/sh\nexit 0\n")
    result = env.run("v2.1", candidate="2.1.4-1")
    assert result.returncode == 1
    assert f"vrg-whoami resolves to {env.stubs / 'vrg-whoami'}" in result.stderr


def test_setup_packaged_check_refuses_missing_vrg(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    result = env.run("v2.1", candidate="2.1.4-1")
    assert result.returncode == 1
    assert "vrg-whoami is not on PATH after the packaged install" in result.stderr


def test_setup_packaged_check_refuses_vrg_off_core_python(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    other = tmp_path.resolve() / "elsewhere/bin/python3"
    _interpreter(other)
    env.packaged_vrg(shebang=str(other))
    result = env.run("v2.1", candidate="2.1.4-1")
    assert result.returncode == 1
    assert f"runs on {other}, not a core vergil-python" in result.stderr


def test_setup_packaged_check_refuses_failing_vrg(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    target = env.packaged_vrg()
    target.write_text(f"#!{target.parent / 'python3'}\nexit 3\n")
    result = env.run("v2.1", candidate="2.1.4-1")
    assert result.returncode == 1
    assert "vrg-whoami --mode failed after the packaged install" in result.stderr


@pytest.mark.parametrize(
    ("tag", "candidate"),
    [("v2.1", "2.2.0-1"), ("v2.1", "2.10.0-1"), ("v2.1", "(none)"), ("v2.1", "")],
)
def test_setup_line_candidate_mismatch_refuses(tmp_path: Path, tag: str, candidate: str) -> None:
    env = _Env(tmp_path, source=True, core=True)
    env.packaged_vrg()
    result = env.run(tag, candidate=candidate)
    assert result.returncode == 1
    assert f"no packaged vergil-tooling matching {tag}" in result.stderr
    assert f"(apt candidate: {candidate or '(none)'})" in result.stderr
    assert not any(" install " in call for call in env.calls())


def test_setup_exact_candidate_mismatch_refuses(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    env.packaged_vrg()
    result = env.run("v2.1.3", candidate="2.1.4-1")
    assert result.returncode == 1
    assert "no packaged vergil-tooling matching v2.1.3" in result.stderr
    assert not any(" install " in call for call in env.calls())


def test_setup_apt_update_failure_fails(tmp_path: Path) -> None:
    env = _Env(tmp_path, source=True, core=True)
    env.packaged_vrg()
    result = env.run("v2.1", candidate="2.1.4-1", extra={"APT_UPDATE_RC": "100"})
    assert result.returncode != 0
    assert len(env.calls()) == 1


def test_setup_self_repo_unchanged(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    setup = _compose_setup(tmp_path, "go", build_env={})
    assert "vergil-tooling @ git+" not in setup
    assert "packaged install" not in setup
    assert "apt-cache policy" not in setup
