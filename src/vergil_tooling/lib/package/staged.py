"""The ``staged`` builder (spec §6.6).

The generic builder for anything that is not a Python venv product. If
``[package.staged].build-command`` is set, it runs (via ``bash -c``, from the
repo root) with ``VRG_STAGING_ROOT`` — an empty directory standing in for
``/`` — and ``VRG_TARGET_ARCH`` set, and must populate the staging root. A
non-zero exit is a hard error. The overlay's files are merged at render time;
an empty result is rejected by :func:`build.run_build`.
"""

from __future__ import annotations

import os
import subprocess

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.build import (
    BuildContext,
    BuildResult,
    contents_from_tree,
    register,
)


@register("staged")
def build_staged(ctx: BuildContext) -> BuildResult:
    """Run the build-command (if any) and package whatever it staged."""
    staged = ctx.pkg.staged
    cmd = staged.build_command if staged is not None else None
    if cmd:
        env = {
            **os.environ,
            "VRG_STAGING_ROOT": str(ctx.staging_root),
            "VRG_TARGET_ARCH": ctx.cell.arch,
        }
        proc = subprocess.run(  # noqa: S603 - the repo's own configured build command
            ["bash", "-c", cmd],  # noqa: S607
            cwd=ctx.repo_root,
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            msg = f"[package.staged].build-command failed (exit {proc.returncode}): {cmd}"
            raise PackageError(msg)
    # An empty staging root yields no contents. Every directory strictly below
    # /opt/<vendor> is owned; /opt/<vendor> itself is shared.
    return BuildResult(
        contents=contents_from_tree(ctx.staging_root, own_below=f"/opt/{ctx.pkg.vendor}")
    )
