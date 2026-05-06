"""Run an arbitrary command inside a dev container.

Auto-detects the project language to select a default Docker image.
Falls back to dev-base:latest when no language is detected and
DOCKER_DEV_IMAGE is not set.  The command to run is taken from CLI
arguments after ``--``.
"""

from __future__ import annotations

import os
import sys

from standard_tooling.lib import git
from standard_tooling.lib.docker import (
    assert_docker_available,
    build_docker_args,
    default_image,
    detect_language,
)
from standard_tooling.lib.docker_cache import ensure_cached_image

_USAGE = """\
usage: st-docker-run [--] <command> [args...]

Run a command inside the project's dev container.

The project language is auto-detected to select the right Docker image;
falls back to dev-base:latest when detection fails.

options:
  -h, --help          show this help message and exit

environment variables:
  GH_TOKEN                (required) GitHub token passed into the container
  DOCKER_DEV_IMAGE        override the auto-detected container image
  DOCKER_NETWORK          join a Docker network (e.g. for integration tests)
  ST_DOCKER_INSTALL_TAG   override the standard-tooling version tag from standard-tooling.toml

examples:
  st-docker-run -- uv run st-validate
  st-docker-run -- uv run pytest tests/
  DOCKER_DEV_IMAGE=custom:img st-docker-run -- make build
"""


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    # Split on -- separator
    if "--" in args:
        idx = args.index("--")
        pre_separator = args[:idx]
        command = args[idx + 1 :]
    else:
        pre_separator = args
        command = args

    if {"-h", "--help"} & set(pre_separator):
        print(_USAGE, end="")
        return 0

    if not command:
        print("Usage: st-docker-run [--] <command> [args...]", file=sys.stderr)
        return 1

    if not os.environ.get("GH_TOKEN"):
        print(
            "ERROR: GH_TOKEN is not set. Set GH_TOKEN in your environment before\n"
            "running st-docker-run. See docs/development/environment-setup.md.",
            file=sys.stderr,
        )
        return 1

    repo_root = git.repo_root()
    lang = detect_language(repo_root)

    env_image = os.environ.get("DOCKER_DEV_IMAGE")
    if env_image:
        image = env_image
        image_source = "env"
    else:
        base = default_image(lang, fallback=True)
        image = ensure_cached_image(repo_root, lang, base)
        image_source = "cached" if image != base else "default"

    print(f"Language: {lang or '<none>'}")
    if image_source == "cached":
        print(f"Image:    {image} (cached)")
    else:
        print(f"Image:    {image}")
    print(f"Command:  {' '.join(command)}")
    network = os.environ.get("DOCKER_NETWORK", "")
    if network:
        print(f"Network:  {network}")
    print("---")

    assert_docker_available()

    pull_policy = "never" if image_source == "cached" else "always"
    docker_args = build_docker_args(repo_root, image, command, pull_policy=pull_policy)
    os.execvp("docker", docker_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
