"""Phase 5: Close tracking issue and run vrg-finalize-repo."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import close_tracking_issue

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def close_and_finalize(ctx: ReleaseContext) -> None:
    """Close the tracking issue with a summary, then finalize the repo."""
    summary = _build_summary(ctx)
    close_tracking_issue(ctx, summary)
    print("Tracking issue closed.")

    print("Running vrg-finalize-repo...")
    result = subprocess.run(  # noqa: S603
        ("vrg-finalize-repo",),  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        raise ReleaseError(
            phase="close-finalize",
            command="vrg-finalize-repo",
            message="vrg-finalize-repo failed.",
            detail=result.stderr or result.stdout,
        )
    print("Finalization complete.")


def _build_summary(ctx: ReleaseContext) -> str:
    lines = [
        f"## Release {ctx.version} — Summary",
        "",
        "### Pull Requests",
        f"- Release PR: {ctx.release_pr_url}",
        f"- Bump PR: {ctx.bump_pr_url}",
        "",
        "### Tags",
        f"- Release tag: `{ctx.tag}`",
        f"- Develop boundary tag: `{ctx.develop_tag}`",
        "",
        "### Artifacts",
        f"- GitHub Release: {ctx.release_url}",
        f"- publish.yml: {ctx.publish_run_url}",
        f"- docs workflow: {ctx.docs_run_url}",
    ]
    return "\n".join(lines)
