"""Preflight checks for vrg-release."""

from __future__ import annotations

import functools
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import (
    cd_runs,
    config,
    git,
    github,
    host_versions,
    progress,
    trust,
    version,
)
from vergil_tooling.lib.checkout_lock import checkout_lock
from vergil_tooling.lib.managed_worktree import (
    ManagedWorktreeError,
    adopt_worktree,
    create_detached_worktree,
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
from vergil_tooling.lib.release.lock_guard import UnlockGuard
from vergil_tooling.lib.release.resume import (
    _SEARCH_COMMAND,
    BRANCH_STAGES,
    _open_release_issues,
    _trusted_read,
    _trusted_release_issues,
    check_release_version,
    first_undone,
    restore_context,
    verify_facts,
)
from vergil_tooling.lib.release.supersede import absorb, load_superseded
from vergil_tooling.lib.release.tracking import read_state, render_contents
from vergil_tooling.lib.release_lock import reclaim, sweeps
from vergil_tooling.lib.release_lock import refs as lock_refs
from vergil_tooling.lib.release_lock.holder import LockOwner
from vergil_tooling.lib.release_lock.model import (
    LockRecord,
    ReleaseLockError,
    new_token,
    utc_now,
)
from vergil_tooling.lib.self_repo import is_self_repo
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.update_deps.context import DEPS_LABEL
from vergil_tooling.lib.update_deps.pr import FIX_TASK_PREFIX

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vergil_tooling.lib.release.state import IssueEntry
    from vergil_tooling.lib.release.supersede import Superseded

_VERSION_OVERRIDE_FIELDS = ("minor", "major")
# The bump a release superseding an untagged one takes by default (spec §4.7).
_PATCH = "patch"

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
# A dependency-update PR's number, from the URL ``gh pr list`` gives for it.
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)/?$")


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
    stages: Sequence[str] = (),
    supersede: str | None = None,
    guard: UnlockGuard | None = None,
) -> ReleaseContext:
    """Run all preflight checks and return an initialized ReleaseContext.

    Every run first checks the host, the GitHub authentication and the
    config. A fresh start then brings the root checkout's clean ``develop`` up
    to ``origin/develop`` under the checkout lock
    (:func:`_check_branch_and_tree`), so a release is cut from the newest head
    rather than refused when a merge landed since the last pull (spec §6.6 of
    epic vergil-project/.github#1218).

    On *resume* only those first three checks run (:func:`_resume_context`):
    the release it adopts passed every fresh-start check when it began (spec
    §4.3 of epic vergil-project/.github#1440).

    A fresh start refuses when ``origin/main`` has commits ``origin/develop``
    lacks (#3527), before any other fresh-start check.

    A fresh start also refuses while any open PR carries the
    ``dependency-update`` label (spec §5.3, epic vergil-project/.github#381);
    *allow_pending_deps* bypasses that gate, and the bypassed PR URLs are
    recorded on the context so the prepare-stage comment names them. Before
    the gate, a PR whose fix task closed as completed is closed (its update
    landed through the fix PR) and recorded as cleared (#3286); the bypass
    applies only to what still blocks after that.

    A fresh start last requires a green develop CD run for the develop head it
    releases (:func:`check_develop_cd`, spec §6.6 of epic
    vergil-project/.github#1218). It runs after every other check, since it
    may wait on a CD run in progress.

    With *supersede* (``X.Y.Z``), the fresh start takes the place of that
    open release (spec §4.7 of epic vergil-project/.github#1440): its state is
    read under the identity rule and checked (:func:`supersede.load_superseded`),
    the open-release check exempts it, and its issues are absorbed into this
    release's list. When it is untagged, the main-ahead check accepts the
    release commit it left on ``main``, and the release takes its next patch,
    ``X.Y.(Z+1)``, unless *version_override* bumps ``minor`` or ``major``.
    """
    _check_host_prerequisites()
    repo = check_gh_auth()
    _read_and_validate_config(repo_root)
    if resume:
        return _resume_context(
            repo,
            repo_root,
            version_override=version_override,
            resume_version=resume_version,
            resume_issue_number=resume_issue_number,
            stages=stages,
        )
    _check_branch_and_tree(repo_root)
    current_version = _current_version(repo_root)

    superseded = None if supersede is None else load_superseded(repo, repo_root, supersede, stages)
    bump = _release_bump(current_version, version_override, superseded)
    if bump is not None:
        release_version = _compute_release_version(current_version, bump)
    else:
        release_version = current_version

    # The one-release check first: a refusal here locks and disarms nothing.
    # The lock gives the atomic exclusion; this check stays as the second
    # guard, for a release past its unlock whose tracking issue is still open
    # (spec §5.3 step 1.2 of epic vergil-project/.github#1664).
    _check_no_open_release(repo, exempt=None if superseded is None else superseded.number)
    # The pre-checks: a refusal here exits with nothing locked (spec §5.3 step 1.1).
    _head_gates(repo, repo_root, release_version, superseded)
    deps_gate = _check_pending_deps(repo, allow=allow_pending_deps)
    _develop_cd_gate(repo)
    judged = _develop_head()

    # Lock and quiesce, then judge again what is cut (spec §5.3 steps 1.2-1.5).
    # Every exit from here until prepare records the tracking issue in the
    # lock runs the unlock path: the orchestrator's guard spans both stages.
    own_guard = guard is None
    if guard is None:
        guard = UnlockGuard()
    try:
        lock = _lock_and_quiesce(repo, release_version, guard)
        _check_branch_and_tree(repo_root)
        deps_gate = _merge_deps_outcomes(
            deps_gate, _check_pending_deps(repo, allow=allow_pending_deps)
        )
        _rejudge_moved_head(
            repo,
            repo_root,
            judged=judged,
            current_version=current_version,
            release_version=release_version,
            superseded=superseded,
        )

        branch = f"release/{release_version}"
        worktree = _acquire_release_worktree(repo_root, branch, resume=False)
        lock.update(worktree=str(worktree))
        os.chdir(worktree)
        cut, pending, issues = _release_contents(worktree, superseded)
    except BaseException as exc:
        if own_guard and guard.armed:
            guard.unlock(exc)
        raise

    print(f"Preflight passed: {repo} v{release_version} — worktree {worktree}")
    ctx = ReleaseContext(
        repo=repo,
        version=release_version,
        repo_root=repo_root,
        version_override=bump,
        release_branch=branch,
        cut=cut,
        pending_release=pending,
        issues=issues,
        worktree_path=worktree,
        bypassed_deps_prs=deps_gate.bypassed,
        cleared_deps_prs=deps_gate.cleared,
        undeleted_deps_branches=deps_gate.undeleted,
        superseded=superseded,
        lock=lock,
    )
    return ctx


