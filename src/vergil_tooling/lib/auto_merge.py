"""Turn on GitHub auto-merge for a PR, pinned to its head (epic vergil-project/.github#1218).

The auto-merge helper of the one PR path (:mod:`vergil_tooling.lib.pr_path`,
epic vergil-project/.github#1664, spec §5.4). It is private to the path:
:func:`vergil_tooling.lib.pr_path.arm` is its one caller.
:func:`_enable` turns auto-merge on through the GraphQL
``enablePullRequestAutoMerge`` mutation, with the caller's merge method and
the pushed commit as ``expectedHeadOid``, so a head pushed later is never
merged. It then reads the setting back and checks that it is on with that
method.

GitHub refuses to turn auto-merge on for a PR that can already merge: there
is nothing left to wait for. The helper then merges the PR at once, with the
same method and ``--match-head-commit`` set to the same head, and reports
``merged_now``. Any other refusal, a read-back that does not match, or a
direct merge that does not leave the PR merged raises
:class:`AutoMergeError`, naming the PR.

Both writes go through ``gh``'s transient-error retry, so an attempt that
landed with its response lost can be followed by one that fails. Before
either error is raised, the PR is read back: auto-merge on with the method
asked for, or the PR merged at the head asked for, is the write having
landed, and is reported as success (#4696).

:func:`auto_merge_enabled` reads whether auto-merge is on for a PR.

Every call takes the PR's full URL, so nothing depends on the cwd.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Literal, get_args

from vergil_tooling.lib import github
from vergil_tooling.lib.process_errors import error_text

#: The GraphQL merge methods the helper turns auto-merge on with: squash for
#: a single-issue PR, rebase for a batch PR, and a merge commit for a
#: release's back-merge and a dependency-update PR (release-lock spec §5.4).
#: :func:`vergil_tooling.lib.pr_path.merge_method` chooses among them.
MergeMethod = Literal["SQUASH", "REBASE", "MERGE"]

_METHODS: tuple[str, ...] = get_args(MergeMethod)

#: The text of GitHub's refusal to turn auto-merge on because the PR can
#: already merge. ``enablePullRequestAutoMerge`` is documented at
#: https://docs.github.com/en/graphql/reference/mutations#enablepullrequestautomerge,
#: but its error messages are not; ``gh`` shows this one as
#: ``GraphQL: Pull request Pull request is in clean status
#: (enablePullRequestAutoMerge)``, as reported at
#: https://github.com/orgs/community/discussions/53088. Only this refusal
#: leads to a direct merge; every other one is an error.
CLEAN_STATUS_REFUSAL = "Pull request is in clean status"

#: The text of GitHub's refusal of a merge, or of turning auto-merge on, because
#: the base branch moved meanwhile: ``gh`` shows it as ``GraphQL: Base branch
#: was modified. Review and try the merge again. (mergePullRequest)``. GitHub
#: gives the refusal no error code (it is a GraphQL error message only, and
#: its text is not documented), so this message is the only signal there is. It
#: is transient: the one PR path reads the PR again and tries again
#: (:class:`BaseBranchModifiedError`, vergil-project/vergil-tooling#5406).
BASE_MODIFIED_REFUSAL = "Base branch was modified"

_ENABLE_AUTO_MERGE_MUTATION = """
mutation($id: ID!, $head: GitObjectID!, $method: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $id, mergeMethod: $method, expectedHeadOid: $head}
  ) {
    pullRequest { number }
  }
}
"""


class AutoMergeError(RuntimeError):
    """Auto-merge could not be turned on, or the PR merged directly; the message names the PR."""


class BaseBranchModifiedError(AutoMergeError):
    """GitHub refused the enable or the direct merge because the base branch moved.

    The refusal is transient and landed nothing: the PR was read back and is
    still open. :func:`vergil_tooling.lib.pr_path.arm` tries again, bounded.
    """


@dataclass(frozen=True)
class AutoMergeResult:
    """What :func:`enable_auto_merge` did.

    *merged_now* is true when GitHub refused auto-merge because the PR could
    already merge, and the helper merged it directly. Otherwise auto-merge is
    on and confirmed, and GitHub merges the PR once it can.
    """

    merged_now: bool


def _pr_view(pr_url: str, fields: str, step: str) -> dict[str, object]:
    try:
        result = github.read_json("pr", "view", pr_url, "--json", fields)
    except subprocess.CalledProcessError as exc:
        msg = f"{pr_url}: {step} failed: {error_text(exc)}"
        raise AutoMergeError(msg) from exc
    if not isinstance(result, dict):
        msg = f"{pr_url}: {step}: gh pr view returned {result!r}"
        raise AutoMergeError(msg)
    return result


def _auto_merge_request(pr_url: str, step: str) -> dict[str, object] | None:
    """The PR's ``autoMergeRequest``: an object when auto-merge is on, else ``None``."""
    request = _pr_view(pr_url, "autoMergeRequest", step).get("autoMergeRequest")
    if request is not None and not isinstance(request, dict):
        msg = f"{pr_url}: {step}: autoMergeRequest is neither an object nor null: {request!r}"
        raise AutoMergeError(msg)
    return request


