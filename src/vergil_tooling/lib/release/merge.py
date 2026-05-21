"""Wait-poll-merge logic shared by Phases 2 and 3."""

from __future__ import annotations

from vergil_tooling.lib import github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import wait_for_checks

_MAX_BRANCH_UPDATES = 5


def wait_and_merge(pr_url: str, *, phase: str, verbose: bool = False) -> None:
    """Wait for checks, handle behind-base, then merge."""
    updates = 0
    while True:
        if github.mergeable(pr_url) == "CONFLICTING":
            raise ReleaseError(
                phase=phase,
                command=f"gh pr view {pr_url} --json mergeable",
                message="PR has merge conflicts.",
            )

        print(f"Waiting for checks on {pr_url}...")
        wait_for_checks(pr_url, verbose=verbose)

        if github.merge_state_status(pr_url) != "BEHIND":
            break

        updates += 1
        if updates > _MAX_BRANCH_UPDATES:
            raise ReleaseError(
                phase=phase,
                command=f"update branch ({updates} attempts)",
                message="Branch still behind after multiple updates.",
            )
        print("Branch is behind base — updating and re-checking...")
        github.update_branch(pr_url)

    print(f"Checks passed. Merging {pr_url}...")
    github.merge(pr_url, strategy="merge")
    print("Merged.")
