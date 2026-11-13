"""The mechanized-PR building block (epic vergil-project/.github#1064, spec §6.2).

Every automated job that opens a PR does it through :func:`open_sync_pr`. The
job writes its changes into a fresh clone of ``develop``
(:func:`vergil_tooling.lib.develop_clone.clone_develop`), then hands the clone
here with its name and the PR's title and body. This module does not use the
agent PR workflow: no issue, no ``vrg-pr-workflow`` state, no relay ref. It works
the way a person would, with plain git in that checkout:

1. **Replace, never build on, an earlier run.** Every branch in the repo under
   the job's own prefix, ``automation/<job>/``, is deleted, its open PR closed
   first. Only a PR whose head is in this repo counts: a PR from a fork whose
   branch happens to carry the same name is never closed or touched.
2. **In sync.** A clean ``git status`` in the clone means nothing to propose:
   the result is ``in_sync``.
3. **Otherwise** the changes are committed on a new branch for this run,
   ``automation/<job>/<run-id>``, whose parent is the clone's ``develop``
   commit, and pushed. Git records content and file mode, so an executable bit
   the job set survives.
4. The PR is opened against ``develop`` and GitHub auto-merge is turned on with
   the squash method and the pushed commit as the expected head, then read back
   to confirm it is on. The module returns without waiting for CI or the merge.

Every GitHub call names the repo (``--repo`` or a ``repos/<repo>/…`` endpoint),
so nothing depends on the cwd. The push authenticates the way
:mod:`vergil_tooling.lib.git` does for any ``-C <checkout>`` push: git's
credential helper, or the App installation token of the checkout's own org. No
token appears in argv or the logs.

Every failure raises :class:`MechanizedPRError`, naming the step that failed and
the repo. A failure after the push leaves the branch behind; the next run's
step 1 deletes it.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from vergil_tooling.lib import git, github
from vergil_tooling.lib.develop_clone import DEVELOP_BRANCH
from vergil_tooling.lib.process_errors import error_text

AUTOMATION_BRANCH_PREFIX = "automation/"

RUN_ID_ENV = "GITHUB_RUN_ID"

# A job name is one branch-path segment, so one job's prefix never contains
# another's (``automation/a/`` is not inside ``automation/a-b/``).
_JOB_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")

_CLOSE_COMMENT = "Superseded by a new run of this automation job; closing."

_ENABLE_AUTO_MERGE_MUTATION = """
mutation($id: ID!, $head: GitObjectID!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $id, mergeMethod: SQUASH, expectedHeadOid: $head}
  ) {
    pullRequest { number }
  }
}
"""


class MechanizedPRError(RuntimeError):
    """A step of the mechanized-PR flow failed; the message names the step and repo."""


@dataclass(frozen=True)
class SyncResult:
    """What :func:`open_sync_pr` did.

    *status* is ``in_sync`` (the clone had no changes, no PR opened) or
    ``opened`` (*pr_url* is the new PR, with auto-merge confirmed on). *closed*
    lists the URLs of the earlier runs' PRs that were closed.
    """

    status: Literal["in_sync", "opened"]
    pr_url: str | None
    closed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _OpenPR:
    number: int
    url: str


def _fail(step: str, repo: str, exc: BaseException) -> MechanizedPRError:
    return MechanizedPRError(f"mechanized PR for {repo}: {step} failed: {error_text(exc)}")


def _check_job(job: str) -> None:
    if not _JOB_RE.match(job):
        msg = f"invalid automation job name {job!r}: use lowercase letters, digits and '-'"
        raise MechanizedPRError(msg)


def run_id() -> str:
    """The run identifier for this run's branch.

    ``$GITHUB_RUN_ID`` when it is set (an Actions run), otherwise a UTC
    ``%Y%m%dT%H%M%SZ`` stamp plus 6 hex characters, so two local runs in the
    same second still differ.
    """
    from_env = os.environ.get(RUN_ID_ENV, "")
    if from_env:
        return from_env
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def job_prefix(job: str) -> str:
    """The branch prefix every run of *job* uses: ``automation/<job>/``."""
    _check_job(job)
    return f"{AUTOMATION_BRANCH_PREFIX}{job}/"


def branch_name(job: str, run_id: str) -> str:
    """The branch for run *run_id* of *job*: ``automation/<job>/<run-id>``."""
    if not _RUN_ID_RE.match(run_id):
        msg = f"invalid automation run ID {run_id!r}: use letters, digits and '-'"
        raise MechanizedPRError(msg)
    return f"{job_prefix(job)}{run_id}"


def _job_branches(repo: str, prefix: str) -> list[str]:
    """Every branch in *repo* whose name starts with *prefix*."""
    ref_prefix = f"refs/heads/{prefix}"
    endpoint = f"repos/{repo}/git/matching-refs/heads/{quote(prefix, safe='/')}"
    branches: list[str] = []
    for item in github.read_json_paginated(endpoint):
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str) or not ref.startswith(ref_prefix):
            msg = f"{endpoint} returned a ref outside {ref_prefix!r}: {item!r}"
            raise MechanizedPRError(msg)
        branches.append(ref.removeprefix("refs/heads/"))
    return branches


def _same_repo(repo: str, item: dict[str, object]) -> bool:
    """True when the PR *item*'s head is in *repo* itself, not a fork.

    ``isCrossRepository`` decides it; a same-repo answer is cross-checked
    against the head repository's full name, and a disagreement raises rather
    than guess.
    """
    cross = item.get("isCrossRepository")
    if not isinstance(cross, bool):
        msg = f"PR list entry has no isCrossRepository flag: {item!r}"
        raise MechanizedPRError(msg)
    if cross:
        return False
    owner = item.get("headRepositoryOwner")
    head_repo = item.get("headRepository")
    login = owner.get("login") if isinstance(owner, dict) else None
    name = head_repo.get("name") if isinstance(head_repo, dict) else None
    if not isinstance(login, str) or not isinstance(name, str):
        msg = f"PR list entry has no head repository: {item!r}"
        raise MechanizedPRError(msg)
    if f"{login}/{name}".lower() != repo.lower():
        msg = f"PR {item.get('url')!r} is not cross-repository, but its head is {login}/{name}"
        raise MechanizedPRError(msg)
    return True


def _open_prs_for(repo: str, branch: str) -> list[_OpenPR]:
    """The open PRs in *repo* whose head is *branch* in *repo* itself."""
    fields = "number,url,isCrossRepository,headRepository,headRepositoryOwner"
    result = github.read_json(
        "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--json", fields
    )
    if not isinstance(result, list):
        msg = f"gh pr list for {branch} returned {type(result).__name__}, not a list"
        raise MechanizedPRError(msg)
    prs: list[_OpenPR] = []
    for item in result:
        if not isinstance(item, dict):
            msg = f"gh pr list for {branch} returned an entry that is not an object: {item!r}"
            raise MechanizedPRError(msg)
        if not _same_repo(repo, item):
            continue
        number = item.get("number")
        url = item.get("url")
        if not isinstance(number, int) or isinstance(number, bool) or not isinstance(url, str):
            msg = f"gh pr list for {branch} returned an entry with no number or URL: {item!r}"
            raise MechanizedPRError(msg)
        prs.append(_OpenPR(number, url))
    return prs


def _clear_earlier_runs(repo: str, prefix: str) -> list[str]:
    """Close the open PR of, and then delete, every branch under *prefix*."""
    closed: list[str] = []
    try:
        for branch in _job_branches(repo, prefix):
            for pr in _open_prs_for(repo, branch):
                # Not `--delete-branch`: gh would also touch the local branch,
                # and the remote branch is deleted below in every case, PR or not.
                github.run(
                    "pr", "close", str(pr.number), "--repo", repo, "--comment", _CLOSE_COMMENT
                )
                closed.append(pr.url)
            github.delete_if_exists(f"repos/{repo}/git/refs/heads/{quote(branch, safe='/')}")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise _fail("clearing earlier runs", repo, exc) from exc
    return closed


def _git(repo: str, clone: Path, step: str, *args: str) -> str:
    try:
        return git.read_output("-C", str(clone), *args)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise _fail(f"git {step}", repo, exc) from exc


def _commit_and_push(repo: str, clone: Path, branch: str, title: str) -> str:
    """Commit the clone's changes on *branch*, push it, and return the commit."""
    _git(repo, clone, "checkout", "checkout", "-q", "-b", branch)
    _git(repo, clone, "add", "add", "--all")
    _git(repo, clone, "commit", "commit", "-q", "-m", title)
    sha = _git(repo, clone, "rev-parse", "rev-parse", "HEAD")
    try:
        git.run("-C", str(clone), "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
    except (subprocess.CalledProcessError, OSError) as exc:
        raise _fail("git push", repo, exc) from exc
    return sha


def _open_pr(repo: str, branch: str, title: str, body: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        url = github.create_pr(
            repo=repo, base=DEVELOP_BRANCH, head=branch, title=title, body_file=str(body_path)
        )
    except subprocess.CalledProcessError as exc:
        raise _fail("opening the PR", repo, exc) from exc
    finally:
        body_path.unlink(missing_ok=True)
    if not url:
        msg = f"mechanized PR for {repo}: opening the PR printed no URL"
        raise MechanizedPRError(msg)
    return url


def _pr_view(repo: str, pr_url: str, fields: str, step: str) -> dict[str, object]:
    try:
        result = github.read_json("pr", "view", pr_url, "--repo", repo, "--json", fields)
    except subprocess.CalledProcessError as exc:
        raise _fail(step, repo, exc) from exc
    if not isinstance(result, dict):
        msg = f"mechanized PR for {repo}: {step}: gh pr view returned {result!r}"
        raise MechanizedPRError(msg)
    return result


def _enable_auto_merge(repo: str, pr_url: str, head: str) -> None:
    """Turn on squash auto-merge for *pr_url* at *head*, then confirm it is on."""
    node_id = _pr_view(repo, pr_url, "id", "reading the PR's node ID").get("id")
    if not isinstance(node_id, str) or not node_id:
        msg = f"mechanized PR for {repo}: {pr_url} has no node ID"
        raise MechanizedPRError(msg)
    try:
        github.graphql(_ENABLE_AUTO_MERGE_MUTATION, id=node_id, head=head)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        raise _fail(f"enabling auto-merge on {pr_url}", repo, exc) from exc
    request = _pr_view(repo, pr_url, "autoMergeRequest", "reading back auto-merge").get(
        "autoMergeRequest"
    )
    method = request.get("mergeMethod") if isinstance(request, dict) else None
    if method != "SQUASH":
        msg = (
            f"mechanized PR for {repo}: auto-merge is not confirmed on {pr_url} "
            f"(autoMergeRequest read back as {request!r})"
        )
        raise MechanizedPRError(msg)


def open_sync_pr(repo: str, clone: Path, *, job: str, title: str, body: str) -> SyncResult:
    """Propose the changes written in *clone* to *repo*'s ``develop`` as one PR.

    *clone* is a :func:`~vergil_tooling.lib.develop_clone.clone_develop`
    checkout of *repo* in which *job* has already written its changes. See the
    module docstring for the steps; every failure raises
    :class:`MechanizedPRError`.
    """
    branch = branch_name(job, run_id())
    closed = _clear_earlier_runs(repo, job_prefix(job))
    if not _git(repo, clone, "status", "status", "--porcelain"):
        return SyncResult(status="in_sync", pr_url=None, closed=closed)
    head = _commit_and_push(repo, clone, branch, title)
    pr_url = _open_pr(repo, branch, title, body)
    _enable_auto_merge(repo, pr_url, head)
    return SyncResult(status="opened", pr_url=pr_url, closed=closed)
