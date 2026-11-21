"""Preflight checks for vrg-release."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import cd_runs, config, git, github, progress, version
from vergil_tooling.lib.checkout_lock import checkout_lock
from vergil_tooling.lib.managed_worktree import (
    ManagedWorktreeError,
    adopt_worktree,
    create_worktree,
    worktree_path,
)
from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import published
from vergil_tooling.lib.pending_release import (
    PendingRelease,
    PendingReleaseError,
    gather_release_contents,
)
from vergil_tooling.lib.process_errors import command_text, error_text
from vergil_tooling.lib.release import state
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.resume import _SEARCH_COMMAND, _open_release_issues
from vergil_tooling.lib.release.tracking import render_contents
from vergil_tooling.lib.self_repo import is_self_repo
from vergil_tooling.lib.update_deps.context import DEPS_LABEL
from vergil_tooling.lib.update_deps.pr import FIX_TASK_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.release.state import IssueEntry

_VERSION_OVERRIDE_FIELDS = ("minor", "major")

# The "Fix task: <url>" line vrg-update-deps writes into a failed PR's body.
_FIX_TASK_RE = re.compile(rf"^{re.escape(FIX_TASK_PREFIX)}(\S+)\s*$", re.MULTILINE)
_PENDING_DEPS_COMMAND = f"gh pr list --state open --label {DEPS_LABEL}"
# Upper bound on the open dependency-update PRs listed; reaching it refuses the
# release, since a pending PR beyond it would go unseen (#4038).
_PENDING_DEPS_LIMIT = 100
# How long preflight waits for the container runtime to answer `<runtime> version`.
_RUNTIME_PROBE_TIMEOUT = 15
# A fix task is an issue URL; it may live in another repo, so the repo comes
# from the URL rather than from the release's own repo (#3286).
_ISSUE_URL_RE = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)/?$")


@dataclass(frozen=True)
class PendingDepsPr:
    """An open ``dependency-update`` PR, with its fix task when one is linked."""

    head: str
    url: str
    fix_task: str | None


@dataclass(frozen=True)
class _DepsGateOutcome:
    """What the dependency-update gate did: PRs it closed, and PRs it let through.

    ``undeleted`` names the head branches of closed PRs whose deletion failed
    (#3332), so the leftover is recorded rather than silent.
    """

    cleared: list[str]
    bypassed: list[str]
    undeleted: list[str]


def preflight(
    *,
    version_override: str | None,
    repo_root: Path,
    resume: bool = False,
    resume_version: str | None = None,
    resume_issue_number: int | None = None,
    allow_pending_deps: bool = False,
) -> ReleaseContext:
    """Run all preflight checks and return an initialized ReleaseContext.

    Every run first brings the root checkout's clean ``develop`` up to
    ``origin/develop`` under the checkout lock (:func:`_check_branch_and_tree`),
    so a release is cut from the newest head rather than refused when a merge
    landed since the last pull (spec §6.6 of epic vergil-project/.github#1218).

    On *resume* (#1612), the version comes from the adopted tracking issue
    (*resume_version*) rather than the develop VERSION file (which may already
    be bumped); the checks that assume a fresh start are skipped — the version
    is expected to be tagged and the tracking issue is expected to exist — an
    existing release branch is adopted rather than refused, and the adopted
    issue (*resume_issue_number*) is pre-set on the context.

    A fresh start refuses when ``origin/main`` has commits ``origin/develop``
    lacks (#3527), before any other fresh-start check. A resume skips it: the
    release it adopts may sit between merge-release and back-merge-bump, where
    main is ahead of develop by design.

    A fresh start also refuses while any open PR carries the
    ``dependency-update`` label (spec §5.3, epic vergil-project/.github#381);
    *allow_pending_deps* bypasses that gate, and the bypassed PR URLs are
    recorded on the context so the prepare-stage comment names them. Before
    the gate, a PR whose fix task closed as completed is closed (its update
    landed through the fix PR) and recorded as cleared (#3286); the bypass
    applies only to what still blocks after that. A resume skips the gate —
    the release it adopts already passed it.

    A fresh start last requires a green develop CD run for the develop head it
    releases (:func:`check_develop_cd`, spec §6.6 of epic
    vergil-project/.github#1218). It runs after every other check, since it
    may wait on a CD run in progress. A resume skips it too: the release it
    adopts already passed it.
    """
    _check_host_prerequisites()
    repo = check_gh_auth()
    _read_and_validate_config(repo_root)
    _check_branch_and_tree(repo_root)

    if resume and resume_version is not None:
        release_version = resume_version
    else:
        try:
            current_version = version.show(repo_root)
        except (FileNotFoundError, version.VersionSyncError, version.CargoVersionError) as exc:
            raise ReleaseError(
                phase="preflight",
                command="version.show",
                message=str(exc),
            ) from exc

        if version_override in _VERSION_OVERRIDE_FIELDS:
            release_version = _compute_release_version(current_version, version_override)
        else:
            release_version = current_version

    deps_gate = _DepsGateOutcome(cleared=[], bypassed=[], undeleted=[])
    if not resume:
        _check_main_not_ahead_of_develop()
        _check_version_not_tagged(release_version)
        _check_no_open_release(repo)
        _check_packages_unpublished(repo, repo_root, release_version)
        deps_gate = _check_pending_deps(repo, allow=allow_pending_deps)
        _develop_cd_gate(repo)

    branch = f"release/{release_version}"
    worktree = _acquire_release_worktree(repo_root, branch, resume=resume)
    os.chdir(worktree)
    cut: str | None = None
    pending: PendingRelease | None = None
    issues: tuple[IssueEntry, ...] = ()
    if not resume:
        cut, pending, issues = _release_contents(worktree)

    print(f"Preflight passed: {repo} v{release_version} — worktree {worktree}")
    ctx = ReleaseContext(
        repo=repo,
        version=release_version,
        repo_root=repo_root,
        version_override=version_override,
        release_branch=branch,
        cut=cut,
        pending_release=pending,
        issues=issues,
        worktree_path=worktree,
        bypassed_deps_prs=deps_gate.bypassed,
        cleared_deps_prs=deps_gate.cleared,
        undeleted_deps_branches=deps_gate.undeleted,
    )
    if resume_issue_number is not None:
        ctx.issue_number = resume_issue_number
        ctx.issue_url = f"https://github.com/{repo}/issues/{resume_issue_number}"
    return ctx


def pending_deps_prs(repo: str) -> list[PendingDepsPr]:
    """Open PRs labelled ``dependency-update`` in *repo*, with their fix task if linked.

    A lookup that cannot complete raises :class:`ReleaseError` — an unknown
    answer must never read as "no pending PRs" and let the release through.
    """
    try:
        raw = github.read_json(
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            DEPS_LABEL,
            "--json",
            "url,body,headRefName",
            "--limit",
            str(_PENDING_DEPS_LIMIT),
        )
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=_PENDING_DEPS_COMMAND,
            message="Could not list open dependency-update PRs; refusing to release blind.",
            detail=error_text(exc),
        ) from exc
    if not isinstance(raw, list):
        raise ReleaseError(
            phase="preflight",
            command=_PENDING_DEPS_COMMAND,
            message=f"gh pr list returned {type(raw).__name__}, expected a list.",
        )
    if len(raw) >= _PENDING_DEPS_LIMIT:
        raise ReleaseError(
            phase="preflight",
            command=_PENDING_DEPS_COMMAND,
            message=(
                f"gh pr list returned {_PENDING_DEPS_LIMIT}+ open dependency-update PRs, "
                "so the list may be truncated; refusing to release blind."
            ),
        )
    found: list[PendingDepsPr] = []
    for item in raw:
        entry = cast("dict[str, object]", item) if isinstance(item, dict) else {}
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            raise ReleaseError(
                phase="preflight",
                command=_PENDING_DEPS_COMMAND,
                message=f"gh pr list returned an entry without a url: {item!r}",
            )
        head = entry.get("headRefName")
        if not isinstance(head, str) or not head:
            raise ReleaseError(
                phase="preflight",
                command=_PENDING_DEPS_COMMAND,
                message=f"gh pr list returned an entry without a head branch: {item!r}",
            )
        body = entry.get("body")
        match = _FIX_TASK_RE.search(body if isinstance(body, str) else "")
        found.append(PendingDepsPr(head=head, url=url, fix_task=match.group(1) if match else None))
    return found


def _gh_error_detail(exc: Exception) -> str:
    """A gh failure's message with its stderr and stdout, else the exception text."""
    return error_text(exc)


def fix_task_status(url: str) -> tuple[str, str | None]:
    """The fix task issue's ``(state, stateReason)``; the reason is None when unset.

    The issue may live in any repo, so *url* must be a full GitHub issue URL.
    An unparseable URL or a lookup that cannot complete raises
    :class:`ReleaseError` — an unknown fix task never clears a PR.
    """
    match = _ISSUE_URL_RE.match(url)
    if match is None:
        raise ReleaseError(
            phase="preflight",
            command="parse fix task URL",
            message=(
                f"Cannot parse fix task URL {url!r}: expected "
                "https://github.com/<owner>/<repo>/issues/<number>."
            ),
        )
    issue_repo, number = match.groups()
    command = f"gh issue view {number} --repo {issue_repo} --json state,stateReason"
    try:
        raw = github.read_json(
            "issue", "view", number, "--repo", issue_repo, "--json", "state,stateReason"
        )
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=f"Could not look up fix task {url}; refusing to release blind.",
            detail=_gh_error_detail(exc),
        ) from exc
    state = raw.get("state") if isinstance(raw, dict) else None
    if not isinstance(state, str) or not state:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=f"gh issue view returned no state for fix task {url}: {raw!r}",
        )
    reason = raw.get("stateReason") if isinstance(raw, dict) else None
    return state, reason if isinstance(reason, str) and reason else None


def _close_landed_deps_pr(pr: PendingDepsPr) -> None:
    """Comment on, then close, a deps PR whose fix task completed (#3286).

    Any failure raises :class:`ReleaseError` naming the state the PR was left in.
    """
    comment = (
        f"Closed by the `vrg-release` preflight: fix task {pr.fix_task} closed as "
        "completed, so this dependency update landed through its fix PR. A "
        "squash-merged fix PR never marks this PR merged, so the release closes it "
        "to clear the dependency-update gate."
    )
    try:
        github.run("pr", "comment", pr.url, "--body", comment)
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"gh pr comment {pr.url}",
            message=(
                f"Could not comment on dependency-update PR {pr.url} before closing it; "
                "it is still open and unchanged, so it still blocks the release."
            ),
            detail=_gh_error_detail(exc),
        ) from exc
    try:
        # Deliberately not `--delete-branch`: gh exits non-zero when the PR
        # closed but the branch delete failed, which would read as "still
        # open" here. The branch is deleted as its own step instead (#3332).
        github.run("pr", "close", pr.url)
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"gh pr close {pr.url}",
            message=(
                f"Could not close dependency-update PR {pr.url}: preflight commented "
                "that it was closing it, but the PR is still open. Close it by hand "
                "(its fix task completed) and re-run vrg-release."
            ),
            detail=_gh_error_detail(exc),
        ) from exc


def _delete_deps_branch(repo: str, pr: PendingDepsPr) -> bool:
    """Delete a closed deps PR's head branch on origin; False if that failed (#3332).

    GitHub's delete-on-merge setting never applies to a PR closed without
    merging, so the closer deletes the branch. A branch already gone counts as
    deleted. A failure does not undo the close — the gate is cleared — but it
    is printed loudly and recorded on the tracking issue by the caller.
    """
    try:
        github.run("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{pr.head}")
    except subprocess.CalledProcessError as exc:
        detail = _gh_error_detail(exc)
        if "Reference does not exist" in detail:
            print(f"Branch {pr.head} of {pr.url} is already deleted.")
            return True
        print(
            f"WARNING: closed dependency-update PR {pr.url}, but could not delete its "
            f"branch {pr.head}: {detail}\nDelete it by hand: "
            f"git push origin --delete {pr.head}"
        )
        return False
    print(f"Deleted branch {pr.head} of closed dependency-update PR {pr.url}.")
    return True


def _check_pending_deps(repo: str, *, allow: bool) -> _DepsGateOutcome:
    """Clear landed deps PRs, then refuse on what still blocks unless *allow*.

    A PR whose fix task closed as completed is closed (#3286) and its head
    branch deleted (#3332); a failed delete is recorded, not fatal. Every fix task
    is looked up before any PR is closed, so a lookup failure leaves nothing
    changed. What remains open blocks (spec §5.3) unless *allow* bypasses it.
    """
    pending = pending_deps_prs(repo)
    landed: list[PendingDepsPr] = []
    blocking: list[tuple[PendingDepsPr, str]] = []
    for pr in pending:
        if pr.fix_task is None:
            blocking.append(
                (
                    pr,
                    " (no fix task linked — the update is still pending or was never "
                    "linked to a fix task; merge this PR if it is green, or close it "
                    "deliberately)",
                )
            )
            continue
        state, reason = fix_task_status(pr.fix_task)
        if state == "CLOSED" and reason == "COMPLETED":
            landed.append(pr)
        elif state == "CLOSED":
            blocking.append(
                (
                    pr,
                    f" (fix task {pr.fix_task} closed without completing "
                    f"({reason or 'no state reason'}) — the update did not land; "
                    "deciding what to do with this PR is a human call: close it "
                    "deliberately or reopen the fix task)",
                )
            )
        else:
            blocking.append(
                (
                    pr,
                    f" (fix task {pr.fix_task} is open — finish the fix: merging its PR "
                    "closes the fix task as completed, and the next release then "
                    "closes this PR itself)",
                )
            )

    cleared: list[str] = []
    undeleted: list[str] = []
    for pr in landed:
        _close_landed_deps_pr(pr)
        print(
            f"Closed dependency-update PR {pr.url}: fix task {pr.fix_task} closed as "
            "completed, so the update landed."
        )
        cleared.append(pr.url)
        if not _delete_deps_branch(repo, pr):
            undeleted.append(pr.head)

    if not blocking:
        return _DepsGateOutcome(cleared=cleared, bypassed=[], undeleted=undeleted)
    if allow:
        urls = [pr.url for pr, _ in blocking]
        print(
            "WARNING: --allow-pending-deps is bypassing open dependency-update "
            f"PR(s): {', '.join(urls)}"
        )
        return _DepsGateOutcome(cleared=cleared, bypassed=urls, undeleted=undeleted)
    described = "; ".join(pr.url + note for pr, note in blocking)
    raise ReleaseError(
        phase="preflight",
        command=_PENDING_DEPS_COMMAND,
        message=(
            f"Open dependency-update PR(s) block this release: {described}. For an "
            "urgent release, pass --allow-pending-deps (the bypass is logged on the "
            "tracking issue)."
        ),
    )


def _release_contents(worktree: Path) -> tuple[str, PendingRelease, tuple[IssueEntry, ...]]:
    """The commit a fresh release branch is cut from, what ``main..<cut>`` holds, and its issues.

    The new branch's worktree is at the ``develop`` commit preflight synced
    to, so its ``HEAD`` is the cut. The pending release and the issue list
    are read pinned to it, never to whatever ``origin/develop`` holds by
    then, and printed as the tracking issue's Contents section will show
    them, before the tracking issue is created. An incomplete lookup refuses
    the release (spec §4.1 of epic vergil-project/.github#1440).
    """
    cut = git.read_output("-C", str(worktree), "rev-parse", "HEAD")
    try:
        pending, issues = gather_release_contents(cut=cut)
    except PendingReleaseError as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"gather pending release main..{cut[:7]}",
            message=f"Could not read what the release contains: {exc}",
        ) from exc
    print(render_contents(pending, issues))
    return cut, pending, issues


def _acquire_release_worktree(repo_root: Path, branch: str, *, resume: bool) -> Path:
    """Create the managed worktree on *branch* off develop, or adopt it on resume.

    All release branch work happens in the worktree, so the root checkout's HEAD
    never moves while the release runs (#1578). A fresh run refuses an existing
    release branch (the lock), unless it is the leftover of an interrupted run
    that recorded nothing, which it adopts (#4367, :func:`_adopt_unrecorded_branch`);
    a resume adopts it — creating the local branch from origin if needed, then
    attaching a worktree (#1612).

    Every git failure on the way, on the fresh path as on the resume path,
    surfaces as a :class:`ReleaseError` naming the branch, the worktree and
    the recovery step, never as a raw ``CalledProcessError`` (#4442).
    """
    exists = git.ref_exists(branch) or git.ref_exists(f"origin/{branch}")
    try:
        if exists and not resume:
            return _adopt_unrecorded_branch(repo_root, branch)
        if resume and exists:
            if not git.ref_exists(branch):
                git.run("branch", branch, f"origin/{branch}")
            return adopt_worktree(repo_root, branch=branch)
        return create_worktree(repo_root, branch=branch, base="develop")
    except ManagedWorktreeError as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"git worktree add {branch}",
            message=str(exc),
            detail=exc.detail,
        ) from exc
    except subprocess.CalledProcessError as exc:
        rerun = "`vrg-release --resume`" if resume else "`vrg-release`"
        raise ReleaseError(
            phase="preflight",
            command=command_text(exc.cmd),
            message=(
                f"A git command failed while setting up release branch '{branch}' "
                f"in its worktree {worktree_path(repo_root, branch)}. Fix the cause "
                f"git reports below, then re-run {rerun}."
            ),
            detail=error_text(exc),
        ) from exc


def _adopt_unrecorded_branch(repo_root: Path, branch: str) -> Path:
    """Adopt a release branch an interrupted fresh run left with no record (#4367).

    Preflight creates the release branch and its worktree before prepare opens
    the tracking issue, so a run stopped in between (an interrupt, a crash)
    leaves a local branch that nothing records and that a fresh run would
    otherwise refuse as an existing release. A fresh run reaches this only
    after it has confirmed that no release tracking issue is open.

    The branch is adopted only when nothing on it can be lost: it is local, it
    was never pushed (``ls-remote`` asks origin itself, not the possibly stale
    remote-tracking refs), it has no commit ``develop`` lacks, and its managed
    worktree, if one is there, is clean and on the branch. The worktree is then
    reused (or attached) and fast-forwarded to ``develop``. Anything else
    refuses with the reason. Nothing is ever deleted.
    """
    exists = f"Release branch '{branch}' already exists"
    if not git.ref_exists(branch):
        raise ReleaseError(
            phase="preflight",
            command=f"git rev-parse origin/{branch}",
            message=(
                f"{exists} on origin, so an earlier release pushed it. If its "
                "tracking issue is open, finish it with `vrg-release --resume`; "
                "otherwise find out what it holds before removing it."
            ),
        )
    command = f"git ls-remote --heads origin refs/heads/{branch}"
    try:
        on_origin = git.read_output("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=f"{exists} locally, and origin could not be asked whether it was pushed.",
            detail=error_text(exc),
        ) from exc
    if on_origin:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=(
                f"{exists} and was pushed to origin, so it may hold release work. If "
                "its tracking issue is open, finish it with `vrg-release --resume`; "
                "otherwise find out what it holds before removing it."
            ),
        )
    ahead = git.commits_ahead("develop", branch)
    if ahead:
        raise ReleaseError(
            phase="preflight",
            command=f"git rev-list --count develop..{branch}",
            message=(
                f"{exists} locally with {ahead} commit(s) develop lacks, and no open "
                "tracking issue records it. Find out what those commits are before "
                "removing the branch and its worktree."
            ),
        )
    path = worktree_path(repo_root, branch)
    if path.exists():
        _check_leftover_worktree(path, branch)
    path = adopt_worktree(repo_root, branch=branch)
    git.run("-C", str(path), "merge", "--ff-only", "develop")
    print(
        f"Adopted '{branch}' and its worktree {path}: an earlier run stopped before "
        "recording anything (not pushed, no commits, no tracking issue)."
    )
    return path


