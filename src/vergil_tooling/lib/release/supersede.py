"""Supersede a release `--resume` cannot finish (spec §4.7, epic vergil-project/.github#1440).

``vrg-release --supersede X.Y.Z`` starts a fresh release that takes the
place of open release ``X.Y.Z``, whose defect is in its tagged commit or
whose CD release job failed. It is eligible only when ``X.Y.Z`` is the open
release and either:

- its tag and GitHub Release exist, and its only stages left before
  ``release-records`` are among the delivery stages, ``cd-publish``,
  ``package-index``, ``update-deps`` and ``install``; or
- it halted at ``confirm-main`` with no tag, its state block recording the
  failed CD release run.

The superseded release's state is read under the identity rule
(:func:`tracking.read_state`) and its facts are checked against GitHub and
git (:func:`resume.verify_facts`) before anything relies on it. A tracking
issue that fails the identity rule can never pass it again, so its release
can never be resumed: its state block is then never read, and the release
is rebuilt from GitHub and git facts alone (:func:`rebuild.rebuild_breached`,
vergil-project/vergil-tooling#4878). The new
release absorbs its issue list (:func:`absorb`), and once the new tracking
issue exists, :func:`close_superseded` closes the old one with a comment
naming the new release. The superseded release posts no records.

An untagged superseded release leaves its release commit on ``main`` without
a tag. The new release's main-ahead check accepts that commit, its
``develop`` VERSION was never bumped, so it takes the next patch unless a
``minor`` or ``major`` bump is given, and its release branch and worktree
are removed when its tracking issue is closed.

The new release takes over the superseded release's release lock rather
than taking a new one (spec §5.3 of epic vergil-project/.github#1664), so
``develop`` stays locked throughout: :func:`judge_superseded_lock` decides
whether it may, and :func:`comment_lock_takeover` records the takeover on
both tracking issues. The lock never names a closed issue: the takeover
clears its tracking issue, and the new release writes its own into the lock
before the superseded release's is closed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github
from vergil_tooling.lib.managed_worktree import remove_worktree, worktree_path
from vergil_tooling.lib.process_errors import command_text, error_text
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.guard import TrackingIssueBreach
from vergil_tooling.lib.release.rebuild import absorb as absorb
from vergil_tooling.lib.release.rebuild import rebuild_breached
from vergil_tooling.lib.release.resume import (
    _found_state,
    _open_release_issues,
    describe_holder,
    verify_facts,
)
from vergil_tooling.lib.release.tracking import read_state
from vergil_tooling.lib.release_lock.model import (
    STALE_AFTER_SECS,
    heartbeat_age_secs,
    heartbeat_fresh,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path

    from vergil_tooling.lib.release.state import IssueEntry, StateBlock
    from vergil_tooling.lib.release_lock.model import LockRecord
    from vergil_tooling.lib.release_lock.refs import LockState

_PHASE = "preflight"

# The tracked stages before release-records, in pipeline order; a test pins
# them to the pipeline's own list.
STAGES_BEFORE_RECORDS = (
    "prepare",
    "merge-release",
    "confirm-main",
    "back-merge-bump",
    "teardown-worktree",
    "confirm-develop",
    "promote",
    "cd-publish",
    "package-index",
    "finalize",
    "update-deps",
    "install",
)
# The stages a tagged release may have left and still be superseded: each
# delivers or installs what the tag already holds.
DELIVERY_STAGES = ("cd-publish", "package-index", "update-deps", "install")
# The stage an untagged release halts at, its CD release job failed.
_CONFIRM_MAIN = "confirm-main"


@dataclass(frozen=True)
class Superseded:
    """The open release a ``--supersede`` run takes the place of.

    *issues* is its issue list, which the new release absorbs: read from
    its state block under the identity rule, or, when its tracking issue
    fails that rule, rebuilt from GitHub and git facts.
    *main_commit* is the untagged release commit it left on ``main`` (its
    release PR's merge commit), or ``None`` when it is tagged.
    """

    version: str
    number: int
    issues: tuple[IssueEntry, ...]
    main_commit: str | None

    @property
    def untagged(self) -> bool:
        """True when the release halted at ``confirm-main`` with no tag."""
        return self.main_commit is not None


def _refuse(version: str, reason: str) -> ReleaseError:
    return ReleaseError(
        phase=_PHASE,
        command=f"vrg-release --supersede {version}",
        message=f"Release {version} cannot be superseded: {reason}",
    )


def check_eligible(state: StateBlock) -> None:
    """Refuse unless the release *state* records may be superseded (spec §4.7).

    Eligible when either its tag is recorded and every stage before
    ``release-records`` that is not done is a delivery stage
    (:data:`DELIVERY_STAGES`), at least one of them, or no tag is recorded,
    its first undone stage is ``confirm-main`` and ``failed_release_run`` is
    set. Anything else raises :class:`ReleaseError` naming what is open.
    """
    version = state.version
    left = [name for name in STAGES_BEFORE_RECORDS if name not in state.done]
    resume = f"Finish it with `vrg-release --resume {version}`."
    if state.tag is not None:
        if not left:
            raise _refuse(
                version,
                "every stage before release-records is done, so nothing is left that a "
                f"defect in its tagged commit could hold back. {resume}",
            )
        blocking = [name for name in left if name not in DELIVERY_STAGES]
        if blocking:
            raise _refuse(
                version,
                f"it is tagged {state.tag}, but stages other than delivery are not done: "
                f"{', '.join(blocking)}. A tagged release is superseded only when its "
                f"stages left are among {', '.join(DELIVERY_STAGES)}. {resume}",
            )
        return
    first = left[0] if left else None
    if first != _CONFIRM_MAIN:
        raise _refuse(
            version,
            f"it has no tag and its first stage not done is {first or 'none'}, and an "
            f"untagged release is superseded only once it halts at {_CONFIRM_MAIN}. {resume}",
        )
    if state.failed_release_run is None:
        raise _refuse(
            version,
            f"it halted at {_CONFIRM_MAIN} with no tag, but its state block records no "
            "failed CD release run, so its CD release job is not known to have failed. "
            f"{resume}",
        )


def load_superseded(repo: str, repo_root: Path, version: str, stages: Sequence[str]) -> Superseded:
    """The open release *version* a ``--supersede`` run takes the place of, checked.

    *version* must be an open ``release: X.Y.Z`` tracking issue's. Its state
    block is read under the identity rule, which locks the issue first, and
    must be eligible (:func:`check_eligible`); its recorded facts are then
    checked against GitHub and git. A tagged release's GitHub Release must
    exist; an untagged one's tag must not exist on origin.

    When the tracking issue fails the identity rule, its state block is not
    read or trusted at all: the release is rebuilt from GitHub and git facts
    (:func:`rebuild.rebuild_breached`), and a tagged one's GitHub Release
    must still exist.
    """
    issues = [issue for issue in _open_release_issues(repo, phase=_PHASE) if issue[0] == version]
    if not issues:
        raise _refuse(version, f"no 'release: {version}' tracking issue is open in {repo}.")
    if len(issues) > 1:
        numbers = ", ".join(f"#{number}" for _, number, _ in issues)
        raise _refuse(
            version,
            f"{len(issues)} open tracking issues are titled 'release: {version}' "
            f"({numbers}); close all but the release's own.",
        )
    _, number, body = issues[0]
    old = ReleaseContext(
        repo=repo,
        version=version,
        repo_root=repo_root,
        version_override=None,
        release_branch=f"release/{version}",
        issue_number=number,
        issue_url=f"https://github.com/{repo}/issues/{number}",
    )
    try:
        current = read_state(old)
    except TrackingIssueBreach as exc:
        rebuilt = rebuild_breached(repo, repo_root, version, number, exc)
        if rebuilt.tag is not None:
            _check_release_exists(repo, version, rebuilt.tag)
        return Superseded(
            version=version,
            number=number,
            issues=rebuilt.issues,
            main_commit=rebuilt.main_commit,
        )
    _found_state(version, number, body, stages)
    check_eligible(current)
    verify_facts(old, current)
    if current.tag is not None:
        _check_release_exists(repo, version, current.tag)
        return Superseded(version=version, number=number, issues=current.issues, main_commit=None)
    _check_untagged(version)
    if old.release_merge_sha is None:
        raise _refuse(version, "its release PR's merge commit could not be confirmed.")
    return Superseded(
        version=version,
        number=number,
        issues=current.issues,
        main_commit=old.release_merge_sha,
    )


def _check_release_exists(repo: str, version: str, tag: str) -> None:
    """Refuse unless *repo*'s GitHub Release of *tag* exists; a failed read refuses."""
    command = f"gh release view {tag} --repo {repo} --json tagName"
    try:
        release = github.read_json("release", "view", tag, "--repo", repo, "--json", "tagName")
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase=_PHASE,
            command=command,
            message=(
                f"Release {version} cannot be superseded: its GitHub Release of {tag} "
                "could not be read, so whether it exists is unknown."
            ),
            detail=error_text(exc),
        ) from exc
    if not isinstance(release, dict) or release.get("tagName") != tag:
        raise _refuse(version, f"GitHub reports no GitHub Release of {tag}: {release!r}.")


