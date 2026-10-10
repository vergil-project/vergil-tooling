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
"""

from __future__ import annotations

from vergil_tooling.lib import github, pr_merge, retry
from vergil_tooling.lib.release.context import ReleaseError


def known_pr_state(pr_url: str) -> str:
    """``github.pr_state`` for a PR URL GitHub just returned, retrying a brief 404."""
    return retry.retry_known_resource(lambda: github.pr_state(pr_url), resource=f"PR {pr_url}")


def wait_and_merge(pr_url: str, *, phase: str) -> None:
    """Wait for checks, handle behind-base, then merge with a merge commit.

    *pr_url* must have just come from GitHub (created or listed): the PR is
    first confirmed readable via :func:`known_pr_state`.
    """
    known_pr_state(pr_url)
    try:
        pr_merge.wait_and_merge(pr_url, strategy="merge")
    except pr_merge.MergeAbortError as exc:
        raise ReleaseError(
            phase=phase,
            command="pr_merge.wait_and_merge",
            message=str(exc),
        ) from exc
