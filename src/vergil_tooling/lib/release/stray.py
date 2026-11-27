"""The stray-merge check: a PR merged into ``develop`` after the release cut.

Spec §5.4 of epic vergil-project/.github#1664. While a release holds the
lock, the only PRs that merge into ``develop`` are the holder's own: its
back-merge (``release/post-<version>``) and its dependency update
(``deps/update-<next version>``), each armed with the holder's pass. A PR
that merged otherwise (a human merge, or an arm that slipped past the
quiesce) is a *stray merge*. The release checks for one twice:

- **before ``merge-release``**, while nothing is published: a stray merge
  there abandons the release (:mod:`~vergil_tooling.lib.release.abandon`);
- **before ``unlock``**: a stray merge there is reported and recorded on the
  tracking issue, and the release goes on.

:func:`stray_merges` walks ``develop``'s first-parent history after the
cut. Each commit there is one merge into ``develop``: a squash or merge
commit, or one commit of a rebase-merged batch. The holder's PRs are found
through the trust layer (:func:`trust.prs_for_branch`: same-repo, trusted
author, merged), so a PR on a branch of the same name from a fork, or from
an outsider, is never taken for the holder's; their merge commits are left
out. Every other commit is named by the PR that merged it, or by its own
sha when GitHub knows no merged PR into ``develop`` for it. Every git or
GitHub failure raises :class:`ReleaseError`: a check that cannot be made is
never read as "no stray merge".
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import git, github, pr_path, trust, version
from vergil_tooling.lib.process_errors import command_text, error_text
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

__all__ = ["stray_merges"]

_PHASE = "stray-merge-check"
_BASE = "develop"
# The prefix of the release's own dependency-update branch
# (update_deps.preflight names it deps/update-<develop version>).
_DEPS_PREFIX = "deps/update-"

_COMMIT_PRS_QUERY = """
query($owner: String!, $name: String!, $oid: GitObjectID!) {
  repository(owner: $owner, name: $name) {
    object(oid: $oid) {
      ... on Commit {
        associatedPullRequests(first: 20) {
          nodes { url merged baseRefName }
        }
      }
    }
  }
}
"""

# Every way a GraphQL read can fail: a gh failure, a response that does not
# parse or carries errors, no App installation or token, and gh not runnable.
_GH_ERRORS = (
    subprocess.CalledProcessError,
    ValueError,
    OSError,
    RuntimeError,
    github.AppAuthError,
    github.NoInstallationError,
)


def _error(message: str, *, command: str, detail: str | None = None) -> ReleaseError:
    return ReleaseError(
        phase=_PHASE,
        command=command,
        message=f"The stray-merge check could not be made: {message}",
        detail=detail,
    )


def _cut(ctx: ReleaseContext) -> str:
    """The commit the release was cut from: this run's, or the state block's on a resume."""
    if ctx.cut:
        return ctx.cut
    if ctx.state is not None and ctx.state.cut:
        return ctx.state.cut
    raise _error(
        "the run knows no cut: it has no cut and no release state block recording one.",
        command="stray_merges",
    )


def _git(ctx: ReleaseContext, *args: str) -> str:
    where = ("-C", str(ctx.work_root))
    try:
        return git.read_output(*where, *args)
    except subprocess.CalledProcessError as exc:
        raise _error(
            "a git command failed.", command=command_text(exc.cmd), detail=error_text(exc)
        ) from exc


def _holder_branches(ctx: ReleaseContext) -> tuple[str, str]:
    """The holder's back-merge and dependency-update branches."""
    develop_version = version.next_version(ctx.version)
    return (f"{pr_path.POST_RELEASE_PREFIX}{ctx.version}", f"{_DEPS_PREFIX}{develop_version}")


def _holder_commits(ctx: ReleaseContext) -> set[str]:
    """The merge commits on ``develop`` of the holder's own merged PRs."""
    commits: set[str] = set()
    for branch in _holder_branches(ctx):
        command = f"prs_for_branch {ctx.repo} {branch}"
        try:
            prs, skipped = trust.prs_for_branch(ctx.repo, branch, state="merged")
        except subprocess.CalledProcessError as exc:
            raise _error(
                f"reading the merged PRs on '{branch}' failed.",
                command=command,
                detail=error_text(exc),
            ) from exc
        except (trust.TrustReadError, TrustedAuthorsNotConfiguredError, ValueError) as exc:
            raise _error(
                f"reading the merged PRs on '{branch}' failed: {exc}", command=command
            ) from exc
        trust.report_skipped(skipped, stream=sys.stderr)
        commits.update(pr.merge_oid for pr in prs if pr.base == _BASE and pr.merge_oid)
    return commits


def _merged_prs(ctx: ReleaseContext, sha: str) -> list[str]:
    """The URLs of the PRs into ``develop`` that GitHub says merged commit *sha*."""
    owner, _, name = ctx.repo.partition("/")
    try:
        data = github.graphql(_COMMIT_PRS_QUERY, owner=owner, name=name, oid=sha)
    except _GH_ERRORS as exc:
        raise _error(
            f"reading the PRs of commit {sha[:12]} failed: {error_text(exc)}",
            command=f"gh api graphql (associatedPullRequests of {sha[:12]})",
        ) from exc
    answer = cast("Any", data)
    try:
        nodes = answer["repository"]["object"]["associatedPullRequests"]["nodes"]
        return [
            str(node["url"])
            for node in nodes
            if node["merged"] is True and node["baseRefName"] == _BASE
        ]
    except (KeyError, TypeError) as exc:
        raise _error(
            f"GitHub's answer for the PRs of commit {sha[:12]} is not the expected shape: {data!r}",
            command=f"gh api graphql (associatedPullRequests of {sha[:12]})",
        ) from exc


def stray_merges(ctx: ReleaseContext) -> list[str]:
    """The PRs merged into ``develop`` after the cut, other than the holder's own.

    A first-parent walk of ``cut..origin/develop``, oldest first, after a
    fetch of ``develop``. Each stray PR is named once, by its URL; a commit
    no merged PR into ``develop`` accounts for is named as
    ``commit <sha12> (no merged PR into develop)``. Empty when nothing but
    the holder's back-merge and dependency-update PRs merged. Any failure
    raises :class:`ReleaseError`.
    """
    cut = _cut(ctx)
    _git(ctx, "fetch", "origin", _BASE)
    listed = _git(ctx, "rev-list", "--first-parent", "--reverse", f"{cut}..origin/{_BASE}")
    shas = [line.strip() for line in listed.splitlines() if line.strip()]
    if not shas:
        return []
    holder = _holder_commits(ctx)
    found: list[str] = []
    for sha in shas:
        if sha in holder:
            continue
        prs = _merged_prs(ctx, sha) or [f"commit {sha[:12]} (no merged PR into {_BASE})"]
        found.extend(pr for pr in prs if pr not in found)
    return found