def _check_untagged(version: str) -> None:
    """Refuse when tag ``v<version>`` exists on origin; a failed read refuses."""
    tag = f"refs/tags/v{version}"
    command = f"git ls-remote --tags origin {tag}"
    try:
        found = git.read_output("ls-remote", "--tags", "origin", tag)
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase=_PHASE,
            command=command,
            message=(
                f"Release {version} cannot be superseded: origin could not be asked "
                f"whether v{version} is tagged."
            ),
            detail=error_text(exc),
        ) from exc
    if found:
        raise _refuse(
            version,
            f"its state block records no tag, but v{version} exists on origin. Inspect "
            "the tag and the tracking issue before superseding it.",
        )


def judge_superseded_lock(
    repo: str, found: LockState, superseded: Superseded, *, now: datetime
) -> LockRecord | None:
    """The record of the lock *found* on *repo* a ``--supersede`` run takes over, if any.

    The superseded release's own lock (its version and its tracking issue)
    when it is ``broken`` or its heartbeat is stale. That lock ``running``
    with a fresh heartbeat is refused, naming it: the release may still be
    alive. ``None`` for anything else (no lock, a lock that cannot be read,
    another release's): the new release takes a fresh lock, which reclaims
    a stale one or refuses naming its holder.
    """
    rec = found.record
    if rec is None:
        return None
    own_issue = f"{repo}#{superseded.number}"
    if rec.version != superseded.version or rec.tracking_issue != own_issue:
        return None
    if heartbeat_fresh(rec, now=now):
        age = heartbeat_age_secs(rec, now=now)
        raise _refuse(
            superseded.version,
            f"its release lock on {repo} is running with a fresh heartbeat: it is"
            f" {describe_holder(rec)}, its heartbeat {age}s old (stale after"
            f" {STALE_AFTER_SECS}s), so the release may still be alive. Wait for it to"
            " stop and its heartbeat to go stale (see `vrg-release-lock status`), then"
            f" re-run `vrg-release --supersede {superseded.version}`.",
        )
    return rec


