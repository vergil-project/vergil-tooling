"""Manage per-branch cached container images with vergil-tooling pre-installed.

Subcommands:

    build      Build (or refresh) the cached image for the current branch
    clean      Remove the cached image for the current branch
    status     Show cache state for the current branch
    clean-all  Remove all cached images managed by vrg-container-cache
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from vergil_tooling.lib import git
from vergil_tooling.lib.container import (
    assert_runtime_available,
    default_image,
    detect_language,
    detect_runtime,
)
from vergil_tooling.lib.container_cache import (
    cache_image_tag,
    cache_sensitive_files,
    compute_cache_hash,
    ensure_cached_image,
    find_cached_image,
)


def _cmd_build(_args: argparse.Namespace, *, runtime: str) -> int:
    repo_root = git.repo_root()
    lang = detect_language(repo_root)
    base = default_image(lang, fallback=True)
    assert_runtime_available(runtime)
    image = ensure_cached_image(repo_root, lang, base, runtime=runtime)
    if image == base:
        print("No caching applied (Python repo or no cache-sensitive files).")
    return 0


def _cmd_clean(_args: argparse.Namespace, *, runtime: str) -> int:
    repo_root = git.repo_root()
    lang = detect_language(repo_root)
    base = default_image(lang, fallback=True)
    branch = git.current_branch()
    existing = find_cached_image(base, branch, runtime=runtime)
    if existing is None:
        print("No cached image for this branch.")
        return 0
    subprocess.run(  # noqa: S603
        [runtime, "rmi", existing[0]],  # noqa: S607
        capture_output=True,
    )
    print(f"Removed: {existing[0]}")
    return 0


def _cmd_status(_args: argparse.Namespace, *, runtime: str) -> int:
    repo_root = git.repo_root()
    lang = detect_language(repo_root)
    base = default_image(lang, fallback=True)
    branch = git.current_branch()
    existing = find_cached_image(base, branch, runtime=runtime)
    if existing is None:
        print("No cached image for this branch.")
        files = cache_sensitive_files(repo_root, lang)
        if files:
            current_hash = compute_cache_hash(files)
            expected = cache_image_tag(base, branch, current_hash)
            print(f"Expected tag: {expected}")
        return 0
    print(f"Cached image: {existing[0]}")
    print(f"Hash:         {existing[1]}")
    files = cache_sensitive_files(repo_root, lang)
    if files:
        current_hash = compute_cache_hash(files)
        if current_hash == existing[1]:
            print("Status:       current")
        else:
            print(f"Status:       stale (current hash: {current_hash})")
    return 0


def _cmd_clean_all(_args: argparse.Namespace, *, runtime: str) -> int:
    result = subprocess.run(  # noqa: S603
        [runtime, "images", "--format", "{{.Repository}}:{{.Tag}}"],  # noqa: S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("Failed to list container images.", file=sys.stderr)
        return 1

    removed = 0
    for line in result.stdout.splitlines():
        if "--" in line.split(":")[-1]:
            subprocess.run(  # noqa: S603
                [runtime, "rmi", line],  # noqa: S607
                capture_output=True,
            )
            removed += 1

    print(f"Removed {removed} cached image(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-container-cache",
        description="Manage per-branch cached container images.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("build", help="Build or refresh the cached image")
    sub.add_parser("clean", help="Remove cached image for current branch")
    sub.add_parser("status", help="Show cache state for current branch")
    sub.add_parser("clean-all", help="Remove all cached images")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    runtime = detect_runtime()

    dispatch = {
        "build": _cmd_build,
        "clean": _cmd_clean,
        "status": _cmd_status,
        "clean-all": _cmd_clean_all,
    }
    return dispatch[args.command](args, runtime=runtime)


if __name__ == "__main__":
    sys.exit(main())
