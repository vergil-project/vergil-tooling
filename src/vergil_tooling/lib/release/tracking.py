"""GitHub tracking issue management for release operations."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import github

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

# GitHub's addComment API rejects any comment body longer than this many
# characters. Posting a larger body fails with
# "Body is too long (maximum is 65536 characters) (addComment)".
_MAX_COMMENT_CHARS = 65_536
# Stay comfortably under the hard limit so the truncation marker and any
# character-vs-byte counting differences cannot push us back over.
_COMMENT_BUDGET = 65_000


def _truncate_for_comment(body: str, *, budget: int = _COMMENT_BUDGET) -> str:
    """Shrink *body* to fit GitHub's comment-size limit.

    Bodies within *budget* are returned unchanged. Larger bodies keep the
    head (the marker comment and the structured phase/command/error preamble)
    and the tail (where failure logs put the actual error), dropping the
    middle and replacing it with a marker noting how many characters were
    removed.
    """
    if len(body) <= budget:
        return body
    marker_template = "\n\n[... {dropped} characters truncated ...]\n\n"
    # The marker's length depends on the dropped count, which we don't know
    # until we know how much we keep. Reserve an upper bound: the dropped
    # count can never have more digits than the original length.
    marker_reserve = len(marker_template.format(dropped=len(body)))
    available = budget - marker_reserve
    head_chars = available // 2
    tail_chars = available - head_chars
    dropped = len(body) - head_chars - tail_chars
    marker = marker_template.format(dropped=dropped)
    return body[:head_chars] + marker + body[-tail_chars:]


def find_existing_tracking_issue(repo: str, version: str) -> str | None:
    """Return the URL of an open 'release: <version>' issue, or None.

    GitHub search tokenizes on punctuation, so the query is a broad net;
    we filter client-side for an exact title match.
    """
    results = github.read_json(
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        f"release: {version} in:title",
        "--state",
        "open",
        "--json",
        "url,title",
    )
    expected_title = f"release: {version}"
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            issue = cast("dict[str, object]", item)
            if str(issue.get("title", "")) == expected_title:
                return str(issue["url"])
    return None


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
    """Post a comment on the tracking issue.

    Bodies are truncated to GitHub's comment-size limit first, so an
    oversized payload (e.g. a multi-minute CD watch log captured into a
    failure detail) reports the failure instead of crashing the release.
    """
    body = _truncate_for_comment(body)
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