def auto_merge_enabled(pr_url: str) -> bool:
    """True when auto-merge is on for *pr_url* (its ``autoMergeRequest`` is set)."""
    return _auto_merge_request(pr_url, "reading auto-merge") is not None


def merged_at(pr_url: str, head: str) -> bool:
    """True when *pr_url* is merged with its head at *head*; a failed read raises.

    The one PR path reads this before it tries a refused enable again, so a PR
    another caller merged in the meantime is a success (vergil-project/vergil-tooling#5406).
    """
    pr = _pr_view(pr_url, "state,headRefOid", "reading the PR's state before a retry")
    return pr.get("state") == "MERGED" and pr.get("headRefOid") == head


def _merge_now(pr_url: str, *, head: str, method: MergeMethod) -> None:
    """Merge *pr_url* at *head* with *method*, then confirm it is merged."""
    try:
        github.merge(pr_url, strategy=method.lower(), match_head=head)
    except subprocess.CalledProcessError as exc:
        # The merge is retried on a transient error, so an attempt that landed
        # with its response lost is followed by one GitHub refuses (#4696): a
        # PR read back as merged at *head* is that case.
        msg = (
            f"{pr_url}: GitHub refused auto-merge because the PR can already merge, "
            f"and the direct merge failed: {error_text(exc)}"
        )
        moved = BASE_MODIFIED_REFUSAL in error_text(exc)
        try:
            landed = _pr_view(pr_url, "state,headRefOid", "reading the state after the error")
        except AutoMergeError as read_exc:
            raise AutoMergeError(f"{msg}; {read_exc}") from exc
        if landed.get("state") == "MERGED" and landed.get("headRefOid") == head:
            print(f"{pr_url}: the direct merge returned an error, but it merged: {error_text(exc)}")
            return
        if moved:
            raise BaseBranchModifiedError(msg) from exc
        raise AutoMergeError(msg) from exc
    state = _pr_view(pr_url, "state", "reading the state after the direct merge").get("state")
    if state != "MERGED":
        msg = f"{pr_url}: the direct merge succeeded, but the PR's state reads {state!r}"
        raise AutoMergeError(msg)


def _enable(pr_url: str, *, head: str, method: MergeMethod) -> AutoMergeResult:
    """Turn on auto-merge for *pr_url* with *method*, pinned to *head*, and confirm it.

    When GitHub refuses because the PR can already merge
    (:data:`CLEAN_STATUS_REFUSAL`), merge it directly with *method* and *head*
    and return ``merged_now=True``. Every other failure raises
    :class:`AutoMergeError` naming the PR.
    """
    if method not in _METHODS:
        msg = f"{pr_url}: merge method must be one of {', '.join(_METHODS)}, got {method!r}"
        raise AutoMergeError(msg)
    node_id = _pr_view(pr_url, "id", "reading the PR's node ID").get("id")
    if not isinstance(node_id, str) or not node_id:
        msg = f"{pr_url} has no node ID"
        raise AutoMergeError(msg)
    try:
        github.graphql(_ENABLE_AUTO_MERGE_MUTATION, id=node_id, head=head, method=method)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        detail = error_text(exc)
        if CLEAN_STATUS_REFUSAL in detail:
            print(f"{pr_url}: already mergeable; merging it directly ({method.lower()})")
            _merge_now(pr_url, head=head, method=method)
            return AutoMergeResult(merged_now=True)
        # The mutation is retried on a transient error, so an attempt that
        # landed with its response lost is followed by one that can fail
        # (#4696). Auto-merge read back as on with *method* is that case.
        msg = f"{pr_url}: enabling auto-merge failed: {detail}"
        try:
            landed = _auto_merge_request(pr_url, "reading back auto-merge after the error")
        except AutoMergeError as read_exc:
            raise AutoMergeError(f"{msg}; {read_exc}") from exc
        if landed is not None and landed.get("mergeMethod") == method:
            print(f"{pr_url}: enabling auto-merge returned an error, but it is on: {detail}")
            return AutoMergeResult(merged_now=False)
        if BASE_MODIFIED_REFUSAL in detail:
            raise BaseBranchModifiedError(msg) from exc
        raise AutoMergeError(msg) from exc
    request = _auto_merge_request(pr_url, "reading back auto-merge")
    read_method = request.get("mergeMethod") if request is not None else None
    if read_method != method:
        msg = (
            f"{pr_url}: auto-merge is not confirmed with {method} "
            f"(autoMergeRequest read back as {request!r})"
        )
        raise AutoMergeError(msg)
    return AutoMergeResult(merged_now=False)
