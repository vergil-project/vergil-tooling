"""Wait-poll-merge logic shared by Phases 2 and 3.

Thin wrapper over the shared engine in ``vergil_tooling.lib.pr_merge``
— release keeps its public interface (``ReleaseError`` on failure,
verbose-aware check waiting, merge-commit strategy) while the loop
logic lives in one place.
"""

from __future__ import annotations

from vergil_tooling.lib import pr_merge
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import wait_for_checks


def wait_and_merge(pr_url: str, *, phase: str, verbose: bool = False) -> None:
    """Wait for checks, handle behind-base, then merge with a merge commit."""
    try:
        pr_merge.wait_and_merge(
            pr_url,
            strategy="merge",
            wait_checks=lambda pr: wait_for_checks(pr, verbose=verbose),
        )
    except pr_merge.MergeAbortError as exc:
        raise ReleaseError(
            phase=phase,
            command="pr_merge.wait_and_merge",
            message=str(exc),
        ) from exc
