"""Run OpenSSF Scorecard inside a dev container with GitHub credentials.

Resolves a GitHub token via App installation token exchange and
injects it into the container.  All CLI arguments are passed through
to the ``scorecard`` binary.
"""

from __future__ import annotations

import os
import sys

from vergil_tooling.lib import git, github
from vergil_tooling.lib.docker import (
    assert_docker_available,
    build_docker_args,
)

_GHCR = "ghcr.io/vergil-project"

_VALID_PREFIXES = {"dev", "prod"}

_USAGE = """\
usage: vrg-scorecard [--prefix <dev|prod>] [scorecard-args...]

Run OpenSSF Scorecard inside a dev container with GitHub credentials.

All arguments after optional flags are passed through to the scorecard
CLI.  The human account's GitHub token is resolved via gh auth and
injected into the container as GH_TOKEN.

options:
  -h, --help              show this help message and exit
  --prefix <dev|prod>     image prefix (default: prod)

examples:
  vrg-scorecard --repo=github.com/vergil-project/vergil-tooling
  vrg-scorecard --prefix dev --repo=github.com/vergil-project/vergil-tooling
  vrg-scorecard --repo=github.com/vergil-project/vergil-tooling --format=json
"""


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    if {"-h", "--help"} & set(args):
        print(_USAGE, end="")
        return 0

    prefix = "prod"
    if "--prefix" in args:
        pi = args.index("--prefix")
        if pi + 1 >= len(args):
            print(
                "error: --prefix requires a value (dev or prod)",
                file=sys.stderr,
            )
            return 1
        prefix = args[pi + 1]
        if prefix not in _VALID_PREFIXES:
            allowed = ", ".join(sorted(_VALID_PREFIXES))
            print(
                f"error: invalid prefix '{prefix}' (allowed: {allowed})",
                file=sys.stderr,
            )
            return 1
        del args[pi : pi + 2]

    repo_root = git.repo_root()
    image = f"{_GHCR}/{prefix}-base:latest"

    token = github.get_installation_token()
    if token is None:
        print(
            "error: no GitHub App credentials configured; scorecard requires authentication",
            file=sys.stderr,
        )
        return 1

    assert_docker_available()

    docker_args = build_docker_args(repo_root, image, ["scorecard", *args])
    idx = docker_args.index(image)
    docker_args[idx:idx] = ["-e", f"GH_TOKEN={token}"]

    os.execvp("docker", docker_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
