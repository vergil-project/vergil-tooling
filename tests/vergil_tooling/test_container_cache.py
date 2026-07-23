"""Tests for vergil_tooling.lib.container_cache."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.container_cache import (
    _allow_stale_base,
    _build_cached_image,
    _is_self_repo,
    _sanitize_branch,
    cache_image_tag,
    cache_sensitive_files,
    clean_branch_images,
    compute_cache_hash,
    ensure_cached_image,
    find_cached_image,
    provision_dev_image,
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


# -- cache_sensitive_files ----------------------------------------------------


def test_cache_files_python(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("lock\n")
    (tmp_path / "vergil.toml").write_text("[vergil-tooling]\n")
    files = cache_sensitive_files(tmp_path, "python")
    names = [f.name for f in files]
    assert "uv.lock" in names
    assert "vergil.toml" in names


def test_cache_files_go(tmp_path: Path) -> None:
    (tmp_path / "go.sum").write_text("sum\n")
    (tmp_path / "vergil.toml").write_text("[vergil-tooling]\n")
    files = cache_sensitive_files(tmp_path, "go")
    names = [f.name for f in files]
    assert "go.sum" in names
    assert "vergil.toml" in names


def test_cache_files_unknown_language(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[vergil-tooling]\n")
    files = cache_sensitive_files(tmp_path, "")
    assert len(files) == 1
    assert files[0].name == "vergil.toml"


def test_cache_files_missing_lockfile(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[vergil-tooling]\n")
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


def test_ensure_returns_existing_cache_on_hash_match(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cached_tag = "ghcr.io/r/dev-go:1.26--feature-42--"
    files = cache_sensitive_files(tmp_path, "go")
    expected_hash = compute_cache_hash(files, base_digest="sha256:abc", salt=tmp_path.name)
    full_tag = cached_tag + expected_hash

    with (
        patch("vergil_tooling.lib.git.current_branch") as mock_branch,
        patch(
            "vergil_tooling.lib.container_cache.resolve_base_digest",
            return_value=("sha256:abc", True),
        ),
        patch(
            "vergil_tooling.lib.container_cache.find_cached_image",
            return_value=(full_tag, expected_hash),
        ),
    ):
        mock_branch.return_value = "feature/42"
        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26", runtime="docker")
    assert result == full_tag


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
    # the second mount site the run-path mask (#2486) missed (#2495).
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
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
        repo_root: Path, lang: str, base_image: str, target_tag: str, *, runtime: str = ""
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
    (tmp_path / "vergil.toml").write_text("[vergil-tooling]\n")
    files = cache_sensitive_files(tmp_path, "go")
    h1 = compute_cache_hash(files, base_digest="sha256:aaa", salt="r")
    h2 = compute_cache_hash(files, base_digest="sha256:bbb", salt="r")
    assert h1 != h2


def test_compute_cache_hash_stable_for_same_inputs(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[vergil-tooling]\n")
    files = cache_sensitive_files(tmp_path, "go")
    h1 = compute_cache_hash(files, base_digest="sha256:aaa", salt="r")
    h2 = compute_cache_hash(files, base_digest="sha256:aaa", salt="r")
    assert h1 == h2


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
