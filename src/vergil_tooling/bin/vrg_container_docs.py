"""Preview or build MkDocs documentation inside a dev container.

Supports ``serve`` and ``build`` subcommands.  For Python repos, wraps
commands with ``uv sync --group docs && uv run ...`` so that
mkdocstrings plugins resolve correctly.
"""

from __future__ import annotations

import os
import sys

from vergil_tooling.lib import git
from vergil_tooling.lib.container import build_container_args, detect_runtime, validated_runtime

_VALID_PREFIXES = {"dev", "prod"}


def _usage(port: str) -> None:
    print("Usage: vrg-container-docs [--prefix <dev|prod>] <serve|build> [mkdocs args...]")
    print()
    print("Commands:")
    print(f"  serve   Start a live-reloading preview server (port {port})")
    print("  build   Build the static documentation site")
    print()
    print("Options:")
    print("  --prefix <dev|prod>  image prefix (default: prod)")
    print()
    print("Environment variables:")
    print("  DOCKER_DOCS_IMAGE  Container image (override; ignores --prefix)")
    print("  MKDOCS_CONFIG      Path to mkdocs.yml (default: docs/site/mkdocs.yml)")
    print("  DOCS_PORT          Host port for serve (default: 8000)")


def _docs_image(prefix: str) -> str:
    return os.environ.get(
        "DOCKER_DOCS_IMAGE",
        f"ghcr.io/vergil-project/{prefix}-base:latest",
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    config = os.environ.get("MKDOCS_CONFIG", "docs/site/mkdocs.yml")
    port = os.environ.get("DOCS_PORT", "8000")

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

    runtime = detect_runtime()
    repo_root = git.repo_root()
    image = _docs_image(prefix)

    container_cmd = mkdocs_cmd
    if (repo_root / "pyproject.toml").is_file():
        container_cmd = f"uv sync --group docs && uv run {mkdocs_cmd}"

    container_args = build_container_args(
        repo_root,
        image,
        ["bash", "-c", container_cmd],
        runtime=runtime,
    )

    if command == "serve":
        idx = container_args.index(image)
        container_args[idx:idx] = ["-p", f"{port}:8000"]

    print(f"Image:   {image}")
    print(f"Config:  {config}")
    print(f"Command: {container_cmd}")
    if command == "serve":
        print(f"URL:     http://localhost:{port}")
    print("---")

    os.execvp(validated_runtime(runtime), container_args)  # noqa: S606, S607
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