def _current_version(repo_root: Path) -> str:
    """``develop``'s VERSION in *repo_root*; any failure to read it is a :class:`ReleaseError`."""
    try:
        return version.show(repo_root)
    except (
        FileNotFoundError,
        version.VersionSyncError,
        version.CargoVersionError,
        version.VersionFileError,
    ) as exc:
        raise ReleaseError(
            phase="preflight",
            command="version.show",
            message=str(exc),
        ) from exc


def _head_gates(
    repo: str, repo_root: Path, release_version: str, superseded: Superseded | None
) -> None:
    """The gates that judge ``develop``'s head, but for the develop CD gate.

    The main/develop check, the already-tagged check and the package check:
    each reads the synced head (its history, its tags or its ``[package]``
    config), so each runs again on a head that moved before the disarm.
    """
    _check_main_not_ahead_of_develop(accept=None if superseded is None else superseded.main_commit)
    _check_version_not_tagged(release_version)
    _check_packages_unpublished(repo, repo_root, release_version)


def _develop_head() -> str:
    """The root checkout's ``develop`` head, as the sync left it."""
    try:
        return git.read_output("rev-parse", "HEAD")
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse HEAD",
            message="Could not read the develop head the release gates judge.",
            detail=error_text(exc),
        ) from exc


def _rejudge_moved_head(
    repo: str,
    repo_root: Path,
    *,
    judged: str,
    current_version: str,
    release_version: str,
    superseded: Superseded | None,
) -> None:
    """Judge the head the second sync reached again, if it is not the one the gates judged.

    After the disarm ``develop`` cannot move, so whatever this judges is what
    is cut (spec §5.3 step 1.5 of epic vergil-project/.github#1664). An
    unmoved head is cut with no gate run again. A moved one (a PR merged
    between the gates and the disarm) must keep the VERSION the release was
    computed from, then passes the head-judging gates and the develop CD
    gate again, on the new head.
    """
    head = _develop_head()
    if head == judged:
        print(f"develop is still at {head[:12]}, the head the gates judged")
        return
    print(
        f"develop moved from {judged[:12]} to {head[:12]} before the disarm; "
        "judging the new head again"
    )
    moved_version = _current_version(repo_root)
    if moved_version != current_version:
        raise ReleaseError(
            phase="preflight",
            command="version.show",
            message=(
                f"develop moved from {judged[:12]} to {head[:12]} before the disarm, and its "
                f"VERSION changed from {current_version} to {moved_version}, so release "
                f"{release_version} no longer matches it. Find out what changed VERSION, "
                "then re-run vrg-release."
            ),
        )
    _head_gates(repo, repo_root, release_version, superseded)
    _develop_cd_gate(repo)