def _check_leftover_worktree(path: Path, branch: str) -> None:
    """Refuse a leftover worktree path unless it is a clean checkout of *branch*."""
    current = git.read_output("-C", str(path), "rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        raise ReleaseError(
            phase="preflight",
            command=f"git -C {path} rev-parse --abbrev-ref HEAD",
            message=(
                f"Release branch '{branch}' already exists, and its worktree path "
                f"{path} is on '{current}', not on it. Find out what is there before "
                "removing it."
            ),
        )
    status = git.read_output("-C", str(path), "status", "--porcelain")
    if status:
        raise ReleaseError(
            phase="preflight",
            command=f"git -C {path} status --porcelain",
            message=(
                f"Release branch '{branch}' already exists, and its worktree {path} "
                "has uncommitted changes. Find out what they are before removing it."
            ),
            detail=status,
        )


def _compute_release_version(current: str, override: str) -> str:
    """Compute the target release version without modifying any files."""
    parts = current.split(".")
    if override == "minor":
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{int(parts[0]) + 1}.0.0"


def _check_host_prerequisites() -> None:
    """Refuse unless a container runtime is reachable for the release's container work.

    The changelog and release notes are generated by git-cliff in the dev
    container, through ``vrg-container-run`` (#4169), and the version bump
    refreshes the lockfile there too (``uv lock``, ``cargo update
    --workspace``; #4407), so the host needs a running docker or nerdctl, not
    git-cliff, uv or cargo. Checked here, on a resume as on a fresh start, so
    a missing or stopped runtime stops the release before any stage runs.
    """
    runtime = next((name for name in ("nerdctl", "docker") if shutil.which(name)), None)
    if runtime is None:
        raise ReleaseError(
            phase="preflight",
            command="which nerdctl docker",
            message=(
                "No container runtime (docker or nerdctl) is on PATH. vrg-release "
                "generates the changelog and refreshes the lockfile in the dev "
                "container (vrg-container-run); install and start one, then re-run."
            ),
        )
    try:
        subprocess.run(  # noqa: S603
            (runtime, "version"),
            check=True,
            capture_output=True,
            text=True,
            timeout=_RUNTIME_PROBE_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"{runtime} version",
            message=(
                f"The {runtime} container runtime is not reachable. vrg-release "
                "generates the changelog and refreshes the lockfile in the dev "
                "container (vrg-container-run); start the runtime, then re-run."
            ),
            detail=error_text(exc),
        ) from exc


