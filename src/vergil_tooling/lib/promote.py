"""Rolling-tag management — force-update vX.Y to track vX.Y.Z."""

from __future__ import annotations

import re
import subprocess
import sys

_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def _git_output(*args: str) -> str:
    """Return stdout of a git command, or '' if it fails."""
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _peeled_commit(tag: str) -> str:
    """The commit origin's *tag* resolves to, or '' if the tag is absent.

    Peels an annotated tag to its commit (the ``^{}`` line from ls-remote) so
    a rolling tag and a release tag can be compared apples-to-apples.
    """
    commit = ""
    for line in _git_output("ls-remote", "origin", f"refs/tags/{tag}").splitlines():
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}"):
            return sha
        commit = sha
    return commit


def _already_promoted(rolling_tag: str, release_tag: str) -> bool:
    """True if origin's *rolling_tag* already resolves to *release_tag*'s commit."""
    release_commit = _peeled_commit(release_tag)
    return bool(release_commit) and _peeled_commit(rolling_tag) == release_commit


def promote(version: str, *, dry_run: bool = False) -> None:
    """Force-update the vX.Y rolling tag to point at vX.Y.Z."""
    m = _VERSION_RE.match(version)
    if not m:
        msg = f"'{version}' is not valid semver (expected X.Y.Z or vX.Y.Z)"
        raise ValueError(msg)

    bare = m.group(1)
    parts = bare.split(".")
    rolling_tag = f"v{parts[0]}.{parts[1]}"
    release_tag = f"v{bare}"

    if dry_run:
        print(f"Would force-update {rolling_tag} -> {release_tag}")
        print(f"Would push {rolling_tag} to origin")
        return

    if _already_promoted(rolling_tag, release_tag):
        print(f"{rolling_tag} already points at {release_tag} — already promoted.")
        return

    print(f"Force-updating {rolling_tag} -> {release_tag}")
    try:
        subprocess.run(  # noqa: S603
            ["git", "tag", "-f", rolling_tag, release_tag],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise

    print(f"Pushing {rolling_tag} to origin")
    try:
        subprocess.run(  # noqa: S603
            ["git", "push", "origin", rolling_tag, "--force"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise

    print(f"Promoted: {rolling_tag} -> {release_tag}")