def _merge_deps_outcomes(first: _DepsGateOutcome, second: _DepsGateOutcome) -> _DepsGateOutcome:
    """The dependency-update gate's outcome over its pre-check and its run after the disarm.

    The PRs closed and the branches left undeleted by either run are kept,
    each once. The PRs bypassed are the second run's: the set it judged can
    no longer change.
    """
    return _DepsGateOutcome(
        cleared=list(dict.fromkeys([*first.cleared, *second.cleared])),
        bypassed=second.bypassed,
        undeleted=list(dict.fromkeys([*first.undeleted, *second.undeleted])),
    )


def _holder_identity() -> str:
    """The GitHub login this release runs as, from ``gh api user``'s JSON."""
    try:
        login = github.read_output("api", "user", "--jq", ".login")
    except Exception as exc:
        raise ReleaseError(
            phase="preflight",
            command="gh api user --jq .login",
            message="Could not read the GitHub login the release lock names as its holder.",
            detail=error_text(exc),
        ) from exc
    if not login:
        raise ReleaseError(
            phase="preflight",
            command="gh api user --jq .login",
            message="gh api user returned no login for the release lock's holder.",
        )
    return login


def _lock_error(what: str, exc: Exception) -> ReleaseError:
    return ReleaseError(
        phase="preflight",
        command=f"release lock on {what}",
        message=str(exc),
    )


def _held_refusal(repo: str) -> ReleaseError:
    """The refusal for a lock another holder has, naming it as read now."""
    try:
        held = lock_refs.read_lock(repo)
    except ReleaseLockError as exc:
        return ReleaseError(
            phase="preflight",
            command=f"take {lock_refs.LOCK_REF}",
            message=(
                f"The release lock on {repo} is held by another release, and reading it to "
                f"name the holder failed: {exc}. Check it with `vrg-release-lock status`."
            ),
        )
    if held.record is not None:
        rec = held.record
        holder = (
            f"held by {rec.holder_identity} on {rec.holder_host} for release {rec.version}"
            f" (stage {rec.stage}, health {rec.health}, tracking issue"
            f" {rec.tracking_issue or 'not yet created'})"
        )
    elif held.sha is not None:
        holder = f"held, but unreadable ({held.error})"
    else:
        holder = "held when this run tried to take it, and released since"
    return ReleaseError(
        phase="preflight",
        command=f"take {lock_refs.LOCK_REF}",
        message=(
            f"Another release holds the release lock on {repo}: it is {holder}. One "
            "release runs at a time; nothing was disarmed. Wait for it to finish (see "
            "`vrg-release-lock status`), then re-run vrg-release."
        ),
    )


