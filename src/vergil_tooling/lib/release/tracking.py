"""GitHub tracking issue management for release operations."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import github

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError


def find_existing_tracking_issue(repo: str, version: str) -> str | None:
    """Return the URL of an open 'release: <version>' issue, or None."""
    result = github.read_output(
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        f"release: {version} in:title",
        "--state",
        "open",
        "--json",
        "url",
        "--jq",
        ".[0].url",
    )
    return result if result else None


def create_tracking_issue(ctx: ReleaseContext) -> None:
    """Create a release tracking issue and populate ctx."""
    body = f"## Release {ctx.version}\n\nRepo: {ctx.repo}\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        url = github.read_output(
            "issue",
            "create",
            "--repo",
            ctx.repo,
            "--title",
            f"release: {ctx.version}",
            "--body-file",
            tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        msg = f"Could not extract issue number from URL: {url}"
        raise ValueError(msg)
    ctx.issue_number = int(match.group(1))
    ctx.issue_url = url


def comment_phase_complete(ctx: ReleaseContext, phase: str, details: str) -> None:
    """Post a phase-completion comment on the tracking issue."""
    body = f"<!-- vrg-release:{phase}:complete -->\n\n**{phase}** complete.\n\n{details}"
    _comment(ctx, body)


def comment_phase_failed(ctx: ReleaseContext, phase: str, exc: ReleaseError) -> None:
    """Post a phase-failure comment on the tracking issue."""
    lines = [
        f"<!-- vrg-release:{phase}:failed -->",
        "",
        f"**{phase}** failed.",
        "",
        f"**Command:** `{exc.command}`",
        f"**Error:** {exc}",
    ]
    if exc.detail:
        lines.append(f"**Detail:** {exc.detail}")
    _comment(ctx, "\n".join(lines))


def close_tracking_issue(ctx: ReleaseContext, summary: str) -> None:
    """Post a summary comment and close the tracking issue."""
    _comment(ctx, summary)
    github.run(
        "issue",
        "close",
        str(ctx.issue_number),
        "--repo",
        ctx.repo,
    )


def _comment(ctx: ReleaseContext, body: str) -> None:
    """Post a comment on the tracking issue."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        github.run(
            "issue",
            "comment",
            str(ctx.issue_number),
            "--repo",
            ctx.repo,
            "--body-file",
            tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
