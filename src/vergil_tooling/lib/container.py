"""Shared container logic for vrg-container-* commands."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def detect_runtime() -> str:
    """Return 'nerdctl' if available, else 'docker'."""
    if shutil.which("nerdctl"):
        return "nerdctl"
    if shutil.which("docker"):
        return "docker"
    print("ERROR: no container runtime found (need docker or nerdctl)", file=sys.stderr)
    raise SystemExit(1)


def _fallback_image(prefix: str) -> str:
    return f"{_GHCR}/{prefix}-base:latest"


_MACHINE_TO_PLATFORM: dict[str, str] = {
    "arm64": "linux/arm64",
    "aarch64": "linux/arm64",
    "x86_64": "linux/amd64",
    "AMD64": "linux/amd64",
}


def container_platform() -> str:
    """Return the container ``--platform`` value for the host architecture."""
    return _MACHINE_TO_PLATFORM.get(platform.machine(), "linux/amd64")


# Keep backward-compatible alias
docker_platform = container_platform


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


def default_image(
    lang: str,
    *,
    fallback: bool = False,
    prefix: str = _DEFAULT_PREFIX,
    version: str | None = None,
) -> str:
    """Return the default container image for a language.

    When *fallback* is True, return the base image if no language
    matches instead of returning an empty string.

    *version* overrides the built-in ``_DEFAULT_VERSIONS`` entry for *lang*
    (issue #2468). Callers pass the repo's declared ``[ci].versions`` primary
    (via :func:`config.primary_ci_version`) so the repo — not a tooling-side
    constant — picks the container tag. When it is ``None`` (or empty), the
    built-in default is used.
    """
    # A declared *version* overrides only for a language that has a per-language
    # image. A language-less/unknown repo has no `prod-<lang>` image, so it always
    # uses the base — a declared [ci].versions must not resurrect a malformed
    # `prod-:<version>` tag (#2475, the #2468 regression).
    default_version = _DEFAULT_VERSIONS.get(lang, "")
    if not default_version:
        return _fallback_image(prefix) if fallback else ""
    resolved = version or default_version
    return f"{_GHCR}/{prefix}-{lang}:{resolved}"


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


def workspace_mount_args(repo_root: Path) -> list[str]:
    """Return the workspace bind-mount + working dir, plus a fresh anonymous
    volume masking the venv for Python repos so in-container uv never rewrites
    the bind-mounted host .venv (#2473/#2495). Used by BOTH the run path and
    the cache-build path — one source of truth so a new mount site can't
    silently reintroduce the corruption."""
    args = ["-v", f"{repo_root}:/workspace", "-w", "/workspace"]
    if detect_language(repo_root) == "python":
        args += ["-v", "/workspace/.venv"]
    return args


def build_container_args(
    repo_root: Path,
    image: str,
    command: list[str],
    *,
    runtime: str = "docker",
    pull_policy: str = "always",
    env_prefixes: Sequence[str] = (),
) -> list[str]:
    """Build the container ``run`` argument list."""
    network = os.environ.get("DOCKER_NETWORK", "")

    container_args = [runtime, "run", "--rm", f"--platform={container_platform()}"]
    if pull_policy != "never":
        container_args.append("--pull=always")

    # The workspace bind-mount, working dir, and the Python-gated `.venv` mask
    # come from one shared helper so the run path and the cache-build path
    # (container_cache.py) can never drift on venv protection (#2486/#2495).
    container_args.extend(workspace_mount_args(repo_root))

    # When repo_root is a git worktree, the worktree's `.git` is a file
    # pointing at <parent>/.git/worktrees/<name>. Mount the parent .git
    # at the same absolute path so the pointer resolves in-container.
    # Without this, every git command in the container fails (#293).
    parent_gitdir = worktree_parent_gitdir(repo_root)
    if parent_gitdir is not None:
        container_args.extend(["-v", f"{parent_gitdir}:{parent_gitdir}"])

    if network:
        container_args.extend(["--network", network])

    extra_volumes = os.environ.get("DOCKER_EXTRA_VOLUMES", "")
    if extra_volumes:
        for vol in extra_volumes.split(";"):
            vol = vol.strip()
            if vol:
                container_args.extend(["-v", vol])

    if env_prefixes:
        prefixes = tuple(env_prefixes)
        for name in os.environ:
            if name.startswith(prefixes):
                container_args.extend(["-e", name])

    # uv's cache (`/root/.cache/uv`, container overlay fs) and the venv target
    # (`/workspace/.venv`, the bind-mounted repo) live on different filesystems,
    # so uv cannot hardlink and falls back to a full copy, printing a warning on
    # every install in every run. Pin the link mode to copy to suppress that
    # noise — uv already copies here, so this changes no behaviour. An explicit
    # host UV_LINK_MODE wins, so an operator can still override it. (#2461)
    uv_link_mode = os.environ.get("UV_LINK_MODE", "copy")
    container_args.extend(["-e", f"UV_LINK_MODE={uv_link_mode}"])

    # Mount host git config so git identity is available in the container.
    gitconfig = Path.home() / ".gitconfig"
    if gitconfig.exists():
        container_args.extend(["-v", f"{gitconfig}:/root/.gitconfig:ro"])

    # Mount host SSH directory so git can authenticate for remote operations.
    ssh_dir = Path.home() / ".ssh"
    if ssh_dir.is_dir():
        container_args.extend(["-v", f"{ssh_dir}:/root/.ssh:ro"])

    container_args.append(image)
    container_args.extend(command)

    return container_args


# Keep backward-compatible alias for existing callers
def build_docker_args(
    repo_root: Path,
    image: str,
    command: list[str],
    *,
    pull_policy: str = "always",
    env_prefixes: Sequence[str] = (),
) -> list[str]:
    """Build the container ``run`` argument list (legacy alias)."""
    return build_container_args(
        repo_root,
        image,
        command,
        runtime="docker",
        pull_policy=pull_policy,
        env_prefixes=env_prefixes,
    )


def assert_runtime_available(runtime: str) -> None:
    """Exit with an error if the container runtime is not reachable."""
    try:
        result = subprocess.run(  # noqa: S603
            [runtime, "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if result.returncode != 0:
            _runtime_unavailable(runtime)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _runtime_unavailable(runtime)


# Keep backward-compatible alias
def assert_docker_available() -> None:
    """Exit with an error if the Docker daemon is not reachable."""
    assert_runtime_available("docker")


def _runtime_unavailable(runtime: str) -> None:
    print(
        f"ERROR: {runtime} is not available. Ensure the container runtime is running.",
        file=sys.stderr,
    )
    sys.exit(1)
