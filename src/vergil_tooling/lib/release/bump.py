"""Phase 3: Poll for bump PR, verify linkage, merge."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import wait_and_merge

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

_POLL_INTERVAL = 10
_POLL_TIMEOUT = 300
_LINKAGE_RE = re.compile(r"(Ref|Fixes|Closes|Resolves)\s+#\d+", re.IGNORECASE)


def merge_bump(ctx: ReleaseContext) -> None:
    """Poll for the bump PR, verify linkage, and merge it."""
    parts = ctx.version.split(".")
    next_patch = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    head = f"release/bump-version-{next_patch}"

    pr_url = _poll_for_bump_pr(ctx.repo, head)
    _verify_issue_linkage(ctx, pr_url)
    wait_and_merge(pr_url, phase="merge-bump", verbose=ctx.verbose)

    ctx.bump_pr_url = pr_url
    ctx.next_version = next_patch


def _poll_for_bump_pr(repo: str, head: str) -> str:
    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        url = github.read_output(
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            head,
            "--json",
            "url",
            "--jq",
            ".[0].url",
        )
        if url:
            print(f"Bump PR found: {url}")
            return url
        if time.monotonic() >= deadline:
            raise ReleaseError(
                phase="merge-bump",
                command=f"gh pr list --head {head}",
                message=(
                    f"Bump PR on branch '{head}' did not appear within "
                    f"{_POLL_TIMEOUT} seconds. Check the version-bump-pr action."
                ),
            )
        print(f"Waiting for bump PR on {head}...")
        time.sleep(_POLL_INTERVAL)


def _verify_issue_linkage(ctx: ReleaseContext, pr_url: str) -> None:
    body = github.read_output(
        "pr",
        "view",
        pr_url,
        "--json",
        "body",
        "--jq",
        ".body",
    )
    if not _LINKAGE_RE.search(body):
        raise ReleaseError(
            phase="merge-bump",
            command=f"gh pr view {pr_url} --json body",
            message=(
                f"Bump PR {pr_url} has no issue linkage in the body. "
                f"This is a bug in the version-bump-pr action — it should "
                f"have auto-discovered tracking issue #{ctx.issue_number}."
            ),
        )
