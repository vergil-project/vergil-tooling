"""Shared Docker container logic for vrg-docker-* commands."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

_GHCR = "ghcr.io/vergil-project"

_DEFAULT_VERSIONS: dict[str, str] = {
    "ruby": "3.4",
    "python": "3.14",
    "go": "1.26",
    "rust": "1.93",
    "java": "21",
}

_DEFAULT_PREFIX = "prod"

_DEFAULT_TEST_COMMANDS: dict[str, str] = {
    "ruby": "bundle install --jobs 4 && bundle exec rake",
    "python": "uv sync && uv run pytest tests/ -v",
    "go": "go test ./...",
    "rust": "cargo test",
    "java": "./mvnw verify",
}


def _fallback_image(prefix: str) -> str:
    return f"{_GHCR}/{prefix}-base:latest"


_MACHINE_TO_PLATFORM: dict[str, str] = {
    "arm64": "linux/arm64",
    "aarch64": "linux/arm64",
    "x86_64": "linux/amd64",
    "AMD64": "linux/amd64",
}


def docker_platform() -> str:
    """Return the Docker ``--platform`` value for the host architecture."""
    return _MACHINE_TO_PLATFORM.get(platform.machine(), "linux/amd64")


def detect_language(repo_root: Path) -> str:
    """Detect the project language from repo contents."""
    if (repo_root / "Gemfile").is_file():
        return "ruby"
    if (repo_root / "pyproject.toml").is_file():
        return "python"
    if (repo_root / "go.mod").is_file():
        return "go"
    if (repo_root / "Cargo.toml").is_file():
        return "rust"
    if (repo_root / "pom.xml").is_file() or (repo_root / "mvnw").is_file():
        return "java"
    return ""


def default_image(lang: str, *, fallback: bool = False, prefix: str = _DEFAULT_PREFIX) -> str:
    """Return the default Docker image for a language.

    When *fallback* is True, return the base image if no language
    matches instead of returning an empty string.
    """
    version = _DEFAULT_VERSIONS.get(lang, "")
    if not version and fallback:
        return _fallback_image(prefix)
    if not version:
        return ""
    return f"{_GHCR}/{prefix}-{lang}:{version}"


def worktree_parent_gitdir(repo_root: Path) -> Path | None:
    """Return the parent repo's ``.git`` directory if *repo_root* is a worktree.

    A git worktree's ``.git`` is a one-line file pointing at the parent
    repo's ``<.git>/worktrees/<name>`` directory; the parent's ``.git``
    must be visible inside the container at the same absolute path for
    the pointer to resolve. The main worktree's ``.git`` is a directory,
    so this returns ``None`` for it.

    Returns ``None`` when the layout is unrecognized rather than raising
    — the caller falls back to the existing single-mount behavior so
    container launches do not regress on edge cases (issue #293).
    """
    marker = repo_root / ".git"
    if not marker.is_file():
        return None
    try:
        content = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content.removeprefix("gitdir:").strip())
    if gitdir.parent.name != "worktrees":
        return None
    return gitdir.parent.parent


def build_docker_args(
    repo_root: Path,
    image: str,
    command: list[str],
    *,
    pull_policy: str = "always",
) -> list[str]:
    """Build the ``docker run`` argument list."""
    network = os.environ.get("DOCKER_NETWORK", "")

    docker_args = ["docker", "run", "--rm", f"--platform={docker_platform()}"]
    if pull_policy != "never":
        docker_args.append("--pull=always")
    docker_args.extend(
        [
            "-v",
            f"{repo_root}:/workspace",
            "-w",
            "/workspace",
        ]
    )

    # When repo_root is a git worktree, the worktree's `.git` is a file
    # pointing at <parent>/.git/worktrees/<name>. Mount the parent .git
    # at the same absolute path so the pointer resolves in-container.
    # Without this, every git command in the container fails (#293).
    parent_gitdir = worktree_parent_gitdir(repo_root)
    if parent_gitdir is not None:
        docker_args.extend(["-v", f"{parent_gitdir}:{parent_gitdir}"])

    if network:
        docker_args.extend(["--network", network])

    extra_volumes = os.environ.get("DOCKER_EXTRA_VOLUMES", "")
    if extra_volumes:
        for vol in extra_volumes.split(";"):
            vol = vol.strip()
            if vol:
                docker_args.extend(["-v", vol])

    for name in os.environ:
        if name.startswith(("MQ_", "GH_", "GITHUB_")):
            docker_args.extend(["-e", name])

    # Mount host git config so git identity is available in the container.
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        docker_args.extend(["-v", f"{gitconfig}:/root/.gitconfig:ro"])

    # Mount host SSH directory so git can authenticate for remote operations.
    ssh_dir = Path.home() / ".ssh"
    if ssh_dir.is_dir():
        docker_args.extend(["-v", f"{ssh_dir}:/root/.ssh:ro"])

    docker_args.append(image)
    docker_args.extend(command)

    return docker_args


def assert_docker_available() -> None:
    """Exit with an error if the Docker daemon is not reachable."""
    try:
        result = subprocess.run(
            ["docker", "version"],  # noqa: S603, S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if result.returncode != 0:
            _docker_unavailable()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _docker_unavailable()


def _docker_unavailable() -> None:
    print(
        "ERROR: Docker is not available. Ensure the Docker daemon is running.",
        file=sys.stderr,
    )
    sys.exit(1)
