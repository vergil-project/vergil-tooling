"""Phase 5: Close tracking issue and run vrg-finalize-pr."""

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

    print("Running vrg-finalize-pr...")
    result = subprocess.run(  # noqa: S603
        ("vrg-finalize-pr",),  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        raise ReleaseError(
            phase="close-finalize",
            command="vrg-finalize-pr",
            message="vrg-finalize-pr failed.",
            detail=result.stderr or result.stdout,
        )
    print("Finalization complete.")


def _build_summary(ctx: ReleaseContext) -> str:
    lines = [
        f"## Release {ctx.version} — Summary",
        "",
        "### Pull Requests",
        f"- Release PR: {ctx.release_pr_url}",
        f"- Back-merge PR: {ctx.bump_pr_url}",
        "",
        "### Tags",
    ]
    if ctx.tag:
        lines.append(f"- Release tag: `{ctx.tag}`")
    if ctx.develop_tag:
        lines.append(f"- Develop boundary tag: `{ctx.develop_tag}`")
    lines.append("")
    lines.append("### Artifacts")
    if ctx.release_url:
        lines.append(f"- GitHub Release: {ctx.release_url}")
    if ctx.cd_run_url:
        lines.append(f"- CD workflow (main): {ctx.cd_run_url}")
    if ctx.develop_cd_run_url:
        lines.append(f"- Develop CD workflow: {ctx.develop_cd_run_url}")
    return "\n".join(lines)