def run_audit(repo_root: Path) -> None:
    """Standalone audit stage: resolve the repo and audit its configuration.

    Releasing vergil-tooling itself (*repo_root* is the self repo) audits its
    managed files against the templates in the source tree being released,
    not the installed release's: a template change on develop also updates
    vergil-tooling's own managed files, so the installed templates would
    report drift that is not there (issue #4414). The GitHub-side audit, and
    every other repo's audit, are unchanged.
    """
    audit_repo_config(check_gh_auth(), in_tree_templates=is_self_repo(repo_root))


def check_gh_auth() -> str:
    try:
        return github.read_output(
            "repo",
            "view",
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
        )
    except Exception as exc:
        raise ReleaseError(
            phase="preflight",
            command="gh repo view",
            message="GitHub CLI authentication failed.",
            detail=error_text(exc),
        ) from exc


def _read_and_validate_config(repo_root: Path) -> None:
    config.read_config(repo_root)


class DevelopDivergedError(Exception):
    """Local ``develop`` cannot fast-forward to ``origin/develop``."""

    def __init__(self, local_sha: str, remote_sha: str) -> None:
        self.local_sha = local_sha
        self.remote_sha = remote_sha
        super().__init__(
            f"Local develop ({local_sha[:8]}) has diverged from "
            f"origin/develop ({remote_sha[:8]}); resolve it first."
        )


