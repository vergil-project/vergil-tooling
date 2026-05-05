"""Tests for standard_tooling.lib.docker_cache."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from standard_tooling.lib.docker_cache import (
    _build_cached_image,
    _sanitize_branch,
    cache_image_tag,
    cache_sensitive_files,
    clean_branch_images,
    compute_cache_hash,
    ensure_cached_image,
    find_cached_image,
)

if TYPE_CHECKING:
    from pathlib import Path

_VALID_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "go"

[dependencies]
standard-tooling = "v1.4"
"""


# -- cache_sensitive_files ----------------------------------------------------


def test_cache_files_python(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("lock\n")
    (tmp_path / "standard-tooling.toml").write_text("[standard-tooling]\n")
    files = cache_sensitive_files(tmp_path, "python")
    names = [f.name for f in files]
    assert "uv.lock" in names
    assert "standard-tooling.toml" in names


def test_cache_files_go(tmp_path: Path) -> None:
    (tmp_path / "go.sum").write_text("sum\n")
    (tmp_path / "standard-tooling.toml").write_text("[standard-tooling]\n")
    files = cache_sensitive_files(tmp_path, "go")
    names = [f.name for f in files]
    assert "go.sum" in names
    assert "standard-tooling.toml" in names


def test_cache_files_unknown_language(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text("[standard-tooling]\n")
    files = cache_sensitive_files(tmp_path, "")
    assert len(files) == 1
    assert files[0].name == "standard-tooling.toml"


def test_cache_files_missing_lockfile(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text("[standard-tooling]\n")
    files = cache_sensitive_files(tmp_path, "go")
    assert len(files) == 1
    assert files[0].name == "standard-tooling.toml"


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
        "ghcr.io/wphillipmoore/dev-go:1.26",
        "feature/42-thing",
        "abcd1234",
    )
    assert tag == "ghcr.io/wphillipmoore/dev-go:1.26--feature-42-thing--abcd1234"


# -- find_cached_image --------------------------------------------------------


def test_find_cached_image_hit() -> None:
    docker_output = (
        "ghcr.io/wphillipmoore/dev-go:1.26--feature-42-thing--abcd1234\n"
        "ghcr.io/wphillipmoore/dev-python:3.14\n"
    )
    mock_result = MagicMock(returncode=0, stdout=docker_output)
    with patch("standard_tooling.lib.docker_cache.subprocess.run", return_value=mock_result):
        result = find_cached_image("ghcr.io/wphillipmoore/dev-go:1.26", "feature/42-thing")
    assert result is not None
    assert result[0] == "ghcr.io/wphillipmoore/dev-go:1.26--feature-42-thing--abcd1234"
    assert result[1] == "abcd1234"


def test_find_cached_image_miss() -> None:
    docker_output = "ghcr.io/wphillipmoore/dev-python:3.14\n"
    mock_result = MagicMock(returncode=0, stdout=docker_output)
    with patch("standard_tooling.lib.docker_cache.subprocess.run", return_value=mock_result):
        result = find_cached_image("ghcr.io/wphillipmoore/dev-go:1.26", "feature/42-thing")
    assert result is None


def test_find_cached_image_docker_error() -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("standard_tooling.lib.docker_cache.subprocess.run", return_value=mock_result):
        assert find_cached_image("img:1", "branch") is None


# -- ensure_cached_image ------------------------------------------------------


def test_ensure_returns_base_for_python(tmp_path: Path) -> None:
    assert ensure_cached_image(tmp_path, "python", "img:1") == "img:1"


def test_ensure_returns_base_when_no_files(tmp_path: Path) -> None:
    with patch("standard_tooling.lib.git.current_branch", return_value="feature/42"):
        assert ensure_cached_image(tmp_path, "go", "img:1") == "img:1"


def test_ensure_returns_existing_cache_on_hash_match(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    cached_tag = "ghcr.io/r/dev-go:1.26--feature-42--"
    files = cache_sensitive_files(tmp_path, "go")
    expected_hash = compute_cache_hash(files, salt=tmp_path.name)
    full_tag = cached_tag + expected_hash

    with (
        patch("standard_tooling.lib.git.current_branch") as mock_branch,
        patch(
            "standard_tooling.lib.docker_cache.find_cached_image",
            return_value=(full_tag, expected_hash),
        ),
    ):
        mock_branch.return_value = "feature/42"
        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26")
    assert result == full_tag


def test_ensure_rebuilds_on_hash_mismatch(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    stale_tag = "ghcr.io/r/dev-go:1.26--feature-42--oldold00"
    new_tag = "ghcr.io/r/dev-go:1.26--feature-42--"

    with (
        patch("standard_tooling.lib.git.current_branch") as mock_branch,
        patch(
            "standard_tooling.lib.docker_cache.find_cached_image",
            return_value=(stale_tag, "oldold00"),
        ),
        patch("standard_tooling.lib.docker_cache.subprocess.run") as mock_run,
        patch(
            "standard_tooling.lib.docker_cache._build_cached_image",
        ) as mock_build,
    ):
        mock_branch.return_value = "feature/42"
        files = cache_sensitive_files(tmp_path, "go")
        expected_hash = compute_cache_hash(files)
        expected_tag = new_tag + expected_hash
        mock_build.return_value = expected_tag

        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26")
    assert result == expected_tag
    # Stale image should have been removed.
    mock_run.assert_called_once()
    assert stale_tag in mock_run.call_args[0][0]


def test_ensure_builds_on_cache_miss(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)

    with (
        patch("standard_tooling.lib.git.current_branch") as mock_branch,
        patch(
            "standard_tooling.lib.docker_cache.find_cached_image",
            return_value=None,
        ),
        patch(
            "standard_tooling.lib.docker_cache._build_cached_image",
            return_value="new:tag",
        ) as mock_build,
    ):
        mock_branch.return_value = "feature/42"
        result = ensure_cached_image(tmp_path, "go", "ghcr.io/r/dev-go:1.26")
    assert result == "new:tag"
    mock_build.assert_called_once()


# -- clean_branch_images ------------------------------------------------------


def test_clean_branch_images_removes_matching() -> None:
    docker_output = (
        "ghcr.io/r/dev-go:1.26--feature-42-thing--abcd1234\n"
        "ghcr.io/r/dev-base:latest--feature-42-thing--efgh5678\n"
        "ghcr.io/r/dev-python:3.14\n"
    )
    mock_result = MagicMock(returncode=0, stdout=docker_output)
    calls = []

    def capture_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        calls.append(cmd)
        return mock_result

    with patch("standard_tooling.lib.docker_cache.subprocess.run", side_effect=capture_run):
        removed = clean_branch_images("feature/42-thing")
    assert removed == 2


def test_clean_branch_images_none_found() -> None:
    mock_result = MagicMock(returncode=0, stdout="ghcr.io/r/dev-python:3.14\n")
    with patch("standard_tooling.lib.docker_cache.subprocess.run", return_value=mock_result):
        assert clean_branch_images("feature/99-other") == 0


def test_clean_branch_images_docker_error() -> None:
    mock_result = MagicMock(returncode=1, stdout="")
    with patch("standard_tooling.lib.docker_cache.subprocess.run", return_value=mock_result):
        assert clean_branch_images("feature/42") == 0


# -- _build_cached_image ------------------------------------------------------


def test_build_cached_image_success(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    start_result = MagicMock(returncode=0)
    commit_result = MagicMock(returncode=0)
    rm_result = MagicMock(returncode=0)

    calls: list[list[str]] = []

    def mock_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
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

    with patch("standard_tooling.lib.docker_cache.subprocess.run", side_effect=mock_run):
        result = _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash")
    assert result == "img:1--branch--hash"


def test_build_cached_image_create_fails(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=1, stderr="no space")
    with (
        patch("standard_tooling.lib.docker_cache.subprocess.run", return_value=create_result),
        pytest.raises(RuntimeError, match="Failed to create container"),
    ):
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash")


def test_build_cached_image_start_fails(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    start_result = MagicMock(returncode=1)
    rm_result = MagicMock(returncode=0)

    def mock_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        if cmd[1] == "create":
            return create_result
        if cmd[1] == "start":
            return start_result
        return rm_result

    with (
        patch("standard_tooling.lib.docker_cache.subprocess.run", side_effect=mock_run),
        pytest.raises(RuntimeError, match="Cache build failed"),
    ):
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash")


def test_build_cached_image_warmup_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)

    def mock_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        if cmd[1] == "create":
            return create_result
        return ok

    with patch("standard_tooling.lib.docker_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash")
    out = capsys.readouterr().out
    assert "Warmup:" in out


def test_build_cached_image_no_warmup_for_unknown_lang(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)

    def mock_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        if cmd[1] == "create":
            return create_result
        return ok

    with patch("standard_tooling.lib.docker_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(tmp_path, "unknown", "img:1", "img:1--branch--hash")
    out = capsys.readouterr().out
    assert "Warmup:" not in out


def test_build_cached_image_uses_uv_tool_install(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)
    create_cmd: list[str] = []

    def mock_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        if cmd[1] == "create":
            create_cmd.extend(cmd)
            return create_result
        return ok

    with patch("standard_tooling.lib.docker_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(tmp_path, "go", "img:1", "img:1--branch--hash")
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
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)

    with (
        patch("standard_tooling.lib.git.current_branch", return_value="develop"),
        patch("standard_tooling.lib.docker_cache.find_cached_image", return_value=None),
        patch(
            "standard_tooling.lib.docker_cache._build_cached_image",
            return_value="img:1--develop--hash",
        ) as mock_build,
    ):
        result = ensure_cached_image(tmp_path, "python", "img:1")
    mock_build.assert_called_once()
    assert result != "img:1"


def test_build_cached_image_python_includes_uv_install(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    create_result = MagicMock(returncode=0, stdout="abc123\n")
    ok = MagicMock(returncode=0)
    create_cmd: list[str] = []

    def mock_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        if cmd[1] == "create":
            create_cmd.extend(cmd)
            return create_result
        return ok

    with patch("standard_tooling.lib.docker_cache.subprocess.run", side_effect=mock_run):
        _build_cached_image(tmp_path, "python", "img:1", "img:1--branch--hash")
    setup_cmd = create_cmd[-1]
    assert "uv tool install" in setup_cmd
    assert "uv sync --group dev" in setup_cmd


def test_ensure_repo_name_included_in_hash(tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-alpha"
    repo_b = tmp_path / "repo-beta"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "standard-tooling.toml").write_text(_VALID_TOML)
    (repo_b / "standard-tooling.toml").write_text(_VALID_TOML)

    built_tags: list[str] = []

    def capture_build(repo_root, lang, base_image, target_tag) -> str:  # noqa: ANN001
        built_tags.append(target_tag)
        return target_tag

    with (
        patch("standard_tooling.lib.git.current_branch", return_value="develop"),
        patch("standard_tooling.lib.docker_cache.find_cached_image", return_value=None),
        patch("standard_tooling.lib.docker_cache._build_cached_image", side_effect=capture_build),
    ):
        ensure_cached_image(repo_a, "go", "img:1")
        ensure_cached_image(repo_b, "go", "img:1")

    assert len(built_tags) == 2
    assert built_tags[0] != built_tags[1], "repos with identical files must get distinct image tags"
