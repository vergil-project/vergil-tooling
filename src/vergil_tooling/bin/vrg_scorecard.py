"""Run OpenSSF Scorecard inside a dev container with GitHub credentials.

Resolves the human account's GitHub token via ``gh auth`` and injects
it into the container.  All CLI arguments are passed through to the
``scorecard`` binary.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.config import ConfigError, read_config
from vergil_tooling.lib.docker import (
    assert_docker_available,
    build_docker_args,
)
from vergil_tooling.lib.github import _human_token

if TYPE_CHECKING:
    from pathlib import Path

_GHCR = "ghcr.io/vergil-project"

_USAGE = """\
usage: vrg-scorecard [scorecard-args...]

Run OpenSSF Scorecard inside a dev container with GitHub credentials.

All arguments are passed through to the scorecard CLI.  The human
account's GitHub token is resolved via gh auth and injected into the
container as GH_TOKEN.

examples:
  vrg-scorecard --repo=github.com/vergil-project/vergil-tooling
  vrg-scorecard --repo=github.com/vergil-project/vergil-tooling --format=json
  vrg-scorecard --repo=github.com/vergil-project/vergil-tooling --checks=Branch-Protection
"""


def _image_prefix(repo_root: Path) -> str:
    try:
        cfg = read_config(repo_root)
        return cfg.docker.image_prefix
    except (FileNotFoundError, ConfigError):
        return "prod"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    if {"-h", "--help"} & set(args):
        print(_USAGE, end="")
        return 0

    repo_root = git.repo_root()
    prefix = _image_prefix(repo_root)
    image = f"{_GHCR}/{prefix}-base:latest"

    token = _human_token()

    assert_docker_available()

    docker_args = build_docker_args(repo_root, image, ["scorecard", *args])
    idx = docker_args.index(image)
    docker_args[idx:idx] = ["-e", f"GH_TOKEN={token}"]

    os.execvp("docker", docker_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
