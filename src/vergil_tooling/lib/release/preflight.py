"""Preflight checks for vrg-release."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import config, git, github, version
from vergil_tooling.lib.managed_worktree import (
    ManagedWorktreeError,
    adopt_worktree,
    create_worktree,
)
from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import published
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import find_existing_tracking_issue
from vergil_tooling.lib.update_deps.context import DEPS_LABEL
from vergil_tooling.lib.update_deps.pr import FIX_TASK_PREFIX

if TYPE_CHECKING:
    from pathlib import Path

_VERSION_OVERRIDE_FIELDS = ("minor", "major")

# The "Fix task: <url>" line vrg-update-deps writes into a failed PR's body.
_FIX_TASK_RE = re.compile(rf"^{re.escape(FIX_TASK_PREFIX)}(\S+)\s*$", re.MULTILINE)
_PENDING_DEPS_COMMAND = f"gh pr list --state open --label {DEPS_LABEL}"
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

    On *resume* (#1612), the version comes from the adopted tracking issue
    (*resume_version*) rather than the develop VERSION file (which may already
    be bumped); the checks that assume a fresh start are skipped — the version
    is expected to be tagged and the tracking issue is expected to exist — an
    existing release branch is adopted rather than refused, and the adopted
    issue (*resume_issue_number*) is pre-set on the context.

    A fresh start also refuses while any open PR carries the
    ``dependency-update`` label (spec §5.3, epic vergil-project/.github#381);
    *allow_pending_deps* bypasses that gate, and the bypassed PR URLs are
    recorded on the context so the prepare-stage comment names them. Before
    the gate, a PR whose fix task closed as completed is closed (its update
    landed through the fix PR) and recorded as cleared (#3286); the bypass
    applies only to what still blocks after that. A resume skips the gate —
    the release it adopts already passed it.
    """
    _check_host_prerequisites()
    repo = check_gh_auth()
    _read_and_validate_config(repo_root)
    _check_branch_and_tree()

    if resume and resume_version is not None:
        release_version = resume_version
    else:
        try:
            current_version = version.show(repo_root)
        except (FileNotFoundError, version.VersionSyncError) as exc:
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
        _check_version_not_tagged(release_version)
        _check_no_existing_tracking_issue(repo, release_version)
        _check_packages_unpublished(repo, repo_root, release_version)
        deps_gate = _check_pending_deps(repo, allow=allow_pending_deps)

    branch = f"release/{release_version}"
    worktree = _acquire_release_worktree(repo_root, branch, resume=resume)
    os.chdir(worktree)

    print(f"Preflight passed: {repo} v{release_version} — worktree {worktree}")
    ctx = ReleaseContext(
        repo=repo,
        version=release_version,
        repo_root=repo_root,
        version_override=version_override,
        release_branch=branch,
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
            "100",
        )
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=_PENDING_DEPS_COMMAND,
            message="Could not list open dependency-update PRs; refusing to release blind.",
            detail=str(exc),
        ) from exc
    if not isinstance(raw, list):
        raise ReleaseError(
            phase="preflight",
            command=_PENDING_DEPS_COMMAND,
            message=f"gh pr list returned {type(raw).__name__}, expected a list.",
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
    """A gh failure's stderr when it has one, else the exception text."""
    stderr = getattr(exc, "stderr", None)
    return f"{exc}\n{stderr}" if stderr else str(exc)


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


def _acquire_release_worktree(repo_root: Path, branch: str, *, resume: bool) -> Path:
    """Create the managed worktree on *branch* off develop, or adopt it on resume.

    All release branch work happens in the worktree, so the root checkout's HEAD
    never moves while the release runs (#1578). A fresh run refuses an existing
    release branch (the lock); a resume adopts it — creating the local branch
    from origin if needed, then attaching a worktree (#1612).
    """
    exists = git.ref_exists(branch) or git.ref_exists(f"origin/{branch}")
    if exists and not resume:
        raise ReleaseError(
            phase="preflight",
            command=f"git rev-parse {branch}",
            message=f"Release branch '{branch}' already exists.",
        )
    try:
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
        ) from exc


def _compute_release_version(current: str, override: str) -> str:
    """Compute the target release version without modifying any files."""
    parts = current.split(".")
    if override == "minor":
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{int(parts[0]) + 1}.0.0"


def _check_host_prerequisites() -> None:
    if shutil.which("git-cliff") is None:
        raise ReleaseError(
            phase="preflight",
            command="which git-cliff",
            message="git-cliff is not on PATH. Install it before running vrg-release.",
        )


def run_audit() -> None:
    """Standalone audit stage: resolve the repo and audit its GitHub config."""
    audit_repo_config(check_gh_auth())


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
            detail=str(exc),
        ) from exc


def _read_and_validate_config(repo_root: Path) -> None:
    config.read_config(repo_root)


def _check_branch_and_tree() -> None:
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
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha != remote_sha:
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse HEAD vs origin/develop",
            message=(
                f"Local develop ({local_sha[:8]}) does not match "
                f"origin/develop ({remote_sha[:8]}). Pull latest first."
            ),
        )


def audit_repo_config(repo: str) -> None:
    result = subprocess.run(  # noqa: S603
        ("vrg-github-repo-config", "audit", "--repo", repo),  # noqa: S607
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
        command=f"vrg-github-repo-config audit --repo {repo}",
        message=message,
        detail=result.stdout + result.stderr,
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
    files at the same identity — which the shared package index refuses, failing
    every repo's index publish until the duplicate assets are removed by hand.

    This is the earliest point that can know: the full package versions follow
    from ``[package]`` and the release version alone, and nothing is branched,
    tagged or published yet. It runs before the dependency-update gate, which
    may close PRs, so a refusal here changes nothing. ``vrg-package
    check-unpublished`` runs the same check in the CD packaging path as the
    backstop. Any failure to read the published state refuses; it never skips.
    """
    try:
        cfg = config.read_config(repo_root)
        identities = published.check_release(repo_root, cfg.package, repo, ver)
    except (PackageError, config.ConfigError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"vrg-package check-unpublished --repo {repo} --version {ver}",
            message=str(exc),
        ) from exc
    if identities:
        print(f"Package check passed: {len(identities)} package(s) not yet published by {repo}")


def _check_no_existing_tracking_issue(repo: str, ver: str) -> None:
    existing = find_existing_tracking_issue(repo, ver)
    if existing is not None:
        raise ReleaseError(
            phase="preflight",
            command=f"gh issue list --search 'release: {ver}'",
            message=(
                f"A tracking issue already exists for version {ver}: {existing}\n"
                f"Close the stale issue or investigate before re-running."
            ),
        )
