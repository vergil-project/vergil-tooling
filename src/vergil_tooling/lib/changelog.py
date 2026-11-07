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
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib.resources import files
from typing import TYPE_CHECKING

from vergil_tooling.lib import git

if TYPE_CHECKING:
    from pathlib import Path

RELEASE_NOTES_DIR = "releases"
CHANGELOG_FILE = "CHANGELOG.md"

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


def _run_cliff(repo_root: Path, config: str, args: tuple[str, ...]) -> None:
    config_path = files("vergil_tooling.configs") / config
    try:
        result = subprocess.run(  # noqa: S603
            ("git-cliff", "--config", str(config_path), *args),  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
    except subprocess.CalledProcessError as exc:
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
        mode = ("--prepend", str(output))
    else:
        mode = ("-o", str(output))
    _run_cliff(repo_root, "cliff.toml", (*_tag_args(version), *mode, *_range(base)))
    _normalize_trailing_newline(output)


def generate_release_notes(repo_root: Path, version: str) -> Path:
    """Generate the per-release notes file for *version* using git-cliff."""
    releases_dir = repo_root / RELEASE_NOTES_DIR
    releases_dir.mkdir(exist_ok=True)
    output = releases_dir / f"v{version}.md"
    base = release_boundary(repo_root, version)
    args = (*_tag_args(version), "-o", str(output), *_range(base))
    _run_cliff(repo_root, "cliff-release-notes.toml", args)
    _normalize_trailing_newline(output)
    return output


def _normalize_trailing_newline(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
