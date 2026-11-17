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
   ``automation/<job>/<run-id>``, whose parent is the clone's ``HEAD``, and
   pushed. ``HEAD`` is normally the ``develop`` tip; a caller may check out an
   older ``develop`` commit first (``vrg-upstream-refresh`` publishes on the
   commit its hook ran against). ``develop`` does not require a branch to be
   up to date, so the PR merges from that base once its own checks pass. Git
   records content and file mode, so an executable bit the job set survives.
4. The PR is opened against ``develop`` and GitHub auto-merge is turned on
   through the shared helper
   (:func:`vergil_tooling.lib.auto_merge.enable_auto_merge`) with the squash
   method and the pushed commit as the expected head, then read back to
   confirm it is on. When GitHub refuses because the PR can already merge, the
   helper merges it directly with the same method and head, and the result is
   ``merged`` with no wait. Any other refusal raises
   :class:`MechanizedPRError`.
5. **Wait until merged** (:func:`vergil_tooling.lib.pr_merge.wait_until_merged`).
   A merged PR is the result ``merged``. A PR whose checks are green is merged
   by the wait itself if auto-merge has not merged it first. A failed check, a
   conflict, a closed PR or the deadline stops the wait with
   :class:`PRNotMergedError`, naming the PR and the reason. The deadline is
   the caller's ``deadline_secs`` when it gives one. A job that holds an App
   token source (:mod:`vergil_tooling.lib.app_token`, registered with
   :func:`vergil_tooling.lib.github.token_source`) passes its own: every
   ``gh`` and ``git`` call then takes a current token, renewed inside the
   job, so the wait is not bound to one token's hour. Without one, the
   deadline is ``$VRG_MECHANIZED_PR_DEADLINE`` seconds, 2400 (40 minutes) by
   default, inside the one hour an ambient App installation token lasts.

Each step that changes GitHub prints one line as it happens: each earlier
run's PR it closes, the PR it opens, a direct merge, and how the wait ended,
with the time it waited. A run that waits for an hour or more can then be read
from its log alone.