def fast_forward_develop() -> None:
    """Fast-forward the checked-out ``develop`` to the fetched ``origin/develop``.

    Does nothing when the two are equal. When local ``develop`` is an
    ancestor of ``origin/develop`` it runs ``git merge --ff-only`` and prints
    ``Fast-forwarded develop <old8>..<new8>``. Otherwise (diverged, or
    carrying local commits) it raises :class:`DevelopDivergedError`.

    The caller has checked that the tree is a clean ``develop``, has fetched
    ``origin develop``, and holds the checkout lock (spec §6.6 of epic
    vergil-project/.github#1218).
    """
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha == remote_sha:
        return
    if not git.is_ancestor(local_sha, remote_sha):
        raise DevelopDivergedError(local_sha, remote_sha)
    git.run("merge", "--ff-only", "origin/develop")
    print(f"Fast-forwarded develop {local_sha[:8]}..{remote_sha[:8]}")


def _check_branch_and_tree(repo_root: Path) -> None:
    """Bring the root checkout's clean ``develop`` up to ``origin/develop``.

    Holds the checkout lock (spec §6.4) for just this sync. It refuses a
    branch other than ``develop`` or a dirty tree, fetches ``--tags --force``
    (so the already-tagged check sees remote tags), and fast-forwards a local
    ``develop`` that is behind, refusing one that has diverged. The lock is
    released before the develop-CD gate, which may wait on a CD run, checks
    the new ``HEAD``.
    """
    with checkout_lock(repo_root, purpose="vrg-release preflight"):
        branch = git.current_branch()
        if branch != "develop":
            raise ReleaseError(
                phase="preflight",
                command="git rev-parse --abbrev-ref HEAD",
                message=f"Must be on develop branch (currently on '{branch}').",
            )
        status = git.read_output("status", "--porcelain")
        if status:
            raise ReleaseError(
                phase="preflight",
                command="git status --porcelain",
                message="Working tree is not clean.",
                detail=status,
            )
        git.run("fetch", "--tags", "--force", "origin", "develop")
        try:
            fast_forward_develop()
        except DevelopDivergedError as exc:
            raise ReleaseError(
                phase="preflight",
                command="git merge-base --is-ancestor HEAD origin/develop",
                message=str(exc),
            ) from exc


