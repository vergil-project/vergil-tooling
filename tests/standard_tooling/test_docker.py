"""Tests for standard_tooling.lib.docker."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from standard_tooling.lib.docker import (
    assert_docker_available,
    build_docker_args,
    default_image,
    detect_language,
    docker_platform,
    worktree_parent_gitdir,
)

if TYPE_CHECKING:
    from pathlib import Path


# -- detect_language ----------------------------------------------------------


def test_detect_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert detect_language(tmp_path) == "python"


def test_detect_ruby(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    assert detect_language(tmp_path) == "ruby"


def test_detect_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    assert detect_language(tmp_path) == "go"


def test_detect_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_language(tmp_path) == "rust"


def test_detect_java_pom(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>\n")
    assert detect_language(tmp_path) == "java"


def test_detect_java_mvnw(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/bash\n")
    assert detect_language(tmp_path) == "java"


def test_detect_none(tmp_path: Path) -> None:
    assert detect_language(tmp_path) == ""


def test_detect_ruby_priority(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("")
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_language(tmp_path) == "ruby"


# -- default_image ------------------------------------------------------------


def test_default_image_known_lang() -> None:
    assert "prod-python" in default_image("python")


def test_default_image_unknown_no_fallback() -> None:
    assert default_image("unknown") == ""


def test_default_image_unknown_with_fallback() -> None:
    assert default_image("unknown", fallback=True) == "ghcr.io/wphillipmoore/prod-base:latest"


def test_default_image_empty_with_fallback() -> None:
    assert default_image("", fallback=True) == "ghcr.io/wphillipmoore/prod-base:latest"


# -- prefix-aware default_image -----------------------------------------------


def test_default_image_prod_prefix() -> None:
    img = default_image("python", prefix="prod")
    assert "prod-python" in img
    assert "dev-python" not in img


def test_default_image_dev_prefix() -> None:
    img = default_image("python", prefix="dev")
    assert "dev-python" in img


def test_default_image_default_prefix_is_prod() -> None:
    img = default_image("python")
    assert "prod-python" in img


def test_default_image_fallback_respects_prefix() -> None:
    img = default_image("unknown", fallback=True, prefix="prod")
    assert "prod-base" in img


def test_default_image_fallback_dev_prefix() -> None:
    img = default_image("unknown", fallback=True, prefix="dev")
    assert "dev-base" in img


# -- docker_platform ----------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
        ("x86_64", "linux/amd64"),
        ("AMD64", "linux/amd64"),
    ],
)
def test_docker_platform_known(machine: str, expected: str) -> None:
    with patch("standard_tooling.lib.docker.platform.machine", return_value=machine):
        assert docker_platform() == expected


def test_docker_platform_unknown_defaults_to_amd64() -> None:
    with patch("standard_tooling.lib.docker.platform.machine", return_value="riscv64"):
        assert docker_platform() == "linux/amd64"


# -- build_docker_args --------------------------------------------------------


def test_build_docker_args_basic(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["echo", "hello"])
    assert args[0:3] == ["docker", "run", "--rm"]
    assert any(a.startswith("--platform=linux/") for a in args)
    assert "--pull=always" in args
    assert f"{tmp_path}:/workspace" in args
    assert "img:1" in args
    assert args[-2:] == ["echo", "hello"]


def test_build_docker_args_network(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"DOCKER_NETWORK": "mynet"}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "--network" in args
    assert "mynet" in args


def test_build_docker_args_extra_volumes(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"DOCKER_EXTRA_VOLUMES": "/a:/b;/c:/d"}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "/a:/b" in args
    assert "/c:/d" in args


def test_build_docker_args_empty_extra_volumes(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {"DOCKER_EXTRA_VOLUMES": ";"}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    v_indices = [i for i, a in enumerate(args) if a == "-v"]
    assert len(v_indices) == 1


def test_build_docker_args_env_passthrough(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "GH_TOKEN": "tok", "GITHUB_SHA": "abc"}
    with patch.dict("os.environ", env, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "MQ_HOST" in args
    assert "GH_TOKEN" in args
    assert "GITHUB_SHA" in args


def test_build_docker_args_no_unrelated_env(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"HOME": "/home/user"}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "HOME" not in args


def test_build_docker_args_mounts_gitconfig(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    gitconfig = fake_home / ".gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert f"{gitconfig}:/root/.gitconfig:ro" in args


def test_build_docker_args_mounts_ssh_dir(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("key\n")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert f"{ssh_dir}:/root/.ssh:ro" in args


def test_build_docker_args_no_ssh_dir(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert all("/root/.ssh" not in a for a in args)


def test_build_docker_args_no_gitconfig(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert all("/root/.gitconfig" not in a for a in args)


# -- assert_docker_available --------------------------------------------------


def test_assert_docker_available_success() -> None:
    mock_result = MagicMock(returncode=0)
    with patch("standard_tooling.lib.docker.subprocess.run", return_value=mock_result):
        assert_docker_available()  # should not raise


def test_assert_docker_available_failure() -> None:
    mock_result = MagicMock(returncode=1)
    with (
        patch("standard_tooling.lib.docker.subprocess.run", return_value=mock_result),
        pytest.raises(SystemExit),
    ):
        assert_docker_available()


def test_assert_docker_available_not_installed() -> None:
    with (
        patch("standard_tooling.lib.docker.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(SystemExit),
    ):
        assert_docker_available()


def test_assert_docker_available_timeout() -> None:
    with (
        patch(
            "standard_tooling.lib.docker.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker version", timeout=15),
        ),
        pytest.raises(SystemExit),
    ):
        assert_docker_available()


# -- worktree_parent_gitdir ---------------------------------------------------


def test_worktree_parent_gitdir_main_worktree(tmp_path: Path) -> None:
    """Main worktree has `.git` as a directory; returns None."""
    (tmp_path / ".git").mkdir()
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_no_git_at_all(tmp_path: Path) -> None:
    """No `.git` present; returns None (defensive)."""
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_secondary_worktree(tmp_path: Path) -> None:
    """`.git` file points at <parent>/.git/worktrees/<name>; returns parent .git."""
    parent_git = tmp_path / "main-repo" / ".git"
    parent_git.mkdir(parents=True)
    worktree_metadata = parent_git / "worktrees" / "issue-1-x"
    worktree_metadata.mkdir(parents=True)
    worktree = tmp_path / "main-repo" / ".worktrees" / "issue-1-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {worktree_metadata}\n", encoding="utf-8")

    assert worktree_parent_gitdir(worktree) == parent_git


def test_worktree_parent_gitdir_malformed_no_gitdir_prefix(tmp_path: Path) -> None:
    """Unexpected file content; returns None (don't crash)."""
    (tmp_path / ".git").write_text("not a real gitdir pointer\n", encoding="utf-8")
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_unrecognized_layout(tmp_path: Path) -> None:
    """`.git` points somewhere that isn't `<parent>/worktrees/<name>`; returns None."""
    target = tmp_path / "elsewhere" / "custom-path"
    target.mkdir(parents=True)
    (tmp_path / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_oserror_on_read(tmp_path: Path) -> None:
    """Unreadable `.git` file (permissions, race, etc.) returns None safely."""
    (tmp_path / ".git").write_text("gitdir: /irrelevant\n", encoding="utf-8")
    with patch(
        "standard_tooling.lib.docker.Path.read_text",
        side_effect=OSError("permission denied"),
    ):
        assert worktree_parent_gitdir(tmp_path) is None


def test_build_docker_args_mounts_parent_git_when_worktree(tmp_path: Path) -> None:
    """Issue #293: secondary worktree triggers an extra parent-.git mount
    so the worktree's `.git` gitdir pointer resolves inside the container.
    """
    parent_git = tmp_path / "main-repo" / ".git"
    parent_git.mkdir(parents=True)
    worktree_metadata = parent_git / "worktrees" / "issue-1-x"
    worktree_metadata.mkdir(parents=True)
    worktree = tmp_path / "main-repo" / ".worktrees" / "issue-1-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {worktree_metadata}\n", encoding="utf-8")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(worktree, "img:1", ["cmd"])

    assert f"{parent_git}:{parent_git}" in args


def test_build_docker_args_no_extra_mount_for_main_worktree(tmp_path: Path) -> None:
    """Main worktree (`.git` is a directory) gets no extra mount."""
    (tmp_path / ".git").mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("standard_tooling.lib.docker.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])

    # Only the workspace mount; no parent-.git mount.
    v_indices = [i for i, a in enumerate(args) if a == "-v"]
    assert len(v_indices) == 1
    assert args[v_indices[0] + 1] == f"{tmp_path}:/workspace"


# -- pull policy --------------------------------------------------------------


def test_build_docker_args_pull_always_by_default(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "--pull=always" in args


def test_build_docker_args_pull_never_omits_pull_flag(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"], pull_policy="never")
    assert "--pull=always" not in args
    assert not any(a.startswith("--pull=") for a in args)