def _lock_and_quiesce(repo: str, release_version: str, guard: UnlockGuard) -> LockOwner:
    """Reclaim a stale lock, take the lock, arm *guard* and quiesce ``develop``.

    Spec §5.3 steps 1.2-1.4 and §5.6 of epic vergil-project/.github#1664. A
    lock held by another release, and not stale, refuses naming its holder,
    version and tracking issue, before anything is disarmed. *guard* is armed
    the moment the lock is taken, and told when the disarm sweep starts.
    """
    identity = _holder_identity()
    host = socket.gethostname()
    try:
        reclaimed = reclaim.reclaim_if_stale(repo, actor=f"{identity} on {host} (vrg-release)")
    except ReleaseLockError as exc:
        raise _lock_error(f"{repo}: reclaiming a stale lock", exc) from exc
    if reclaimed is not None:
        print(
            f"Reclaimed a stale release lock on {repo} (was at {reclaimed.old_sha}): "
            f"{reclaimed.reason}"
        )
    now = utc_now()
    record = LockRecord(
        holder_identity=identity,
        holder_host=host,
        version=release_version,
        worktree="",
        tracking_issue="",
        stage="preflight",
        health="running",
        broken_stage="",
        started_at=now,
        heartbeat_at=now,
        token=new_token(),
    )
    try:
        owner = LockOwner.take(repo, record)
    except ReleaseLockError as exc:
        raise _lock_error(f"{repo}: taking the lock", exc) from exc
    if owner is None:
        raise _held_refusal(repo)
    guard.arm(repo, owner)
    print(f"Took the release lock on {repo} for release {release_version}")
    try:
        result = sweeps.quiesce(repo, on_disarm=guard.disarm_started)
    except ReleaseLockError as exc:
        raise _lock_error(f"{repo}: quiescing develop", exc) from exc
    print(
        f"Quiesced develop: disarmed {len(result.disarmed) + len(result.late)} PR(s)"
        + (f", deleted {len(result.expired)} expired arming ref(s)" if result.expired else "")
    )
    return owner


def _release_bump(
    current: str, version_override: str | None, superseded: Superseded | None
) -> str | None:
    """The version bump a fresh start applies on its release branch, or None for none.

    *version_override* (``minor`` or ``major``) when given. A release that
    supersedes an untagged one otherwise takes its next patch: that release
    never reached ``back-merge-bump``, so ``develop``'s VERSION is still
    its version, which must be so (spec §4.7 of epic vergil-project/.github#1440).
    ``develop``'s VERSION is otherwise released as it is.
    """
    if superseded is not None and superseded.untagged and current != superseded.version:
        raise ReleaseError(
            phase="preflight",
            command="version.show",
            message=(
                f"develop's VERSION is {current}, but superseded release "
                f"{superseded.version} halted before its back-merge, so it should still be "
                f"{superseded.version}. Find out what changed it before superseding."
            ),
        )
    if version_override in _VERSION_OVERRIDE_FIELDS:
        return version_override
    if superseded is not None and superseded.untagged:
        return _PATCH
    return None