def audit_repo_config(repo: str, *, in_tree_templates: bool = False) -> None:
    command: tuple[str, ...] = ("vrg-github-repo-config", "audit", "--repo", repo)
    if in_tree_templates:
        command = (*command, "--in-tree-templates")
    result = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    # Exit 1 is a genuine compliance diff; anything else means the audit
    # could not complete (auth/API failure, missing config). Don't conflate
    # the two — a crash is not a non-compliance verdict (#1691). Either way
    # the captured output rides along as detail so the real reason survives.
    if result.returncode == 1:
        message = "Repository configuration is non-compliant."
    else:
        message = "Repository configuration audit could not complete."
    raise ReleaseError(
        phase="preflight",
        command=" ".join(command),
        message=message,
        detail=result.stdout + result.stderr,
    )


def _check_main_not_ahead_of_develop() -> None:
    """Refuse a fresh release while ``origin/main`` has commits develop lacks (#3527).

    A completed release leaves main contained in develop: back-merge-bump
    branches ``release/post-<version>`` from main and merges it to develop with
    a merge commit. Main is ahead only when a release stalled between
    merge-release and back-merge-bump (as 2.1.6 did at confirm-main), or when
    something landed on main directly. A fresh release on top of that fails
    late, at merge-release; here nothing is cut yet. Develop being ahead of
    main is normal and never refuses. A failed fetch or compare refuses too —
    an unknown answer never reads as "not diverged".
    """
    command = "git log origin/develop..origin/main"
    try:
        git.run("fetch", "origin", "main")
        ahead = git.read_output("log", "--format=%h %s", "origin/develop..origin/main")
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message="Could not compare origin/main with origin/develop; refusing to release blind.",
            detail=error_text(exc),
        ) from exc
    if not ahead:
        return
    count = len(ahead.splitlines())
    raise ReleaseError(
        phase="preflight",
        command=command,
        message=(
            f"origin/main has {count} commit(s) not on origin/develop, so main and "
            "develop have diverged. If an earlier release stopped before its "
            "back-merge (its tracking issue is still open), finish it with "
            "`vrg-release --resume`. Otherwise back-merge main into develop: "
            "`git fetch origin && git switch -c release/back-merge-main origin/main "
            "&& git push -u origin release/back-merge-main`, open a PR from it to "
            "develop, merge it with a merge commit (a squash leaves main's commits "
            "off develop), then re-run vrg-release."
        ),
        detail=ahead,
    )