def comment_lock_takeover(repo: str, number: int, body: str, *, phase: str = _PHASE) -> None:
    """Post *body*, a record of a lock takeover, on tracking issue *number*; raise on failure."""
    try:
        github.run("issue", "comment", str(number), "--repo", repo, "--body", body)
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase=phase,
            command=f"gh issue comment {number} --repo {repo}",
            message=(
                f"The release lock takeover could not be recorded on tracking issue #{number}."
            ),
            detail=error_text(exc),
        ) from exc


def superseded_issue(repo: str, version: str) -> int | None:
    """The number of the open ``release: <version>`` tracking issue, or None once it is closed."""
    numbers = [number for ver, number, _ in _open_release_issues(repo) if ver == version]
    if len(numbers) > 1:
        raise _refuse(
            version, f"{len(numbers)} open tracking issues are titled 'release: {version}'."
        )
    return numbers[0] if numbers else None


def _git(phase: str, label: str, *args: str) -> str:
    """Run git *args*, turning a failure into a :class:`ReleaseError` of *phase*."""
    try:
        return git.read_output(*args)
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase=phase,
            command=command_text(exc.cmd),
            message=(
                f"A git command failed while removing the {label}'s branch. Fix "
                "the cause git reports below, then resume the release."
            ),
            detail=error_text(exc),
        ) from exc