def _resume_context(
    repo: str,
    repo_root: Path,
    *,
    version_override: str | None,
    resume_version: str | None,
    resume_issue_number: int | None,
    stages: Sequence[str],
) -> ReleaseContext:
    """The context a resume runs with, rebuilt from the tracking issue's state block.

    The state is read under the identity rule (:func:`tracking.read_state`),
    its facts are checked against GitHub and git (:func:`resume.verify_facts`)
    and restored on the context. The release worktree is adopted only when
    the first undone stage is at or before ``merge-release``, the last stage
    that works on ``release/<version>``, and a missing branch is then a
    refusal (#4886). The branch's VERSION is then held to the target version
    the state records (:func:`resume.check_release_version`, #4899): a bump
    the stopped run did not commit is completed by ``prepare``, and any other
    VERSION is refused. Past it, up to ``teardown-worktree``, the branch is not
    needed, and GitHub deletes it once the release PR merges, so
    :func:`_post_merge_worktree` sets up the worktree without it (#4889).
    After ``teardown-worktree`` the run stays in the root checkout, since
    every later stage runs from it and ``finalize`` may already have pruned
    the release branches (spec §4.3 of epic vergil-project/.github#1440).
    """
    if resume_version is None or resume_issue_number is None or not stages:
        raise ReleaseError(
            phase="preflight",
            command="preflight",
            message=(
                "A resume needs the tracking issue's version and number and the "
                "pipeline's stages; the resume lookup did not provide them."
            ),
        )
    branch = f"release/{resume_version}"
    ctx = ReleaseContext(
        repo=repo,
        version=resume_version,
        repo_root=repo_root,
        version_override=version_override,
        release_branch=branch,
        issue_number=resume_issue_number,
        issue_url=f"https://github.com/{repo}/issues/{resume_issue_number}",
    )
    current = read_state(ctx)
    verify_facts(ctx, current)
    restore_context(ctx, current)
    first = first_undone(current, stages)
    on_branch = first is not None and stages.index(first) <= stages.index("merge-release")
    if on_branch:
        ctx.worktree_path = _acquire_release_worktree(repo_root, branch, resume=True)
    elif first is not None and stages.index(first) <= stages.index(BRANCH_STAGES[-1]):
        ctx.worktree_path = _post_merge_worktree(
            repo_root, branch, needed="back-merge-bump" not in current.done
        )
    if ctx.worktree_path is not None:
        os.chdir(ctx.worktree_path)
        where = f"worktree {ctx.worktree_path}"
    else:
        os.chdir(repo_root)
        where = f"root checkout {repo_root}"
    if on_branch:
        check_release_version(ctx, current)
    resume_at = first or "nothing left undone"
    print(f"Preflight passed: {repo} v{resume_version} — resuming at {resume_at} in {where}")
    return ctx


def _deps_pr_number(url: str) -> int:
    """The number of deps PR *url*, from the URL ``gh pr list`` gave for it."""
    match = _PR_NUMBER_RE.search(url)
    if match is None:
        raise ReleaseError(
            phase="preflight",
            command=_PENDING_DEPS_COMMAND,
            message=f"gh pr list returned a PR url with no number: {url!r}",
        )
    return int(match.group(1))


def _trusted_fix_task(repo: str, url: str) -> str | None:
    """The ``Fix task:`` link in the trusted body of deps PR *url*, else ``None``.

    The body is read through :func:`trust.read_issue`, never from the PR
    listing, so a link an untrusted author or editor wrote is never followed
    (GHSA-777j-j57r-92ff); the untrusted body is reported on stderr.
    """
    body = _trusted_read(repo, _deps_pr_number(url)).body
    if body is None:
        return None
    link = _FIX_TASK_RE.search(body)
    return link.group(1) if link else None


def pending_deps_prs(repo: str) -> list[PendingDepsPr]:
    """Open PRs labelled ``dependency-update`` in *repo*, with their fix task if linked.

    The fix task counts only when its link is in a trusted body
    (:func:`_trusted_fix_task`). A lookup that cannot complete raises
    :class:`ReleaseError` — an unknown answer must never read as "no pending
    PRs" and let the release through.
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
            "url,headRefName",
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
        found.append(PendingDepsPr(head=head, url=url, fix_task=_trusted_fix_task(repo, url)))
    return found


def _gh_error_detail(exc: Exception) -> str:
    """A gh failure's message with its stderr and stdout, else the exception text."""
    return error_text(exc)


def _fix_task_ref(url: str) -> tuple[str, str]:
    """The ``(owner/repo, number)`` of fix task *url*, or a :class:`ReleaseError`."""
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
    return issue_repo, number


def _require_trusted_close(url: str) -> None:
    """Refuse unless the last close of fix task *url* was by a trusted actor.

    A close by an identity with no role is not a close for the gate (spec
    §5.2): the release stops before any write, naming who closed it and when,
    and the human decides (GHSA-777j-j57r-92ff). A read failure is a
    :class:`ReleaseError` too.
    """
    issue_repo, number = _fix_task_ref(url)
    command = f"close_judgement {issue_repo}#{number}"
    try:
        judgement = trust.close_judgement(issue_repo, int(number))
        if judgement.role is not None:
            return
        closes = [e for e in trust.read_events(issue_repo, int(number)) if e.kind == "closed"]
        if not closes:
            raise trust.TrustReadError(command, "the fix task has no close event")  # noqa: TRY301
    except (trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=f"Could not read who closed fix task {url}; refusing to release blind.",
            detail=str(exc),
        ) from exc
    login = closes[-1].actor.login
    item = trust.Skipped(
        issue_repo,
        int(number),
        "event",
        None if login is None else trust.normalise_login(login),
        closes[-1].at,
        "close",
        str(judgement.cause),
    )
    error = trust.UntrustedContentError(item, "the fix task's close is untrusted")
    raise ReleaseError(phase="preflight", command=command, message=str(error)) from error


