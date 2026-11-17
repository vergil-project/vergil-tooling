"""Find, watch and classify CD workflow runs.

Shared by ``vrg-finalize-pr`` (its post-merge CD check) and ``vrg-release``
(its develop-CD gate and its confirm stages), so every command judges a CD
run the same way (epic vergil-project/.github#1218, spec §6.5 and §6.6).

A run is found by the commit it ran on (``headSha``), never as "the latest run
on the branch", which can be a stale or unrelated one (issue #2753). A run
cancelled by a concurrency group making way for a higher-priority run is
*superseded*, not failed (issue #3834). Of the jobs that failed, a docs job's
failure is *deferred*, as ``confirm-develop`` defers a develop docs publish;
any other failure is *blocking*.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Literal

from vergil_tooling.lib import github

RunState = Literal[
    "queued", "in_progress", "success", "failed", "timed_out", "superseded", "cancelled", "none"
]

_CD_WORKFLOW_NAME = "CD"

# CD is dispatched asynchronously by the push that a merge produces, so the
# run may not be registered the instant anything looks. Poll up to
# _CD_DISPATCH_TIMEOUT_SECS for the run whose headSha matches the commit; if
# none has by then, the commit triggered no CD run (path filters, a skip).
# _CD_POLL_INTERVAL_SECS paces both that wait and the watch of a found run.
_CD_DISPATCH_TIMEOUT_SECS = 120
# Default CD watch ceiling: 90 minutes. The longest known CD is vergil-containers'
# (rust image jobs alone take ~40 min); run 38973347564 took 43m31s and was
# misreported as a failure under the old fixed 1800s (issue #3316). 90 min is
# roughly 2x that run — headroom for a slow-runner day without letting a truly
# hung run block for hours. A timeout is "result unknown", not a CD failure;
# override per invocation with $VRG_CD_TIMEOUT, mirroring $VRG_CHECKS_TIMEOUT
# for the merge-gate wait.
_CD_WATCH_TIMEOUT_SECS = 5400
CD_TIMEOUT_ENV = "VRG_CD_TIMEOUT"
_CD_POLL_INTERVAL_SECS = 10

_CD_PASSING_CONCLUSIONS = ("success", "skipped", "neutral")


class CDRunLookupError(Exception):
    """The CD runs of a branch could not be listed (``gh`` failed, or bad JSON)."""


def cd_timeout_secs() -> int:
    """The CD watch deadline in seconds: ``$VRG_CD_TIMEOUT`` or 5400.

    Mirrors :func:`vergil_tooling.lib.github.checks_timeout_secs`: a malformed
    or non-positive override is a configuration error and raises rather than
    silently falling back — the deadline is what keeps a hung CD run from
    blocking forever, so it cannot be disabled (issue #3316).
    """
    raw = os.environ.get(CD_TIMEOUT_ENV, "").strip()
    if not raw:
        return _CD_WATCH_TIMEOUT_SECS
    try:
        secs = int(raw)
    except ValueError:
        secs = 0
    if secs <= 0:
        msg = f"{CD_TIMEOUT_ENV} must be a positive whole number of seconds, got {raw!r}"
        raise ValueError(msg)
    return secs


def _list_cd_runs(target_branch: str, *, repo: str | None = None) -> list[dict[str, object]]:
    """List the 30 most recent CD workflow runs on ``target_branch``, newest first.

    *repo* names the repository; without it ``gh`` uses the current one. An
    empty list means the workflow has no run on the branch yet. A ``gh``
    failure (which includes a repo with no CD workflow) or a malformed
    response raises :class:`CDRunLookupError` with the reason.
    """
    command = ["gh", "run", "list"]
    if repo is not None:
        command += ["--repo", repo]
    command += [
        "--workflow",
        _CD_WORKFLOW_NAME,
        "--branch",
        target_branch,
        "--limit",
        "30",
        "--json",
        "conclusion,databaseId,headSha,status,createdAt,url",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)  # noqa: S603
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        msg = f"`{' '.join(command)}` failed (exit {result.returncode}): {detail}"
        raise CDRunLookupError(msg)
    stdout = result.stdout or ""
    try:
        runs = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError as exc:
        msg = f"`{' '.join(command)}` returned malformed JSON: {exc}"
        raise CDRunLookupError(msg) from exc
    if not isinstance(runs, list):
        msg = f"`{' '.join(command)}` returned {type(runs).__name__}, not a list of runs"
        raise CDRunLookupError(msg)
    return runs


def _match_cd_run(runs: list[dict[str, object]], head_sha: str) -> dict[str, object] | None:
    """Return the run in ``runs`` whose ``headSha`` equals ``head_sha``."""
    for run in runs:
        if isinstance(run, dict) and (run.get("headSha") or "") == head_sha:
            return run
    return None


def _view_cd_run(run_id: object) -> dict[str, object] | None:
    """Fetch a CD run's current status/conclusion/url, or ``None`` on error."""
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gh",
            "run",
            "view",
            str(run_id),
            "--json",
            "status,conclusion,url",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout or ""
    try:
        data = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _poll_for_run(
    target_branch: str,
    head_sha: str,
    *,
    repo: str | None,
    dispatch_timeout: float,
    poll_interval: float = _CD_POLL_INTERVAL_SECS,
) -> dict[str, object] | None:
    """Wait up to *dispatch_timeout* for the CD run on *head_sha* to register.

    Returns the run, or ``None`` when no run registered in time. A listing
    that fails raises :class:`CDRunLookupError`.
    """
    deadline = time.monotonic() + dispatch_timeout
    while True:
        run = _match_cd_run(_list_cd_runs(target_branch, repo=repo), head_sha)
        if run is not None:
            return run
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def find_run(
    repo: str, branch: str, head_sha: str, *, dispatch_timeout: float
) -> dict[str, object] | None:
    """The CD run of *repo* on *branch* for *head_sha*, or ``None``.

    ``None`` means no run registered for the commit within *dispatch_timeout*.
    A listing that fails raises :class:`CDRunLookupError`.
    """
    return _poll_for_run(branch, head_sha, repo=repo, dispatch_timeout=dispatch_timeout)


def wait_completed(
    run_id: object, *, timeout: float, poll_interval: float = _CD_POLL_INTERVAL_SECS
) -> dict[str, object] | None:
    """Poll run *run_id* until it completes; its last view, or ``None`` at *timeout*.

    The run is viewed at least once, so a run already completed is returned
    with no wait. A view that fails or is malformed is retried, not fatal.
    """
    deadline = time.monotonic() + timeout
    while True:
        view = _view_cd_run(run_id)
        if view is not None and view.get("status") == "completed":
            return view
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval)


