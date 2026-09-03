"""Tests for vergil_tooling.lib.container_cache."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.config import vrg_install_tag
from vergil_tooling.lib.container_cache import (
    _VRG_GIT_URL,
    _allow_stale_base,
    _build_cached_image,
    _compose_setup,
    _image_is_launchable,
    _is_self_repo,
    _sanitize_branch,
    _warmup_command,
    apt_install_command,
    cache_image_tag,
    cache_sensitive_files,
    clean_branch_images,
    compute_cache_hash,
    ensure_cached_image,
    find_cached_image,
    missing_warmup_files,
    provision_dev_image,
    prune_orphan_branch_images,
    resolve_base_digest,
)

if TYPE_CHECKING:
    from pathlib import Path


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
    """Create the manifests the cpp warmup requires."""
    (root / "conanfile.txt").write_text("[generators]\nCMakeToolchain\n")
    (root / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\n")


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
    )
    assert tag == "ghcr.io/vergil-project/dev-go:1.26--feature-42-thing--abcd1234"


# -- find_cached_image --------------------------------------------------------


def test_find_cached_image_hit() -> None:
    docker_output = (
        "ghcr.io/vergil-project/dev-go:1.26--feature-42-thing--abcd1234\n"
        "ghcr.io/vergil-project/dev-python:3.14\n"
    )
    mock_result = MagicMock(returncode=0, stdout=docker_output)
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        result = find_cached_image(
            "ghcr.io/vergil-project/dev-go:1.26", "feature/42-thing", runtime="docker"
        )
    assert result is not None
    assert result[0] == "ghcr.io/vergil-project/dev-go:1.26--feature-42-thing--abcd1234"
    assert result[1] == "abcd1234"


def test_find_cached_image_miss() -> None:
    docker_output = "ghcr.io/vergil-project/dev-python:3.14\n"
    mock_result = MagicMock(returncode=0, stdout=docker_output)
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        result = find_cached_image(
            "ghcr.io/vergil-project/dev-go:1.26", "feature/42-thing", runtime="docker"
        )
    assert result is None


def test_find_cached_image_docker_error() -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        assert find_cached_image("img:1", "branch", runtime="docker") is None


# -- ensure_cached_image ------------------------------------------------------


def test_ensure_returns_base_for_python(tmp_path: Path) -> None:
    assert ensure_cached_image(tmp_path, "python", "img:1", runtime="docker") == "img:1"


def test_ensure_returns_base_when_no_files(tmp_path: Path) -> None:
    with patch("vergil_tooling.lib.git.current_branch", return_value="feature/42"):
        assert ensure_cached_image(tmp_path, "go", "img:1", runtime="docker") == "img:1"


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
        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker")
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
        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker")
    assert result == "rebuilt:tag"
    build.assert_called_once()
    # The unlaunchable image was force-removed before rebuilding.
    assert mock_run.call_count == 1
    rmi_cmd = mock_run.call_args[0][0]
    assert rmi_cmd[:3] == ["docker", "rmi", "-f"]
    assert full_tag in rmi_cmd


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

        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker")
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
        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker")
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
        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker")

    assert result == "rebuilt:tag"
    mock_build.assert_called_once()
    # The stale image was removed.
    assert stale_tag in mock_run.call_args[0][0]


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


def test_provision_passes_declared_version(tmp_path: Path) -> None:
    # The repo's declared [ci].versions primary threads into image selection so
    # provisioning warms the same image vrg-container-run picks (issue #2468).
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container_cache.primary_ci_version", return_value="3.12"),
        patch(
            "vergil_tooling.lib.container_cache.default_image", return_value="base:1"
        ) as default_img,
        patch("vergil_tooling.lib.container_cache.ensure_cached_image", return_value="base:1"),
    ):
        provision_dev_image(tmp_path, "python", runtime="docker")
    default_img.assert_called_once_with("python", fallback=True, prefix="prod", version="3.12")


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


# -- clean_branch_images ------------------------------------------------------


def test_clean_branch_images_removes_matching() -> None:
    docker_output = (
        "ghcr.io/r/dev-go:1.26--feature-42-thing--abcd1234\n"
        "ghcr.io/r/dev-base:latest--feature-42-thing--efgh5678\n"
        "ghcr.io/r/dev-python:3.14\n"
    )
    mock_result = MagicMock(returncode=0, stdout=docker_output)
    calls = []

    def capture_run(cmd: list[str], **kwargs: object) -> MagicMock:
        calls.append(cmd)
        return mock_result

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=capture_run):
        removed = clean_branch_images("feature/42-thing", runtime="docker")
    assert removed == 2


def test_clean_branch_images_none_found() -> None:
    mock_result = MagicMock(returncode=0, stdout="ghcr.io/r/dev-python:3.14\n")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        assert clean_branch_images("feature/99-other", runtime="docker") == 0


def test_clean_branch_images_docker_error() -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        assert clean_branch_images("feature/42", runtime="docker") == 0


# -- prune_orphan_branch_images -----------------------------------------------


def test_prune_orphan_branch_images_removes_only_orphans() -> None:
    images = (
        "ghcr.io/r/dev-python:3.14--develop--aaaa1111\n"  # live
        "ghcr.io/r/dev-python:3.14--feature-300-live--bbbb2222\n"  # live
        "ghcr.io/r/dev-python:3.14--feature-42-gone--cccc3333\n"  # orphan
        "ghcr.io/r/dev-go:1.26--feature-9-abandoned--dddd4444\n"  # orphan
        "ghcr.io/r/dev-python:3.14\n"  # plain base image — never touched
    )
    removed_lines: list[str] = []

    def capture_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if len(cmd) >= 2 and cmd[1] == "rmi":
            removed_lines.append(cmd[2])
            return MagicMock(returncode=0, stdout="")
        return MagicMock(returncode=0, stdout=images)

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=capture_run):
        removed = prune_orphan_branch_images(["develop", "feature/300-live"], runtime="docker")

    assert removed == 2
    assert removed_lines == [
        "ghcr.io/r/dev-python:3.14--feature-42-gone--cccc3333",
        "ghcr.io/r/dev-go:1.26--feature-9-abandoned--dddd4444",
    ]


def test_prune_orphan_branch_images_keeps_all_when_all_live() -> None:
    images = "ghcr.io/r/dev-python:3.14--develop--aaaa1111\n"

    def capture_run(cmd: list[str], **kwargs: object) -> MagicMock:
        assert not (len(cmd) >= 2 and cmd[1] == "rmi"), "no image should be removed"
        return MagicMock(returncode=0, stdout=images)

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=capture_run):
        assert prune_orphan_branch_images(["develop"], runtime="docker") == 0


def test_prune_orphan_branch_images_no_live_branches_removes_all_cached() -> None:
    images = (
        "ghcr.io/r/dev-python:3.14--feature-1-x--aaaa1111\n"
        "ghcr.io/r/dev-python:3.14\n"  # base — no `--`, untouched
    )

    def capture_run(cmd: list[str], **kwargs: object) -> MagicMock:
        return MagicMock(returncode=0, stdout=images)

    with patch("vergil_tooling.lib.container_cache.subprocess.run", side_effect=capture_run):
        assert prune_orphan_branch_images([], runtime="docker") == 1


def test_prune_orphan_branch_images_docker_error() -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=mock_result):
        assert prune_orphan_branch_images(["develop"], runtime="docker") == 0


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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker"
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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")

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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")
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
        _build_cached_image(tmp_path, lang, "img:1", "img:1--branch--hash", runtime="docker")
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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")


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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")


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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")

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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", quiet_warmup=True
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", quiet_warmup=True
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
            tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker", quiet_warmup=True
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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")
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
        _build_cached_image(tmp_path, "unknown", "img:1", "img:1--branch--hash", runtime="docker")
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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")
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
        result = ensure_cached_image(tmp_path, "python", "img:1", runtime="docker")
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
        _build_cached_image(tmp_path, "python", "img:1", "img:1--branch--hash", runtime="docker")
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
        ensure_cached_image(repo_a, "go", "img:1", runtime="docker")
        ensure_cached_image(repo_b, "go", "img:1", runtime="docker")

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
        _build_cached_image(tmp_path, "python", "img:1", "img:1--branch--hash", runtime="docker")
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
        _build_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26", "target:tag", runtime="docker")
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
    assert "apt-get" not in create_cmd[-1]


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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")
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
    setup = _compose_setup(tmp_path, "go")
    warmup = _warmup_command("go", tmp_path)
    assert "uv tool install" in setup
    assert "make deps" in setup
    assert setup.index("uv tool install") < setup.index("make deps") < setup.index(warmup)


def test_compose_setup_absent_build_is_identical_to_install_and_warmup(tmp_path: Path) -> None:
    # Absent build-command ⇒ byte-identical to the pre-existing composition.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    _bootstrap_go(tmp_path)
    setup = _compose_setup(tmp_path, "go")
    tag = vrg_install_tag(tmp_path)
    uv_install = f"uv tool install --quiet 'vergil-tooling @ git+{_VRG_GIT_URL}@{tag}'"
    warmup = _warmup_command("go", tmp_path)
    assert setup == f"{uv_install} && {warmup}"


def test_compose_setup_self_repo_slots_build_after_apt_before_warmup(tmp_path: Path) -> None:
    # Self-repo skips the uv install; the build-command still runs after apt and
    # before warmup, the same relative slot.
    (tmp_path / "vergil.toml").write_text(_BUILD_CMD_TOML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    (tmp_path / "uv.lock").write_text("")
    setup = _compose_setup(tmp_path, "python")
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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")
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
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")
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


def test_cpp_has_no_warmup_skip_entry(tmp_path: Path) -> None:
    # cpp is born-green (epic #342): repo-init scaffolds a complete cpp
    # skeleton, so there is no half-bootstrapped state to skip warmup for.
    # cpp therefore has no _WARMUP_REQUIRES entry — missing_warmup_files
    # returns [] for any tree, exactly like an unlisted language.
    assert missing_warmup_files("cpp", tmp_path) == []  # empty tree
    (tmp_path / "conanfile.txt").write_text("[generators]\n")
    assert missing_warmup_files("cpp", tmp_path) == []  # partial tree, still no skip


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
    setup = _compose_setup(tmp_path, "go")
    # No warmup on an unbootstrapped repo: nothing is appended after the install.
    assert " && " not in setup
    assert "uv tool install" in setup


def test_build_cached_image_reports_skipped_warmup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The skip is announced; a silently unwarmed image would be
    # indistinguishable from a warmed one until a dependency turned up missing.
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    ok = MagicMock(returncode=0, stdout="abc123\n")
    with patch("vergil_tooling.lib.container_cache.subprocess.run", return_value=ok):
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash", runtime="docker")
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