def fix_task_status(url: str) -> tuple[str, str | None]:
    """The fix task issue's ``(state, stateReason)``; the reason is None when unset.

    The issue may live in any repo, so *url* must be a full GitHub issue URL.
    An unparseable URL or a lookup that cannot complete raises
    :class:`ReleaseError` — an unknown fix task never clears a PR.
    """
    issue_repo, number = _fix_task_ref(url)
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


def _owns_its_branch(repo: str, pr: PendingDepsPr) -> bool:
    """True when deps PR *pr* is a same-repo PR with a trusted author on its head branch.

    The gate deletes ``refs/heads/<head>`` in *repo* by the PR's branch name,
    so it first finds the PR among the trust layer's same-repo trusted PRs on
    that branch (:func:`trust.prs_for_branch`, GHSA-r4j2-cwcp-xg56). A fork
    PR's head is in the fork: the branch of that name in *repo* is not its own
    and is never deleted for it. Each PR skipped is reported on stderr,
    metadata only; a read failure raises :class:`ReleaseError`.
    """
    command = f"prs_for_branch {repo} {pr.head}"
    try:
        prs, skipped = trust.prs_for_branch(repo, pr.head, state="open")
    except (trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=(
                f"Could not read the PRs on branch {pr.head} of dependency-update PR "
                f"{pr.url}; refusing to release blind."
            ),
            detail=str(exc),
        ) from exc
    trust.report_skipped(skipped, stream=sys.stderr)
    number = _deps_pr_number(pr.url)
    return any(found.number == number for found in prs)


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

    A PR whose fix task closed as completed, by a trusted actor, is closed
    (#3286) and its head branch deleted (#3332); a failed delete is recorded,
    not fatal. The branch is deleted only for a same-repo PR with a trusted
    author (:func:`_owns_its_branch`). A completed close by an untrusted actor
    stops the release (:func:`_require_trusted_close`). Every fix task and
    branch is looked up before any PR is closed, so a lookup failure or a stop
    leaves nothing changed. What remains open blocks (spec §5.3) unless
    *allow* bypasses it.
    """
    pending = pending_deps_prs(repo)
    landed: list[tuple[PendingDepsPr, bool]] = []
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
            _require_trusted_close(pr.fix_task)
            landed.append((pr, _owns_its_branch(repo, pr)))
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
    for pr, owns_branch in landed:
        _close_landed_deps_pr(pr)
        print(
            f"Closed dependency-update PR {pr.url}: fix task {pr.fix_task} closed as "
            "completed, so the update landed."
        )
        cleared.append(pr.url)
        if not owns_branch:
            print(
                f"Not deleting branch {pr.head}: {pr.url} is not a same-repo PR with a "
                "trusted author, so the branch of that name here is not its head."
            )
            continue
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


def _release_contents(
    worktree: Path, superseded: Superseded | None = None
) -> tuple[str, PendingRelease, tuple[IssueEntry, ...]]:
    """The commit a fresh release branch is cut from, what ``main..<cut>`` holds, and its issues.

    The new branch's worktree is at the ``develop`` commit preflight synced
    to, so its ``HEAD`` is the cut. The pending release and the issue list
    are read pinned to it, never to whatever ``origin/develop`` holds by
    then, and printed as the tracking issue's Contents section will show
    them, before the tracking issue is created. An incomplete lookup refuses
    the release (spec §4.1 of epic vergil-project/.github#1440). A release
    that supersedes another absorbs its issues (spec §4.7).
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
    if superseded is None:
        print(render_contents(pending, issues))
        return cut, pending, issues
    absorbed = tuple(issue.ref for issue in superseded.issues)
    issues = absorb(superseded.issues, issues)
    print(render_contents(pending, issues, supersedes=superseded.version, absorbed=absorbed))
    return cut, pending, issues


def _acquire_release_worktree(repo_root: Path, branch: str, *, resume: bool) -> Path:
    """Create the managed worktree on *branch* off develop, or adopt it on resume.

    All release branch work happens in the worktree, so the root checkout's HEAD
    never moves while the release runs (#1578). A fresh run refuses an existing
    release branch (the lock), unless it is the leftover of an interrupted run
    that recorded nothing, which it adopts (#4367, :func:`_adopt_unrecorded_branch`);
    a resume adopts it — creating the local branch from origin if needed, then
    attaching a worktree (#1612). A resume whose branch exists neither locally
    nor on origin refuses, naming it, rather than cutting a new one from
    develop (#4886). A resume calls this only before ``merge-release`` is
    done, while the branch must still exist; past it, GitHub deletes the
    merged branch, and :func:`_post_merge_worktree` is used instead (#4889).

    Every git failure on the way, on the fresh path as on the resume path,
    surfaces as a :class:`ReleaseError` naming the branch, the worktree and
    the recovery step, never as a raw ``CalledProcessError`` (#4442).
    """
    exists = git.ref_exists(branch) or git.ref_exists(f"origin/{branch}")
    if resume and not exists:
        raise ReleaseError(
            phase="preflight",
            command=f"git rev-parse --verify {branch} origin/{branch}",
            message=(
                f"Release branch '{branch}' is missing both locally and on origin, "
                "so `vrg-release --resume` cannot adopt it, and a resume never cuts "
                "a new one from develop. Find out what removed the branch and "
                "restore it (for example, push it back to origin from a clone that "
                "still has it), then re-run `vrg-release --resume`."
            ),
        )
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


def _post_merge_worktree(repo_root: Path, branch: str, *, needed: bool) -> Path | None:
    """The worktree a resume past ``merge-release`` runs in, never needing *branch* (#4889).

    Once the release PR has merged, no stage reads ``release/<version>``, and
    the fleet's ``delete_branch_on_merge`` has GitHub delete it on origin, so
    a clone that never had it locally has no copy at all. The managed
    worktree path is reused when it is there, whichever branch it is on (an
    earlier run's ``back-merge-bump`` may have switched it to the post
    branch); a path that is not a git worktree of its own is refused, since
    git run there would act on the root checkout. With no worktree, one is
    created detached at ``develop`` only when *needed* (``back-merge-bump``
    is not done: it checks out its post branch in the worktree, never in the
    root checkout). Otherwise the run needs none: it returns None, and
    ``teardown-worktree`` has nothing to remove.
    """
    path = worktree_path(repo_root, branch)
    if path.exists():
        _check_is_worktree(path)
        print(f"Reusing the release worktree {path}.")
        return path
    if not needed:
        return None
    try:
        path = create_detached_worktree(repo_root, branch=branch, base="develop")
    except ManagedWorktreeError as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"git worktree add --detach {path} develop",
            message=str(exc),
            detail=exc.detail,
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="preflight",
            command=command_text(exc.cmd),
            message=(
                f"A git command failed while creating the release worktree {path}, "
                "detached at develop, for back-merge-bump. Fix the cause git reports "
                "below, then re-run `vrg-release --resume`."
            ),
            detail=error_text(exc),
        ) from exc
    print(
        f"Created the release worktree {path}, detached at develop: the release PR "
        f"has merged, so '{branch}' is no longer needed."
    )
    return path


def _check_is_worktree(path: Path) -> None:
    """Refuse *path* unless it is the top level of a git worktree of its own."""
    command = f"git -C {path} rev-parse --show-toplevel"
    try:
        top = git.read_output("-C", str(path), "rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=(
                f"The release worktree path {path} exists, but git cannot read it as "
                "a worktree. Find out what is there, remove it, then re-run "
                "`vrg-release --resume`."
            ),
            detail=error_text(exc),
        ) from exc
    if Path(top).resolve() != path.resolve():
        raise ReleaseError(
            phase="preflight",
            command=command,
            message=(
                f"The release worktree path {path} exists but is not a git worktree: "
                f"git resolves it to {top}. Find out what is there, remove it, then "
                "re-run `vrg-release --resume`."
            ),
        )


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
    if override == _PATCH:
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
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


