"""Changelog and release notes generation via git-cliff.

A release's changes are the commits on the release branch that the previous
release does not contain: the range from the merge base of ``HEAD`` and the
previous ``vX.Y.Z`` tag (:func:`release_boundary`) to ``HEAD``.

The ``develop-vX.Y.Z`` tags are not the boundary. CD places one on the
``develop`` tip after the release has merged, so a PR merged to ``develop``
while the release was in flight sits behind it although ``vX.Y.Z`` does not
contain it, and a tag-bounded range would drop it from both releases (#3805).
git-cliff is therefore given the commit range explicitly and told to ignore
those tags; ``--tag`` only names the new release.

git-cliff is not a host dependency: it runs in the repo's dev container
through ``vrg-container-run``, which mounts the repo root as the container's
working directory (and a worktree's parent ``.git``, so history resolves). The
output paths are therefore given relative to the repo root, and the bundled
cliff configs, which live in the host's vergil-tooling install, are mounted
read-only at :data:`CONFIG_MOUNT` (#4169).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from importlib.resources import as_file, files
from typing import TYPE_CHECKING

from vergil_tooling.lib import git

if TYPE_CHECKING:
    from pathlib import Path

RELEASE_NOTES_DIR = "releases"
CHANGELOG_FILE = "CHANGELOG.md"

# Where the bundled cliff configs are mounted, read-only, in the dev container.
CONFIG_MOUNT = "/opt/vergil-cliff"

# The develop-side boundary tags, as cliff.toml's tag_pattern matches them.
BOUNDARY_TAG_PATTERN = "develop-v[0-9].*"

_RELEASE_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_release(version: str) -> tuple[int, int, int]:
    match = _RELEASE_VERSION.fullmatch(version)
    if match is None:
        msg = f"not a release version (X.Y.Z): {version!r}"
        raise ValueError(msg)
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def previous_release_tag(repo_root: Path, version: str) -> str | None:
    """Return the highest ``vX.Y.Z`` tag below *version*, or ``None`` if there is none.

    Rolling tags (``v1.3``), boundary tags (``develop-v…``) and pre-release
    suffixes are not releases and are never chosen.
    """
    current = _parse_release(version)
    output = git.read_output(
        "-C", str(repo_root), "for-each-ref", "--format=%(refname:lstrip=2)", "refs/tags/v*"
    )
    releases = [
        (_parse_release(tag[1:]), tag)
        for tag in output.splitlines()
        if _RELEASE_VERSION.fullmatch(tag[1:])
    ]
    earlier = [release for release in releases if release[0] < current]
    return max(earlier)[1] if earlier else None


def release_boundary(repo_root: Path, version: str) -> str | None:
    """Return the commit after which ``HEAD``'s commits belong to release *version*.

    It is the merge base of ``HEAD`` and the previous release's tag: the tag's
    own commit once ``main`` has been merged back into ``develop``, the commit
    the release branch was cut from before that. ``None`` means there is no
    previous release, so every commit belongs to *version*.
    """
    tag = previous_release_tag(repo_root, version)
    if tag is None:
        return None
    return git.read_output(
        "-C", str(repo_root), "merge-base", "HEAD", f"refs/tags/{tag}^{{commit}}"
    )


def _tag_args(version: str) -> tuple[str, ...]:
    return ("--tag", f"develop-v{version}", "--ignore-tags", BOUNDARY_TAG_PATTERN)


def _range(base: str | None) -> tuple[str, ...]:
    """Return git-cliff's commit-range argument: none (all history) without a *base*."""
    return () if base is None else (f"{base}..HEAD",)


def _published_changelog(repo_root: Path) -> str | None:
    """Return ``CHANGELOG.md`` as committed at ``HEAD``, or ``None`` if it is not tracked."""
    tracked = git.read_output(
        "-C", str(repo_root), "ls-tree", "--name-only", "HEAD", "--", CHANGELOG_FILE
    )
    if not tracked:
        return None
    return git.read_output("-C", str(repo_root), "show", f"HEAD:{CHANGELOG_FILE}") + "\n"


def _container_cliff(config_path: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return the command prefix and environment that run git-cliff in the dev container.

    *config_path* is the host path of a bundled config. Its directory is added
    to ``DOCKER_EXTRA_VOLUMES`` (after any volumes already set there), which
    ``vrg-container-run`` mounts, so git-cliff reads it at :data:`CONFIG_MOUNT`.
    """
    volume = f"{config_path.parent}:{CONFIG_MOUNT}:ro"
    existing = os.environ.get("DOCKER_EXTRA_VOLUMES", "")
    env = {**os.environ, "DOCKER_EXTRA_VOLUMES": f"{existing};{volume}" if existing else volume}
    command = (
        "vrg-container-run",
        "--",
        "git-cliff",
        "--config",
        f"{CONFIG_MOUNT}/{config_path.name}",
    )
    return command, env


def _run_cliff(repo_root: Path, config: str, args: tuple[str, ...]) -> None:
    """Run git-cliff with bundled *config* and *args* on *repo_root*, in the dev container.

    Path arguments in *args* are relative to *repo_root*, the container's
    working directory.
    """
    with as_file(files("vergil_tooling.configs") / config) as config_path:
        command, env = _container_cliff(config_path)
        try:
            result = subprocess.run(  # noqa: S603
                (*command, *args),
                check=True,
                capture_output=True,
                text=True,
                cwd=repo_root,
                env=env,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stdout:
                print(exc.stdout, end="")
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def generate_changelog(repo_root: Path, version: str) -> None:
    """Add release *version*'s section to the top of ``CHANGELOG.md``.

    The published entries are kept as committed at ``HEAD``: the new section is
    prepended to that content, so earlier releases' entries are never rewritten
    and a re-run replaces, rather than repeats, the new section. A first
    release, or a repo with no committed changelog, gets the file written whole.
    """
    output = repo_root / CHANGELOG_FILE
    base = release_boundary(repo_root, version)
    published = _published_changelog(repo_root)
    if base is not None and published is not None:
        output.write_text(published, encoding="utf-8")
        mode = ("--prepend", CHANGELOG_FILE)
    else:
        mode = ("-o", CHANGELOG_FILE)
    _run_cliff(repo_root, "cliff.toml", (*_tag_args(version), *mode, *_range(base)))
    _normalize_trailing_newline(output)


def generate_release_notes(repo_root: Path, version: str) -> Path:
    """Generate the per-release notes file for *version* using git-cliff."""
    releases_dir = repo_root / RELEASE_NOTES_DIR
    releases_dir.mkdir(exist_ok=True)
    output = releases_dir / f"v{version}.md"
    base = release_boundary(repo_root, version)
    args = (*_tag_args(version), "-o", f"{RELEASE_NOTES_DIR}/{output.name}", *_range(base))
    _run_cliff(repo_root, "cliff-release-notes.toml", args)
    _normalize_trailing_newline(output)
    return output


def _normalize_trailing_newline(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