def watch_run(run_id: str, *, timeout: float) -> RunState:
    """Wait for CD run *run_id* to complete, up to *timeout* seconds; its state.

    Every status short of ``completed`` (``queued``, ``pending``, ``waiting``,
    ``requested``, ``in_progress``) is waited through. The result is:

    - ``success``: concluded ``success``, ``skipped`` or ``neutral``;
    - ``superseded``: cancelled by a concurrency supersede
      (:func:`_supersede_group`);
    - ``cancelled``: any other cancel;
    - ``failed``: any other conclusion;
    - ``timed_out``: still not completed at *timeout*.

    A cancel that cannot be checked for a supersede raises the lookup's error
    (``subprocess.CalledProcessError`` or ``ValueError``) rather than reading
    as either answer.
    """
    view = wait_completed(run_id, timeout=timeout)
    if view is None:
        return "timed_out"
    conclusion = str(view.get("conclusion") or "")
    if conclusion in _CD_PASSING_CONCLUSIONS:
        return "success"
    if conclusion == "cancelled":
        if _supersede_group(run_id, str(view.get("url") or "")) is not None:
            return "superseded"
        return "cancelled"
    return "failed"


@dataclass(frozen=True)
class CDRunProblem:
    """Why the CD run this merge triggered did not confirm a pass.

    Either the run *concluded* non-success (``conclusion`` set — a real CD
    failure) or the watch deadline elapsed while it was still running
    (``conclusion`` is ``None`` — result unknown, never reported as a failed
    run; issue #3316).
    """

    run_id: object
    target_branch: str
    head_sha: str
    url: str
    conclusion: str | None
    waited_secs: int
    # Why a cancelled run could not be cleared as a concurrency supersede, when
    # the lookup itself failed (issue #3834); shown with the failure.
    note: str = ""

    @property
    def timed_out(self) -> bool:
        return self.conclusion is None

    @property
    def run_ref(self) -> str:
        return f"CD workflow run {self.run_id} on {self.target_branch} ({self.head_sha[:7]})"

    def describe(self) -> str:
        if self.timed_out:
            detail = f"is still in progress after {self.waited_secs}s; result unknown."
        else:
            detail = f"ended with conclusion '{self.conclusion}'."
        text = f"{self.run_ref} {detail}\n  {self.url}"
        if self.note:
            text += f"\n  {self.note}"
        return text


