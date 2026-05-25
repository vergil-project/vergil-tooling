"""Run an arbitrary command inside a dev container.

Auto-detects the project language to select a default container image.
Falls back to dev-base:latest when no language is detected and
DOCKER_DEV_IMAGE is not set.  The command to run is taken from CLI
arguments after ``--``.
"""

from __future__ import annotations

import os
import sys

from vergil_tooling.lib import git
from vergil_tooling.lib.config import container_env_prefixes
from vergil_tooling.lib.container import (
    assert_runtime_available,
    build_container_args,
    default_image,
    detect_language,
    detect_runtime,
)
from vergil_tooling.lib.container_cache import ensure_cached_image

_VALID_PREFIXES = {"dev", "prod"}

_USAGE = """\
usage: vrg-container-run [--prefix <dev|prod>] [--] <command> [args...]

Run a command inside the project's dev container.

The project language is auto-detected to select the right container image;
falls back to dev-base:latest when detection fails.

options:
  -h, --help              show this help message and exit
  --prefix <dev|prod>     image prefix (default: prod)

environment variables:
  DOCKER_DEV_IMAGE        override the auto-detected container image
  DOCKER_NETWORK          join a Docker network (e.g. for integration tests)
  VRG_DOCKER_INSTALL_TAG   override the vergil-tooling version tag from vergil.toml

examples:
  vrg-container-run -- uv run vrg-validate
  vrg-container-run --prefix dev -- vrg-validate
  vrg-container-run -- uv run pytest tests/
  DOCKER_DEV_IMAGE=custom:img vrg-container-run -- make build
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

    prefix = "prod"
    if "--prefix" in pre_separator:
        pi = pre_separator.index("--prefix")
        if pi + 1 >= len(pre_separator):
            print("error: --prefix requires a value (dev or prod)", file=sys.stderr)
            return 1
        prefix = pre_separator[pi + 1]
        if prefix not in _VALID_PREFIXES:
            allowed = ", ".join(sorted(_VALID_PREFIXES))
            print(f"error: invalid prefix '{prefix}' (allowed: {allowed})", file=sys.stderr)
            return 1

    if not command:
        print("error: no command specified", file=sys.stderr)
        return 1

    runtime = detect_runtime()
    repo_root = git.repo_root()
    lang = detect_language(repo_root)

    env_image = os.environ.get("DOCKER_DEV_IMAGE")
    if env_image:
        image = env_image
        image_source = "env"
    else:
        base = default_image(lang, fallback=True, prefix=prefix)
        image = ensure_cached_image(repo_root, lang, base, runtime=runtime)
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

    assert_runtime_available(runtime)

    env_prefixes = container_env_prefixes(repo_root)
    pull_policy = "never" if image_source == "cached" else "always"
    container_args = build_container_args(
        repo_root,
        image,
        command,
        runtime=runtime,
        pull_policy=pull_policy,
        env_prefixes=env_prefixes,
    )
    if runtime == "nerdctl":
        os.execvp("nerdctl", container_args)  # noqa: S606, S607
    else:
        os.execvp("docker", container_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
