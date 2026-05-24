"""Rolling-tag management — force-update vX.Y to track vX.Y.Z."""

from __future__ import annotations

import re
import subprocess

_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


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

    print(f"Force-updating {rolling_tag} -> {release_tag}")
    subprocess.run(  # noqa: S603
        ["git", "tag", "-f", rolling_tag, release_tag],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )

    print(f"Pushing {rolling_tag} to origin")
    subprocess.run(  # noqa: S603
        ["git", "push", "origin", rolling_tag, "--force"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )

    print(f"Promoted: {rolling_tag} -> {release_tag}")
