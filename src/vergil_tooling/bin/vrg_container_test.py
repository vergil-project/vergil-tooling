"""Run a repository's test suite inside a dev container.

Auto-detects the project language from package manager files and selects
a default container image and test command.  All defaults can be overridden
via environment variables.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.config import container_env_prefixes
from vergil_tooling.lib.container import (
    _DEFAULT_TEST_COMMANDS,
    build_container_args,
    default_image,
    detect_language,
    detect_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_detect_language = detect_language


def build_test_container_args(
    repo_root: Path,
    lang: str,
    *,
    runtime: str = "docker",
    env_prefixes: Sequence[str] = (),
) -> list[str]:
    """Build the container run argument list for test execution."""
    image = os.environ.get("DOCKER_DEV_IMAGE") or default_image(lang)
    test_cmd = os.environ.get("DOCKER_TEST_CMD") or _DEFAULT_TEST_COMMANDS.get(lang, "")
    network = os.environ.get("DOCKER_NETWORK", "")

    if not image:
        print(f"ERROR: no container image configured for language: {lang}", file=sys.stderr)
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

    return build_container_args(
        repo_root, image, ["bash", "-c", test_cmd], runtime=runtime, env_prefixes=env_prefixes
    )


# Keep backward-compatible alias
build_test_docker_args = build_test_container_args


def _runtime_is_available(runtime: str) -> bool:
    """Check whether the container runtime is reachable."""
    try:
        result = subprocess.run(  # noqa: S603, S607
            [runtime, "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Keep backward-compatible alias
_docker_is_available = _runtime_is_available


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    runtime = detect_runtime()
    repo_root = git.repo_root()
    lang = detect_language(repo_root)

    if not lang and not os.environ.get("DOCKER_DEV_IMAGE"):
        print(
            "ERROR: could not detect project language from repo contents.",
            file=sys.stderr,
        )
        print("Set DOCKER_DEV_IMAGE and DOCKER_TEST_CMD explicitly.", file=sys.stderr)
        return 1

    env_prefixes = container_env_prefixes(repo_root)
    container_args = build_test_container_args(
        repo_root, lang, runtime=runtime, env_prefixes=env_prefixes
    )

    if not _runtime_is_available(runtime):
        print(
            f"ERROR: {runtime} is not available. Ensure the container runtime is running.",
            file=sys.stderr,
        )
        return 1

    if runtime == "nerdctl":
        os.execvp("nerdctl", container_args)  # noqa: S606, S607
    else:
        os.execvp("docker", container_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