def _check_version_not_tagged(ver: str) -> None:
    try:
        latest_tag = git.read_output(
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            "v*",
        )
    except subprocess.CalledProcessError:
        return
    if latest_tag == f"v{ver}":
        raise ReleaseError(
            phase="preflight",
            command="git describe --tags --match v*",
            message=(
                f"Version {ver} is already tagged as {latest_tag}. "
                f"The post-publish version bump may not have run."
            ),
        )


def _check_packages_unpublished(repo: str, repo_root: Path, ver: str) -> None:
    """Refuse a release that would re-publish an already published package (#3309).

    Package builds are not byte-reproducible, so rebuilding a ``(name, full
    version, arch, format)`` an earlier release published yields different
    files at the same identity, a conflict apt and dnf cannot resolve. The
    shared package index does not check for it.

    This is the earliest point that can know: the full package versions follow
    from ``[package]`` and the release version alone, and nothing is branched,
    tagged or published yet. It runs before the dependency-update gate, which
    may close PRs, so a refusal here changes nothing. ``vrg-package
    check-unpublished`` runs the same check in the CD packaging path as the
    backstop. Any failure to read the published state refuses; it never skips.
    """
    try:
        cfg = config.read_config(repo_root)
        identities = published.check_release(repo_root, cfg, repo, ver)
    except (PackageError, config.ConfigError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"vrg-package check-unpublished --repo {repo} --version {ver}",
            message=str(exc),
        ) from exc
    if identities:
        print(f"Package check passed: {len(identities)} package(s) not yet published by {repo}")


