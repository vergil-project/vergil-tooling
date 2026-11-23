"""The release's PR lookups by branch, through the trust layer (GHSA-r4j2-cwcp-xg56).

prepare adopts the release branch's PR and back-merge-bump the post branch's
by its branch name. Only a same-repo PR with a trusted author counts
(:func:`trust.prs_for_branch`): a fork PR on the same branch name, or a PR
whose author holds no role, never matches, and each one skipped is reported
on stderr, metadata only. A read failure, or several open PRs on one branch,
raises :class:`ReleaseError`, never "no PR" or "the first".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from vergil_tooling.lib import trust
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError


@dataclass(frozen=True)
class BranchPRs:
    """A branch's open PR, and its most recent closed or merged one."""

    open_pr: trust.PRRef | None
    latest_closed: trust.PRRef | None


def branch_prs(repo: str, branch: str, *, phase: str) -> BranchPRs:
    """The same-repo PRs with a trusted author whose head is *branch* in *repo*.

    One read of every PR on the branch. The most recent closed or merged PR
    is the highest-numbered one; its ``state`` says whether it merged.
    """
    command = f"prs_for_branch {repo} {branch}"
    try:
        prs, skipped = trust.prs_for_branch(repo, branch, state="all")
    except (trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
        raise ReleaseError(
            phase=phase,
            command=command,
            message=f"Could not read the PRs on '{branch}'; refusing to guess.",
            detail=str(exc),
        ) from exc
    trust.report_skipped(skipped, stream=sys.stderr)
    opened = [pr for pr in prs if pr.state == "OPEN"]
    if len(opened) > 1:
        numbers = ", ".join(f"#{pr.number}" for pr in opened)
        raise ReleaseError(
            phase=phase,
            command=command,
            message=(
                f"{len(opened)} open PRs on '{branch}' ({numbers}); the release cannot "
                "tell which is its own. Close the others before resuming."
            ),
        )
    closed = max((pr for pr in prs if pr.state != "OPEN"), key=lambda pr: pr.number, default=None)
    return BranchPRs(open_pr=opened[0] if opened else None, latest_closed=closed)
