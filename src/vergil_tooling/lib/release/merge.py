"""Wait-poll-merge logic shared by Phases 2 and 3.

Thin wrapper over the shared engine in ``vergil_tooling.lib.pr_merge``
— release keeps its public interface (``ReleaseError`` on failure,
merge-commit strategy) while the loop logic lives in one place. Check
waiting is the engine's default bounded poller (``github.wait_for_checks``):
the old streamed ``gh pr checks --watch`` had no deadline and could not see
past a stale check-run, so it hung a release for 12+ hours (#3170).

Every PR URL the release workflow handles was just returned by GitHub
itself (``gh pr create`` or ``gh pr list``), so a 404 on reading it means
"not readable yet", not "missing" (#3137). :func:`known_pr_state` reads such
a PR's state under :func:`retry.retry_known_resource`, and
:func:`wait_and_merge` uses it as a readability gate before handing the PR to
the shared engine. The gate lives here, not in ``pr_merge``, because that
engine also serves ``vrg-finalize-pr``, whose PR comes from user input and
must keep failing fast on a 404.

When the engine updates a BEHIND PR on GitHub, the server adds a merge commit
the local branch never sees. :func:`sync_local_branch` fast-forwards the local
branch (and the worktree it is checked out in) to the merged PR's head, so the
release worktree matches what actually merged (#3175).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, pr_merge, retry
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def known_pr_state(pr_url: str) -> str:
    """``github.pr_state`` for a PR URL GitHub just returned, retrying a brief 404."""
    return retry.retry_known_resource(lambda: github.pr_state(pr_url), resource=f"PR {pr_url}")


def wait_and_merge(
    pr_url: str,
    *,
    phase: str,
    on_branch_updated: Callable[[], None] | None = None,
) -> None:
    """Wait for checks, handle behind-base, then merge with a merge commit.

    *pr_url* must have just come from GitHub (created or listed): the PR is
    first confirmed readable via :func:`known_pr_state`. *on_branch_updated*
    is forwarded to the engine, which calls it after each server-side
    update-branch.
    """
    known_pr_state(pr_url)
    try:
        pr_merge.wait_and_merge(pr_url, strategy="merge", on_branch_updated=on_branch_updated)
    except pr_merge.MergeAbortError as exc:
        raise ReleaseError(
            phase=phase,
            command="pr_merge.wait_and_merge",
            message=str(exc),
        ) from exc


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    """True when *ancestor* is reachable from *descendant*.

    Exit 0 means yes and 1 means no; any other exit (a bad revision, a broken
    repository) raises rather than being read as "not an ancestor".
    """
    args = ("git", "merge-base", "--is-ancestor", ancestor, descendant)
    result = subprocess.run(args, check=False, capture_output=True, text=True)  # noqa: S603
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise subprocess.CalledProcessError(
        result.returncode, args, output=result.stdout, stderr=result.stderr
    )


def sync_local_branch(pr_url: str, *, branch: str, worktree: Path, phase: str) -> None:
    """Fast-forward local *branch*, checked out in *worktree*, to *pr_url*'s head.

    Call it after the PR merged following a server-side update-branch: the
    merged head is then final and descends from the local tip. Raises
    ``ReleaseError`` when the worktree is not on *branch* or when the head does
    not descend from the local tip — it never resets over local commits.
    """
    head = github.head_sha(pr_url)
    local = git.read_output("rev-parse", branch)
    if local == head:
        return
    checked_out = git.read_output("-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD")
    if checked_out != branch:
        raise ReleaseError(
            phase=phase,
            command="sync_local_branch",
            message=(
                f"Cannot sync {branch} to the merged PR head {head}: worktree "
                f"{worktree} has {checked_out} checked out, not {branch}."
            ),
        )
    git.run("fetch", "origin", head)
    if not _is_ancestor(local, head):
        raise ReleaseError(
            phase=phase,
            command="sync_local_branch",
            message=(
                f"Local {branch} ({local}) is not an ancestor of the merged PR head "
                f"{head}; refusing to move it over local commits. Inspect worktree "
                f"{worktree} and reconcile {branch} with origin by hand."
            ),
        )
    git.run("-C", str(worktree), "merge", "--ff-only", head)
    print(f"Fast-forwarded local {branch} to the merged PR head {head}.")