def _open_release_recovery(repo: str, ver: str, number: int, body: str) -> str:
    """The line naming open release *ver* (issue *number*) and how to finish it."""
    url = f"https://github.com/{repo}/issues/{number}"
    try:
        state.parse_state(body)
    except state.StateBlockError as exc:
        if state.BEGIN not in body and state.END not in body:
            return (
                f"release {ver} ({url}) has no release state block: older vrg-release "
                "tooling opened it, and `vrg-release --resume` cannot finish it. Finish "
                "that release by hand, then close the issue."
            )
        return (
            f"release {ver} ({url}) has a state block that cannot be read ({exc}). "
            f"Fix the block, then finish it with `vrg-release --resume {ver}`."
        )
    return f"release {ver} ({url}) is open. Finish it with `vrg-release --resume {ver}`."


def _check_no_open_release(repo: str) -> None:
    """Refuse a fresh start while any ``release: X.Y.Z`` tracking issue is open.

    One release runs at a time (spec §4.7 of epic vergil-project/.github#1440),
    whatever its version. Each open release is named with its recovery: the
    ``vrg-release --resume`` that finishes it, or, for an issue with no state
    block (left by older tooling), finishing it by hand and closing the issue.
    """
    issues = _open_release_issues(repo, phase="preflight")
    if not issues:
        print("Open-release check passed: no release is in flight")
        return
    lines = [
        f"- {_open_release_recovery(repo, ver, number, body)}"
        for ver, number, body in sorted(issues)
    ]
    raise ReleaseError(
        phase="preflight",
        command=_SEARCH_COMMAND,
        message=(
            "One release runs at a time, and a fresh vrg-release refuses while "
            "another is open:\n" + "\n".join(lines)
        ),
    )


# -- the develop-CD gate (epic vergil-project/.github#1218, spec §6.6) ---------

_DEVELOP = "develop"
_CD_RUNS_COMMAND = f"gh run list --workflow {cd_runs._CD_WORKFLOW_NAME} --branch {_DEVELOP}"
# How many supersedes in a row the gate follows before it gives up: each one
# means a newer run took the place of the one it was judging.
_MAX_SUPERSEDES = 10


def _develop_cd_gate(repo: str) -> None:
    """Run :func:`check_develop_cd` on the develop head preflight fast-forwarded to."""
    check_develop_cd(repo, git.read_output("rev-parse", "HEAD"))


def check_develop_cd(repo: str, head_sha: str) -> None:
    """Refuse a release unless develop's CD run for *head_sha* is green.

    The run is found by its commit, as finalize finds it (:mod:`cd_runs`).
    A run queued, pending or in progress is waited on, bounded by
    ``$VRG_CD_TIMEOUT``. A run that failed, timed out, or was cancelled other
    than by a concurrency supersede raises :class:`ReleaseError` with its URL.
    A failure only of a docs job (one ``confirm-develop`` defers) prints a
    loud warning instead. A superseded run is skipped in favour of the run
    created next on develop, which is judged the same way; with no such run
    the gate refuses, since nothing green covers *head_sha*.

    Only when no run registers for *head_sha* within the dispatch window does
    the gate fall back to the latest completed develop CD run whose head is an
    ancestor of *head_sha*, which must be green. A listing or lookup that
    fails refuses: an unknown answer never reads as green.
    """
    try:
        timeout = cd_runs.cd_timeout_secs()
    except ValueError as exc:
        raise ReleaseError(
            phase="preflight", command=cd_runs.CD_TIMEOUT_ENV, message=str(exc)
        ) from exc
    deadline = time.monotonic() + timeout
    print(f"Checking develop CD for {head_sha[:12]}...")
    run = _cd_lookup(
        lambda: cd_runs.find_run(
            repo, _DEVELOP, head_sha, dispatch_timeout=cd_runs._CD_DISPATCH_TIMEOUT_SECS
        )
    )
    if run is None:
        _judge_ancestor_run(repo, head_sha)
        return
    for _ in range(_MAX_SUPERSEDES + 1):
        url = _run_url(repo, run)
        print(f"  Develop CD run: {url}")
        state = _watch(run, timeout=max(0.0, deadline - time.monotonic()))
        if state != "superseded":
            unfinished = (
                f"The develop CD run is still not complete after {timeout}s; result unknown. "
                f"Re-run vrg-release once it finishes, or raise {cd_runs.CD_TIMEOUT_ENV}."
            )
            _judge_run(repo, run, state, unfinished=unfinished)
            return
        newer = _next_run(repo, run)
        if newer is None:
            raise ReleaseError(
                phase="preflight",
                command=_CD_RUNS_COMMAND,
                message=(
                    f"The develop CD run for {head_sha[:12]} was superseded by a concurrency "
                    "group, and no newer develop CD run took its place, so nothing green "
                    f"covers this commit. Re-run it (gh run rerun {run.get('databaseId')} "
                    f"--repo {repo}), then re-run vrg-release. Run: {url}"
                ),
            )
        print(f"  Superseded; following the newer develop CD run {newer.get('databaseId')}.")
        run = newer
    raise ReleaseError(
        phase="preflight",
        command=_CD_RUNS_COMMAND,
        message=(
            f"Develop CD runs were superseded {_MAX_SUPERSEDES + 1} times in a row; "
            f"refusing to release until one completes. Last run: {_run_url(repo, run)}"
        ),
    )


