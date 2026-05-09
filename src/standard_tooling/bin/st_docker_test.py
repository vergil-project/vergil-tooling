"""Run a repository's test suite inside a dev container.

Auto-detects the project language from package manager files and selects
a default Docker image and test command.  All defaults can be overridden
via environment variables.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from standard_tooling.lib import git
from standard_tooling.lib.docker import (
    _DEFAULT_IMAGES,
    _DEFAULT_TEST_COMMANDS,
    build_docker_args,
    detect_language,
)

if TYPE_CHECKING:
    from pathlib import Path

_detect_language = detect_language


def build_test_docker_args(repo_root: Path, lang: str) -> list[str]:
    """Build the docker run argument list for test execution."""
    image = os.environ.get("DOCKER_DEV_IMAGE") or _DEFAULT_IMAGES.get(lang, "")
    test_cmd = os.environ.get("DOCKER_TEST_CMD") or _DEFAULT_TEST_COMMANDS.get(lang, "")
    network = os.environ.get("DOCKER_NETWORK", "")

    if not image:
        print(f"ERROR: no Docker image configured for language: {lang}", file=sys.stderr)
        sys.exit(1)

    if not test_cmd:
        print(f"ERROR: no test command configured for language: {lang}", file=sys.stderr)
        sys.exit(1)

    print(f"Language: {lang or '<none>'}")
    print(f"Image:    {image}")
    print(f"Command:  {test_cmd}")
    if network:
        print(f"Network:  {network}")
    print("---")

    return build_docker_args(repo_root, image, ["bash", "-c", test_cmd])


def _docker_is_available() -> bool:
    """Check whether the Docker daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "version"],  # noqa: S603, S607
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    repo_root = git.repo_root()
    lang = detect_language(repo_root)

    if not lang and not os.environ.get("DOCKER_DEV_IMAGE"):
        print(
            "ERROR: could not detect project language from repo contents.",
            file=sys.stderr,
        )
        print("Set DOCKER_DEV_IMAGE and DOCKER_TEST_CMD explicitly.", file=sys.stderr)
        return 1

    docker_args = build_test_docker_args(repo_root, lang)

    if not _docker_is_available():
        print(
            "ERROR: Docker is not available. Ensure the Docker daemon is running.",
            file=sys.stderr,
        )
        return 1

    os.execvp("docker", docker_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