@dataclass(frozen=True)
class CDRunSuperseded:
    """The CD run this merge triggered was cancelled by a concurrency supersede.

    GitHub cancelled its jobs because a higher-priority run was waiting on the
    same concurrency group (issue #3834). That is not a CD failure, but it is
    not a pass either: this run published nothing, and whichever run took its
    place is not checked here. GitHub records only the group, never the run
    that superseded it, and that run may be another workflow (a scheduled
    rebuild sharing the group) or a newer commit's CD, so it cannot be
    followed without guessing.
    """

    run_id: object
    target_branch: str
    head_sha: str
    url: str
    group: str

    @property
    def run_ref(self) -> str:
        return f"CD workflow run {self.run_id} on {self.target_branch} ({self.head_sha[:7]})"

    def describe(self) -> str:
        return (
            f"{self.run_ref} was superseded: GitHub cancelled it because a "
            f"higher-priority run was waiting on concurrency group '{self.group}'.\n"
            f"  {self.url}"
        )


# The check-run annotation GitHub writes on each job a concurrency group
# cancels to make way for a higher-priority waiting run. The group is quoted
# for a workflow-level group and bare for a job-level one.
_SUPERSEDE_ANNOTATION_RE = re.compile(
    r"^Canceling since a higher priority waiting request for '?(?P<group>.+?)'? exists$"
)
_RUN_URL_REPO_RE = re.compile(r"github\.com/([^/]+/[^/]+)/actions/runs/\d+")


def _supersede_group(run_id: object, url: str) -> str | None:
    """The concurrency group that superseded cancelled CD run *run_id*, or ``None``.

    A cancelled run is a supersede only when every job ended passing or
    cancelled, at least one was cancelled, and every cancelled job carries
    GitHub's supersede annotation. Any other cancel (by a human, or a run with
    no job to carry the annotation) returns ``None``, and so does a run where
    a job really failed. The annotations are read from the check-runs API (a
    job's id is its check-run id); the job logs do not carry the message.

    *url* is the run's URL, which names the repo the run belongs to. A ``gh``
    or API failure propagates, as does a payload of the wrong shape
    (``ValueError``): the caller reports the run as failed and says why.
    """
    match = _RUN_URL_REPO_RE.search(url)
    if match is None:
        msg = f"cannot read the repository of CD run URL {url!r}"
        raise ValueError(msg)
    repo = match.group(1)
    data = github.read_json("run", "view", str(run_id), "--repo", repo, "--json", "jobs")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        msg = f"gh run view {run_id} returned no jobs list"
        raise ValueError(msg)
    cancelled: list[object] = []
    for job in jobs:
        if not isinstance(job, dict):
            msg = f"gh run view {run_id} returned a job that is not an object: {job!r}"
            raise ValueError(msg)
        conclusion = job.get("conclusion")
        if conclusion == "cancelled":
            cancelled.append(job.get("databaseId"))
        elif conclusion not in _CD_PASSING_CONCLUSIONS:
            return None
    if not cancelled:
        return None
    groups: set[str] = set()
    for job_id in cancelled:
        endpoint = f"repos/{repo}/check-runs/{job_id}/annotations"
        annotations = github.read_json("api", endpoint)
        if not isinstance(annotations, list):
            msg = f"{endpoint} returned no annotations list"
            raise ValueError(msg)
        found = None
        for annotation in annotations:
            message = annotation.get("message") if isinstance(annotation, dict) else None
            hit = _SUPERSEDE_ANNOTATION_RE.match(str(message or "").strip())
            if hit is not None:
                found = hit.group("group")
                break
        if found is None:
            return None
        groups.add(found)
    return ", ".join(sorted(groups))