def _check_main_not_ahead_of_develop(*, accept: str | None = None) -> None:
    """Refuse a fresh release while ``origin/main`` has commits develop lacks (#3527).

    A completed release leaves main contained in develop: back-merge-bump
    branches ``release/post-<version>`` from main and merges it to develop with
    a merge commit. Main is ahead only when a release stalled between
    merge-release and back-merge-bump (as 2.1.6 did at confirm-main), or when
    something landed on main directly. A fresh release on top of that fails
    late, at merge-release; here nothing is cut yet. Develop being ahead of
    main is normal and never refuses. A failed fetch or compare refuses too —
    an unknown answer never reads as "not diverged".

    *accept* is the release commit an untagged release that this one
    supersedes left on main (spec §4.7 of epic vergil-project/.github#1440):
    main may be ahead by that commit and its ancestors, and by nothing else.
    The superseding release's back-merge reconciles it.
    """
    command = "git log origin/develop..origin/main"
    try:
        git.run("fetch", "origin", "main")
        ahead = git.read_output("log", "--format=%h %s", "origin/develop..origin/main")
        if ahead and accept is not None:
            ahead = git.read_output(
                "log", "--format=%h %s", "origin/develop..origin/main", f"^{accept}"
            )
            if not ahead:
                print(
                    f"Main/develop check passed: origin/main is ahead only by the superseded "
                    f"release commit {accept[:12]}, which this release's back-merge reconciles"
                )
                return
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


