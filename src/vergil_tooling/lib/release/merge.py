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
the shared engine. The gate lives here, not in ``pr_merge``, because it is
the release workflow's: the engine's other callers keep failing fast on a
404.

When the engine updates a BEHIND PR on GitHub, the server adds a merge commit
the local branch never sees. :func:`sync_local_branch` fast-forwards the local
branch (and the worktree it is checked out in) to the merged PR's head, so the
release worktree matches what actually merged (#3175).

The release PR's merge into ``main`` (``into_main``) uses the caller's bypass
of the "Main updates" ruleset, whose ``update`` rule lets only its bypass actor,
the human through the repository admin role, update ``main``
(vergil-project/vergil-tooling#5095). It is the only bypassed merge: the
back-merge into ``develop`` is a plain one. The engine re-checks the checks and
reviews the bypass would skip before it merges, and this wrapper refuses it for
any identity but the human's and for a PR whose base is not ``main``.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, identity_mode, pr_merge, retry
from vergil_tooling.lib.github_config import MAIN_UPDATE_RULESET_NAME
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class ReleaseConflictError(ReleaseError):
    """The PR conflicts with its base branch (the engine's ``MergeConflictError``).

    A distinct subclass so ``merge-release`` can self-heal a conflicting
    release PR by merging main into it (#3526); every other caller still
    catches it as a plain ``ReleaseError``.
    """


def known_pr_state(pr_url: str) -> str:
    """``github.pr_state`` for a PR URL GitHub just returned, retrying a brief 404."""
    return retry.retry_known_resource(lambda: github.pr_state(pr_url), resource=f"PR {pr_url}")


def _refuse_bypass(pr_url: str, *, phase: str) -> None:
    """Refuse the ruleset-bypassed merge into ``main`` unless it is the human's, into main.

    ``vrg-release`` already refuses every agent identity before any side
    effect; this is the same fail-closed test again, at the one merge that
    uses a bypass, plus the automation identity, which is not a bypass actor
    (``APP_MERGES_RELEASES`` is false). The PR's base must be ``main``, so the
    bypass is never spent on any other branch.
    """
    if (
        identity_mode.current_mode() is not identity_mode.IdentityMode.HUMAN
        or identity_mode.any_agent_signal()
    ):
        raise ReleaseError(
            phase=phase,
            command="pr_merge.wait_and_merge",
            message=(
                f"Refusing to merge {pr_url} into main with the {MAIN_UPDATE_RULESET_NAME!r} "
                "ruleset bypass: only the human maintainer merges a release PR."
            ),
        )
    base = github.base_ref(pr_url)
    if base != "main":
        raise ReleaseError(
            phase=phase,
            command="pr_merge.wait_and_merge",
            message=(
                f"Refusing the {MAIN_UPDATE_RULESET_NAME!r} ruleset bypass for {pr_url}: "
                f"its base is {base!r}, not 'main'."
            ),
        )


def wait_and_merge(
    pr_url: str,
    *,
    phase: str,
    on_branch_updated: Callable[[], None] | None = None,
    into_main: bool = False,
) -> None:
    """Wait for checks, handle behind-base, then merge with a merge commit.

    *pr_url* must have just come from GitHub (created or listed): the PR is
    first confirmed readable via :func:`known_pr_state`. *on_branch_updated*
    is forwarded to the engine, which calls it after each server-side
    update-branch.

    *into_main* marks the release PR's merge into ``main``: it is refused
    for any identity but the human's and for a PR whose base is not
    ``main``, then merged with the "Main updates" ruleset bypass, once the
    engine's own checks pass (#5095).
    """
    known_pr_state(pr_url)
    bypass: str | None = None
    if into_main:
        _refuse_bypass(pr_url, phase=phase)
        bypass = MAIN_UPDATE_RULESET_NAME
    try:
        pr_merge.wait_and_merge(
            pr_url,
            strategy="merge",
            on_branch_updated=on_branch_updated,
            bypass_ruleset=bypass,
        )
    except pr_merge.MergeConflictError as exc:
        raise ReleaseConflictError(
            phase=phase,
            command="pr_merge.wait_and_merge",
            message=str(exc),
        ) from exc
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
    local = git.branch_commit(branch)
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
