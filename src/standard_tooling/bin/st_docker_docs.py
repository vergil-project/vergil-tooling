"""Preview or build MkDocs documentation inside a dev container.

Supports ``serve`` and ``build`` subcommands.  For Python repos, wraps
commands with ``uv sync --group docs && uv run ...`` so that
mkdocstrings plugins resolve correctly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from standard_tooling.lib import git
from standard_tooling.lib.config import ConfigError, read_config
from standard_tooling.lib.docker import docker_platform


def _usage(port: str) -> None:
    print("Usage: docker-docs <serve|build> [mkdocs args...]")
    print()
    print("Commands:")
    print(f"  serve   Start a live-reloading preview server (port {port})")
    print("  build   Build the static documentation site")
    print()
    print("Environment variables:")
    print("  DOCKER_DOCS_IMAGE  Docker image (default: ghcr.io/wphillipmoore/prod-base:latest)")
    print("  MKDOCS_CONFIG      Path to mkdocs.yml (default: docs/site/mkdocs.yml)")
    print("  DOCS_PORT          Host port for serve (default: 8000)")


def _docs_image(repo_root: Path) -> str:
    env_image = os.environ.get("DOCKER_DOCS_IMAGE")
    if env_image:
        return env_image
    try:
        cfg = read_config(repo_root)
        prefix = cfg.docker.image_prefix
    except (FileNotFoundError, ConfigError):
        prefix = "prod"
    return f"ghcr.io/wphillipmoore/{prefix}-base:latest"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    config = os.environ.get("MKDOCS_CONFIG", "docs/site/mkdocs.yml")
    port = os.environ.get("DOCS_PORT", "8000")

    if not args:
        _usage(port)
        return 1

    command = args[0]
    extra_args = args[1:]

    if command == "serve":
        mkdocs_cmd = f"mkdocs serve -f {config} -a 0.0.0.0:8000"
    elif command == "build":
        mkdocs_cmd = f"mkdocs build -f {config}"
    else:
        print(f"ERROR: unknown command: {command}", file=sys.stderr)
        _usage(port)
        return 1

    if extra_args:
        mkdocs_cmd += " " + " ".join(extra_args)

    repo_root = git.repo_root()
    image = _docs_image(repo_root)

    container_cmd = mkdocs_cmd
    if (repo_root / "pyproject.toml").is_file():
        container_cmd = f"uv sync --group docs && uv run {mkdocs_cmd}"

    docker_args = [
        "docker",
        "run",
        "--rm",
        f"--platform={docker_platform()}",
        "-v",
        f"{repo_root}:/workspace",
        "-w",
        "/workspace",
    ]

    if command == "serve":
        docker_args.extend(["-p", f"{port}:8000"])

    common_dir = repo_root / ".." / "mq-rest-admin-common"
    if common_dir.is_dir():
        real_common = str(common_dir.resolve())
        docker_args.extend(["-v", f"{real_common}:/workspace/.mq-rest-admin-common:ro"])

    docker_args.append(image)
    docker_args.extend(["bash", "-c", container_cmd])

    print(f"Image:   {image}")
    print(f"Config:  {config}")
    print(f"Command: {container_cmd}")
    if command == "serve":
        print(f"URL:     http://localhost:{port}")
    print("---")

    os.execvp("docker", docker_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
