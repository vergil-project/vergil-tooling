"""Open a PR with ``gh pr create``, adopting the one a failed attempt opened.

``gh pr create`` is not idempotent. GitHub can carry out a create and still
answer with an error (a gateway timeout on the response), and a blind retry is
then refused with "a pull request for branch … already exists", leaving an
open PR the caller never recorded (#4696). So :func:`create_or_adopt` runs the
create once per attempt (``github.create_pr(..., retry=False)``) and, after
every failed attempt, looks for the open PR from the head branch into the
base. It adopts that PR only when it can show it is the one the caller asked
for: a same-repo head on that branch, that base, that title, and the head
commit the caller pushed. Any other open PR fails loudly, naming it. With none
open, a transient error (:func:`retry.is_retryable`) is retried with the
shared backoff and any other error is raised as it came.

Shared by ``vrg-submit-pr`` and the mechanized, release and dependency-update
PR creators (#4699).
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import github, retry
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Callable


class PRCreateError(Exception):
    """A failed create left a PR open that could not be adopted, or its lookup failed."""


@dataclass(frozen=True)
class PRSpec:
    """The PR a create asks for, which an open PR must match to be adopted.

    *head_oid* is the head commit the caller pushed. *repo* (``OWNER/REPO``)
    names the repo to look in; ``None`` reads the cwd's, as ``gh`` does.
    """

    head: str
    base: str
    title: str
    head_oid: str
    repo: str | None = None


@dataclass(frozen=True)
class CreatedPR:
    """The PR :func:`create_or_adopt` returns: opened by it, or *adopted*."""

    url: str
    adopted: bool


def adoptable(
    spec: PRSpec, error: subprocess.CalledProcessError, *, opener: str, rerun: str
) -> str | None:
    """The URL of the open PR a failed create of *spec* left behind, or ``None`` when none is open.

    An open PR that does not match *spec* raises :class:`PRCreateError` naming
    it, with *error*; so do several open PRs and a failed lookup. *opener*
    names who opened the PR in that message ("this submit") and *rerun* says
    how to try again once the PR is closed ("submit again").
    """
    try:
        if spec.repo is None:
            prs = github.open_prs_for_head(spec.head, base=spec.base)
        else:
            prs = github.open_prs_for_head(spec.head, base=spec.base, repo=spec.repo)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"creating the PR failed ({error_text(error)}), and looking for an open PR "
            f"from {spec.head} into {spec.base} failed too: {error_text(exc)}"
        )
        raise PRCreateError(msg) from exc
    if not prs:
        return None
    if len(prs) > 1:
        urls = ", ".join(pr.url for pr in prs)
        msg = (
            f"creating the PR failed ({error_text(error)}), and several PRs are open from "
            f"{spec.head} into {spec.base} ({urls}); none was adopted. Inspect them, then "
            f"close the ones that should not be open and {rerun}."
        )
        raise PRCreateError(msg)
    pr = prs[0]
    mismatches = [
        f"its {what} is {got!r}, not {want!r}"
        for what, got, want in (
            ("head branch", pr.head_ref, spec.head),
            ("base", pr.base_ref, spec.base),
            ("title", pr.title, spec.title),
            ("head commit", pr.head_oid, spec.head_oid),
        )
        if got != want
    ]
    if pr.cross_repo:
        mismatches.insert(0, "its head is in another repository")
    if not mismatches:
        return pr.url
    msg = (
        f"creating the PR failed ({error_text(error)}), and {pr.url} is already open from "
        f"{spec.head} into {spec.base}, but it is not the PR {opener} opened "
        f"({'; '.join(mismatches)}); it was not adopted. Inspect it, then close it and "
        f"{rerun}."
    )
    raise PRCreateError(msg)


def create_or_adopt(
    create: Callable[[], str], spec: PRSpec, *, opener: str, rerun: str
) -> CreatedPR:
    """Open *spec*'s PR with *create* and return it, adopting the one a failed attempt opened.

    *create* runs ``gh pr create`` exactly once and returns the new PR's URL
    (``github.create_pr(..., retry=False)``); this function does the retrying.
    After each failed attempt :func:`adoptable` looks for the open PR. A match
    is adopted; any other open PR raises :class:`PRCreateError`. With none
    open, a transient error is retried up to :data:`retry.MAX_RETRIES` times
    and any other error is raised.
    """
    for attempt in range(retry.MAX_RETRIES + 1):
        try:
            return CreatedPR(url=create(), adopted=False)
        except subprocess.CalledProcessError as exc:
            adopted = adoptable(spec, exc, opener=opener, rerun=rerun)
            if adopted is not None:
                print(
                    f"Creating the PR returned an error ({error_text(exc)}), but GitHub had "
                    f"opened it: adopting {adopted}."
                )
                return CreatedPR(url=adopted, adopted=True)
            if attempt == retry.MAX_RETRIES or not retry.is_retryable(exc):
                raise
            delay = retry.compute_delay(attempt)
            print(
                f"GitHub API error creating the PR (attempt {attempt + 1}/"
                f"{retry.MAX_RETRIES + 1}), no PR open; retrying in {delay:.1f}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