def _check_no_open_release(repo: str, *, exempt: int | None = None) -> None:
    """Refuse a fresh start while any ``release: X.Y.Z`` tracking issue is open.

    *exempt* is the tracking issue a ``--supersede`` run takes the place of
    (spec §4.7 of epic vergil-project/.github#1440); only it may stay open.

    One release runs at a time (spec §4.7 of epic vergil-project/.github#1440),
    whatever its version. Only a release issue with a trusted body counts
    (:func:`_trusted_release_issues`). Each open release is named with its recovery: the
    ``vrg-release --resume`` that finishes it, or, for an issue with no state
    block (left by older tooling), finishing it by hand and closing the issue.
    """
    issues = _trusted_release_issues(
        [i for i in _open_release_issues(repo, phase="preflight") if i[1] != exempt], repo
    )
    if not issues:
        if exempt is None:
            print("Open-release check passed: no release is in flight")
        else:
            print(f"Open-release check passed: only the superseded release #{exempt} is open")
        return
    # Versions order as semantic versions (2.0.9 before 2.0.10), never as
    # strings (#4877); the issue number breaks a tie.
    by_version = functools.cmp_to_key(host_versions.compare)
    lines = [
        f"- {_open_release_recovery(repo, ver, number, body)}"
        for ver, number, body in sorted(issues, key=lambda i: (by_version(i[0]), i[1]))
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
        state = _watch(repo, run, timeout=max(0.0, deadline - time.monotonic()))
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


def _watch(repo: str, run: dict[str, object], *, timeout: float) -> cd_runs.RunState:
    """Wait for *run* to complete, up to *timeout*; its state.

    A run that could not be read refuses as a read failure naming its cause,
    never as a run still in progress (#5174).
    """
    run_id = str(run.get("databaseId"))
    try:
        return _cd_lookup(lambda: cd_runs.watch_run(run_id, timeout=timeout, repo=repo))
    except cd_runs.CDRunReadError as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"gh run view {run_id} --repo {repo}",
            message=(
                f"{exc.message} Refusing to release without develop's CD result. "
                f"Run: {_run_url(repo, run)}"
            ),
            detail=exc.detail,
        ) from exc


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
        state = _watch(repo, run, timeout=0)
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