Every GitHub call names the repo (``--repo``, a ``repos/<repo>/…`` endpoint or
the PR's URL), so nothing depends on the cwd. The push authenticates the way
:mod:`vergil_tooling.lib.git` does for any ``-C <checkout>`` push: the
registered token source's current token, git's credential helper, or the App
installation token of the checkout's own org. No token appears in argv or the
logs.

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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from vergil_tooling.lib import auto_merge, git, github, pr_merge
from vergil_tooling.lib.develop_clone import DEVELOP_BRANCH
from vergil_tooling.lib.process_errors import error_text

AUTOMATION_BRANCH_PREFIX = "automation/"

RUN_ID_ENV = "GITHUB_RUN_ID"

#: The wait-until-merged deadline, in seconds: ``$VRG_MECHANIZED_PR_DEADLINE``,
#: else 40 minutes, inside the one hour an App installation token lasts.
DEADLINE_ENV = "VRG_MECHANIZED_PR_DEADLINE"
DEFAULT_DEADLINE_SECS = 2400

#: Mechanized PRs are squash-merged, the method auto-merge is turned on with.
_MERGE_STRATEGY = "squash"

# A job name is one branch-path segment, so one job's prefix never contains
# another's (``automation/a/`` is not inside ``automation/a-b/``).
_JOB_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")

_CLOSE_COMMENT = "Superseded by a new run of this automation job; closing."


class MechanizedPRError(RuntimeError):
    """A step of the mechanized-PR flow failed; the message names the step and repo."""


class PRNotMergedError(MechanizedPRError):
    """The PR was opened but did not merge; *pr_url* and *reason* say which and why."""

    def __init__(self, repo: str, pr_url: str, reason: str) -> None:
        self.pr_url = pr_url
        self.reason = reason
        super().__init__(f"mechanized PR for {repo}: {pr_url} did not merge: {reason}")


@dataclass(frozen=True)
class SyncResult:
    """What :func:`open_sync_pr` did.

    *status* is ``in_sync`` (the clone had no changes, no PR opened) or
    ``merged`` (*pr_url* is the new PR, opened and merged this run). *closed*
    lists the URLs of the earlier runs' PRs that were closed.
    """

    status: Literal["in_sync", "merged"]
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


def deadline_secs() -> int:
    """The wait-until-merged deadline: ``$VRG_MECHANIZED_PR_DEADLINE`` or 2400 seconds.

    A malformed or non-positive value raises :class:`MechanizedPRError` rather
    than fall back, since the deadline is what keeps the watch inside the
    token's life.
    """
    raw = os.environ.get(DEADLINE_ENV, "").strip()
    if not raw:
        return DEFAULT_DEADLINE_SECS
    try:
        secs = int(raw)
    except ValueError:
        secs = 0
    if secs <= 0:
        msg = f"{DEADLINE_ENV} must be a positive whole number of seconds, got {raw!r}"
        raise MechanizedPRError(msg)
    return secs


# ``open_sync_pr``'s ``deadline_secs`` keyword shadows :func:`deadline_secs`
# inside it, so it reaches the function by this name.
_env_deadline_secs = deadline_secs


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
                print(f"{repo}: closed the earlier run's PR {pr.url}")
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


def _enable_auto_merge(repo: str, pr_url: str, head: str) -> bool:
    """Turn on squash auto-merge for *pr_url* at *head*; true when it merged directly."""
    try:
        result = auto_merge.enable_auto_merge(pr_url, head=head, method="SQUASH")
    except auto_merge.AutoMergeError as exc:
        raise _fail("enabling auto-merge", repo, exc) from exc
    return result.merged_now


def _wait_until_merged(repo: str, pr_url: str, deadline: int) -> None:
    """Watch *pr_url* until it merges; raise :class:`PRNotMergedError` when it does not.

    Prints how the wait ended and how long it took, merged or not.
    """
    started = time.monotonic()

    def ended(outcome: str) -> None:
        waited = int(time.monotonic() - started)
        print(f"{repo}: the wait for {pr_url} ended after {waited}s: {outcome}")

    try:
        pr_merge.wait_until_merged(pr_url, strategy=_MERGE_STRATEGY, deadline_secs=deadline)
    except pr_merge.MergeAbortError as exc:
        ended(f"not merged ({exc})")
        raise PRNotMergedError(repo, pr_url, str(exc)) from exc
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        reason = f"the wait until merged failed: {error_text(exc)}"
        ended(f"not merged ({reason})")
        raise PRNotMergedError(repo, pr_url, reason) from exc
    ended("merged")


def _check_deadline(secs: int) -> int:
    """*secs*, refused unless it is a positive whole number of seconds."""
    if isinstance(secs, bool) or not isinstance(secs, int) or secs <= 0:
        msg = (
            "the wait-until-merged deadline must be a positive whole number of seconds, "
            f"got {secs!r}"
        )
        raise MechanizedPRError(msg)
    return secs


def open_sync_pr(  # noqa: PLR0913 - the PR's parts and its deadline, all keyword-only
    repo: str,
    clone: Path,
    *,
    job: str,
    title: str,
    body: str,
    deadline_secs: int | None = None,
) -> SyncResult:
    """Propose the changes written in *clone* to *repo*'s ``develop`` as one PR.

    *clone* is a :func:`~vergil_tooling.lib.develop_clone.clone_develop`
    checkout of *repo* in which *job* has already written its changes; the
    commit's parent is the clone's ``HEAD``. *deadline_secs* is the
    wait-until-merged deadline: ``None`` keeps the environment's rule
    (:func:`deadline_secs`), and a value that is not a positive whole number
    raises before any PR is touched. See the module docstring for the steps;
    every failure raises :class:`MechanizedPRError`, and a PR that was opened
    but did not merge raises its subclass :class:`PRNotMergedError`.
    """
    deadline = _env_deadline_secs() if deadline_secs is None else _check_deadline(deadline_secs)
    branch = branch_name(job, run_id())
    closed = _clear_earlier_runs(repo, job_prefix(job))
    if not _git(repo, clone, "status", "status", "--porcelain"):
        return SyncResult(status="in_sync", pr_url=None, closed=closed)
    head = _commit_and_push(repo, clone, branch, title)
    pr_url = _open_pr(repo, branch, title, body)
    print(f"{repo}: opened {pr_url}; waiting up to {deadline}s for it to merge")
    if _enable_auto_merge(repo, pr_url, head):
        print(f"{repo}: {pr_url} was already mergeable and merged directly")
    else:
        _wait_until_merged(repo, pr_url, deadline)
    return SyncResult(status="merged", pr_url=pr_url, closed=closed)