# -- job classification -------------------------------------------------------

_RELEASE_JOB_LEAF = "release"
_DOCS_JOB_FRAGMENT = "docs"


def is_release_job(name: str) -> bool:
    """True when *name* is the CD release job, identified by its leaf segment.

    Consumer repos call the reusable ``cd-release`` workflow, so their release
    job surfaces as the reusable leaf ``release / release``. vergil-actions
    *defines* that reusable workflow and cannot call itself, so its CD runs an
    inline job it names ``cd / release`` (#2001). Both share the leaf segment
    ``release`` — match on the leaf (the part after the last `` / ``), not the
    full name, so vergil-actions' own release verifies while a decoy like
    ``release-notes / build`` (leaf ``build``) still fails the #1853 guard.
    Matching the leaf exactly keeps that guard against loose substrings intact.
    """
    return name.rsplit(" / ", 1)[-1] == _RELEASE_JOB_LEAF


def is_docs_job(name: str) -> bool:
    """True when *name* is a docs job (its name contains ``docs``).

    Reusable-workflow leaf jobs are surfaced as ``<caller> / <job>`` (e.g.
    ``docs / docs``), so this substring-matches rather than compares exactly.
    """
    return _DOCS_JOB_FRAGMENT in name


def _job_family(name: str) -> str:
    return name.split(" / ", 1)[0]


def _did_not_succeed(job: dict[str, Any]) -> bool:
    """A completed job whose conclusion is neither ``success`` nor ``skipped``."""
    return job.get("status") == "completed" and job.get("conclusion") not in (
        "success",
        "skipped",
    )


def failed_job_labels(jobs: list[dict[str, Any]]) -> list[str]:
    """Ordered-unique families of completed non-release jobs that did not succeed.

    A job "did not succeed" when it completed with a conclusion that is neither
    ``success`` nor ``skipped`` (a skipped job — e.g. codeql — is not a
    failure). A job that has not completed is not judged here (#3352).
    Reusable leaves are ``<family> / <job>``; collapse to the family so a
    matrix of failed ``docker-publish`` leaves reports once as
    ``docker-publish`` — except when the family is the release job's own
    (``release / package-sign``), where the bare family would read as the
    release itself failing, so the full job name is reported. The release job
    itself is never listed: ``confirm-main`` judges it on its own.
    """
    release_families = {_job_family(n) for j in jobs if is_release_job(n := j.get("name", ""))}
    failed: list[str] = []
    for job in jobs:
        name = job.get("name", "")
        if is_release_job(name) or not _did_not_succeed(job):
            continue
        family = _job_family(name)
        label = name if family in release_families else family
        if label and label not in failed:
            failed.append(label)
    return failed


def classify_jobs(jobs: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Split a run's failed jobs into ``(blocking_failures, deferred_failures)``.

    Labels follow :func:`failed_job_labels`. A docs job's failure is deferred,
    as ``confirm-develop`` defers a develop docs publish; every other failure,
    a release job's included, is blocking. Jobs that have not completed are
    in neither list.
    """
    docs = [job for job in jobs if is_docs_job(job.get("name", ""))]
    other = [job for job in jobs if not is_docs_job(job.get("name", ""))]
    release_failures = [
        name
        for job in other
        if is_release_job(name := job.get("name", "")) and _did_not_succeed(job)
    ]
    return [*release_failures, *failed_job_labels(other)], failed_job_labels(docs)