def remove_release_branch(
    repo_root: Path,
    branch: str,
    *,
    phase: str,
    reason: str = "superseded by a new release",
    label: str = "superseded release",
) -> None:
    """Remove *branch*'s release worktree, its local branch and its branch on origin.

    Each is removed only when it is there, so a re-run after a partial
    removal finishes it. The worktree's removal is logged with *reason*
    (spec §5.8 of epic vergil-project/.github#1664); *label* names the
    release in the output. Supersede and the abandon step of a stray merge
    both remove a release this way. A git failure raises
    :class:`ReleaseError`.
    """
    path = worktree_path(repo_root, branch)
    if path.exists():
        print(f"Removing the {label} worktree {path}.")
        try:
            remove_worktree(path, branch=branch, reason=reason)
        except subprocess.CalledProcessError as exc:
            raise ReleaseError(
                phase=phase,
                command=command_text(exc.cmd),
                message=f"The {label} worktree {path} could not be removed.",
                detail=error_text(exc),
            ) from exc
    if git.ref_exists(f"refs/heads/{branch}"):
        print(f"Deleting the {label} branch '{branch}'.")
        _git(phase, label, "branch", "-D", branch)
    if _git(phase, label, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"):
        print(f"Deleting '{branch}' on origin.")
        _git(phase, label, "push", "origin", "--delete", branch)
    if git.ref_exists(f"refs/remotes/origin/{branch}"):
        _git(phase, label, "branch", "-dr", f"origin/{branch}")


def close_superseded(  # noqa: PLR0913 - the superseded and superseding releases, each named
    repo: str,
    number: int | None,
    new_version: str,
    *,
    superseded_version: str,
    new_issue_url: str,
    untagged: bool,
    repo_root: Path,
    phase: str = "prepare",
) -> None:
    """Close superseded tracking issue *number* with a comment naming release *new_version*.

    Called once the new tracking issue (*new_issue_url*) exists. An
    *untagged* superseded release's worktree and ``release/<version>``
    branch, local and on origin, are removed first, so the closed issue
    means nothing of it is left. *number* is None when the issue is already
    closed: only the removal is then made. A failure raises
    :class:`ReleaseError`.
    """
    if untagged:
        remove_release_branch(repo_root, f"release/{superseded_version}", phase=phase)
    if number is None:
        print(f"The tracking issue of superseded release {superseded_version} is already closed.")
        return
    comment = (
        f"Superseded by release {new_version}: {new_issue_url}\n\n"
        f"Release {superseded_version} posts no `Released:` records. Its issues are "
        f"absorbed into release {new_version}, whose records name that release and say "
        f"it supersedes v{superseded_version}."
    )
    try:
        github.run("issue", "close", str(number), "--repo", repo, "--comment", comment)
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase=phase,
            command=f"gh issue close {number} --repo {repo}",
            message=(
                f"The tracking issue #{number} of superseded release {superseded_version} "
                f"could not be closed. Resume with `vrg-release --resume {new_version}`, "
                "which closes it."
            ),
            detail=error_text(exc),
        ) from exc
    print(f"Closed tracking issue #{number}: release {superseded_version} is superseded.")