def _cd_lookup[T](call: Callable[[], T]) -> T:
    """Run a CD-run lookup, turning its failure into a preflight refusal."""
    try:
        return call()
    except (cd_runs.CDRunLookupError, subprocess.CalledProcessError, ValueError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=_CD_RUNS_COMMAND,
            message="Could not read develop's CD runs; refusing to release without them.",
            detail=error_text(exc),
        ) from exc


def _run_url(repo: str, run: dict[str, object]) -> str:
    return str(run.get("url") or f"https://github.com/{repo}/actions/runs/{run.get('databaseId')}")


def _watch(run: dict[str, object], *, timeout: float) -> cd_runs.RunState:
    return _cd_lookup(lambda: cd_runs.watch_run(str(run.get("databaseId")), timeout=timeout))


def _next_run(repo: str, run: dict[str, object]) -> dict[str, object] | None:
    """The develop CD run created right after *run*, or ``None``."""
    created = str(run.get("createdAt") or "")
    runs = _cd_lookup(lambda: cd_runs._list_cd_runs(_DEVELOP, repo=repo))
    newer = [r for r in runs if str(r.get("createdAt") or "") > created]
    return min(newer, key=lambda r: str(r.get("createdAt"))) if newer else None


def _judge_ancestor_run(repo: str, head_sha: str) -> None:
    """With no run for *head_sha*, require the latest completed ancestor run green."""
    runs = _cd_lookup(lambda: cd_runs._list_cd_runs(_DEVELOP, repo=repo))
    for run in runs:
        sha = str(run.get("headSha") or "")
        if run.get("status") != "completed" or not sha:
            continue
        # Every ancestor of the local head is in the local object store, so a
        # commit missing from it is not an ancestor.
        if not git.ref_exists(f"{sha}^{{commit}}"):
            continue
        if not _cd_lookup(functools.partial(git.is_ancestor, sha, head_sha)):
            continue
        print(
            f"  No develop CD run registered for {head_sha[:12]}; judging the latest "
            f"completed run on an ancestor ({sha[:12]}): {_run_url(repo, run)}"
        )
        state = _watch(run, timeout=0)
        # The ancestor run is judged as it stands, with no wait, so an
        # unfinished one is reported as such, never as a timeout.
        unfinished = (
            f"The develop CD run on ancestor {sha[:12]} of {head_sha[:12]} is not finished; "
            "result unknown. Re-run vrg-release once it finishes."
        )
        _judge_run(repo, run, state, unfinished=unfinished)
        return
    raise ReleaseError(
        phase="preflight",
        command=_CD_RUNS_COMMAND,
        message=(
            f"No develop CD run registered for {head_sha[:12]}, and none of the recent "
            "completed develop CD runs is on an ancestor of it; refusing to release "
            "without a green develop CD run."
        ),
    )


def _judge_run(
    repo: str, run: dict[str, object], state: cd_runs.RunState, *, unfinished: str
) -> None:
    """Pass a green run, warn on a docs-only failure, refuse anything else.

    *unfinished* is the refusal for a run that did not finish (``timed_out``),
    which only the caller can word: it alone knows whether it waited.
    """
    run_id = str(run.get("databaseId"))
    url = _run_url(repo, run)
    if state == "success":
        print("  Develop CD is green.")
        return
    if state == "failed":
        data = _cd_lookup(
            lambda: github.read_json("run", "view", run_id, "--repo", repo, "--json", "jobs")
        )
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ReleaseError(
                phase="preflight",
                command=f"gh run view {run_id} --json jobs",
                message=f"The develop CD run failed and its jobs could not be read. Run: {url}",
            )
        blocking, deferred = cd_runs.classify_jobs(jobs)
        if deferred and not blocking:
            print(
                f"WARNING: develop CD run {run_id} failed only in deferred publish "
                f"job(s): {', '.join(deferred)}. Releasing anyway; fix them before the "
                f"next release. Run: {url}",
                file=sys.stderr,
            )
            progress.mark_warn(f"develop CD publish deferred: {', '.join(deferred)}")
            return
        failed = ", ".join(blocking) or "no failed job identified"
        message = f"The develop CD run failed ({failed}). Run: {url}"
    elif state == "timed_out":
        message = f"{unfinished} Run: {url}"
    else:
        how = {
            "superseded": "was superseded by a concurrency group, so it published nothing",
            "cancelled": "was cancelled (not by a concurrency supersede)",
        }.get(state, f"ended in state {state!r}")
        message = f"The develop CD run {how}. Run: {url}"
    raise ReleaseError(
        phase="preflight",
        command=f"gh run view {run_id}",
        message=f"{message}\nRefusing to release from a develop whose CD is not green.",
    )
