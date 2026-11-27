"""Discover the open tracking issue to resume (vrg-release --resume, #1612).

A fresh release creates the ``release: X.Y.Z`` tracking issue with a stage
checklist and a state block in its body. Resume finds that open issue, validates
its state block against the running tooling's stage list, and reports the
version and number; preflight then reads the state under the identity rule,
verifies its facts and rebuilds the context from it, and the pipeline runs only
the stages the state does not record as done (spec §4.3 of epic
vergil-project/.github#1440).
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Literal, cast

from vergil_tooling.lib import git, github, host_versions, trust, version
from vergil_tooling.lib.release import state
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release_lock.model import (
    STALE_AFTER_SECS,
    heartbeat_age_secs,
    heartbeat_fresh,
)
from vergil_tooling.lib.release_lock.refs import LOCK_REF
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from vergil_tooling.lib.release.context import ReleaseContext
    from vergil_tooling.lib.release.state import StateBlock
    from vergil_tooling.lib.release_lock.model import LockRecord
    from vergil_tooling.lib.release_lock.refs import LockState

_TITLE_RE = re.compile(r"^release: (\d+\.\d+\.\d+)$")
# Upper bound on the open issues the release search lists. Reaching it is a
# refusal: an in-flight release issue could lie beyond it (#4038).
_SEARCH_LIST_LIMIT = 100
_SEARCH_COMMAND = "gh issue list --search 'release: in:title' --state open"
_SHORT = 12

# The stages that work on the release branches, in its worktree. A resume
# runs in the worktree only when its first undone stage is at or before the
# last of them (spec §4.3 of epic vergil-project/.github#1440); past
# merge-release it does so without release/<version>, which GitHub deletes
# once the release PR merges (#4889).
BRANCH_STAGES = ("prepare", "merge-release", "back-merge-bump", "teardown-worktree")
# The stages that run on every run and are never recorded done.
ALWAYS_RUN = ("audit", "preflight", "publish-status")
# The stage that deletes the release lock (spec §5.3 step 2 of epic
# vergil-project/.github#1664). A resume before it holds the lock; one at it
# deletes what is left of its own; one past it reads no lock.
UNLOCK_STAGE = "unlock"
# The stage before which a resume that re-takes the lock checks for stray merges.
MERGE_STAGE = "merge-release"
#: What a resume before ``unlock`` does with the lock it finds: adopt its own, or re-take it.
ResumeLock = Literal["adopt", "retake"]
_BREAK = 'vrg-release-lock break --reason "<why>"'


def _open_release_issues(repo: str, *, phase: str = "resume") -> list[tuple[str, int, str]]:
    """Return ``(version, number, body)`` for each open ``release: X.Y.Z`` issue.

    Shared by ``--resume``'s lookup and a fresh start's one-release-at-a-time
    check (spec §4.7 of epic vergil-project/.github#1440); *phase* names the
    caller in a refusal. Raises :class:`ReleaseError` when the search fills the
    listing limit, since the list may then be truncated, and when the response
    is not a list of issue objects, which is never read as "no open release".
    """
    results = github.read_json(
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        "release: in:title",
        "--state",
        "open",
        "--limit",
        str(_SEARCH_LIST_LIMIT),
        "--json",
        "number,title,body",
    )
    if not isinstance(results, list):
        raise ReleaseError(
            phase=phase,
            command=_SEARCH_COMMAND,
            message=f"The release-issue search returned {type(results).__name__}, not a list.",
        )
    if len(results) >= _SEARCH_LIST_LIMIT:
        raise ReleaseError(
            phase=phase,
            command=_SEARCH_COMMAND,
            message=(
                f"The release-issue search matched {_SEARCH_LIST_LIMIT}+ open issues; "
                "refusing to judge the open releases from a possibly truncated list."
            ),
        )
    issues: list[tuple[str, int, str]] = []
    for raw in results:
        if not isinstance(raw, dict):
            raise ReleaseError(
                phase=phase,
                command=_SEARCH_COMMAND,
                message=f"The release-issue search returned a non-object item: {raw!r}.",
            )
        item = cast("dict[str, object]", raw)
        match = _TITLE_RE.match(str(item.get("title", "")))
        if match:
            issues.append((match.group(1), int(str(item["number"])), str(item.get("body", ""))))
    return issues


def _trusted_read(repo: str, number: int) -> trust.IssueContent:
    """:func:`trust.read_issue` of *repo*#*number*, its skips reported on stderr.

    A read that cannot complete raises :class:`ReleaseError`: who wrote the
    content is never guessed.
    """
    try:
        content = trust.read_issue(repo, number)
    except (trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"read_issue {repo}#{number}",
            message=f"Could not read who wrote {repo}#{number}; refusing to release blind.",
            detail=str(exc),
        ) from exc
    trust.report_skipped(content.skipped, stream=sys.stderr)
    return content


def _trusted_release_issues(
    issues: Sequence[tuple[str, int, str]], repo: str
) -> list[tuple[str, int, str]]:
    """The *issues* whose body is trusted, each with that trusted body.

    A ``release:`` issue with an untrusted body (an outsider's, or one an
    untrusted identity edited) is no release in flight: it is reported on
    stderr, metadata only, and never blocks the start (GHSA-777j-j57r-92ff).
    """
    trusted: list[tuple[str, int, str]] = []
    for ver, number, _listed_body in issues:
        body = _trusted_read(repo, number).body
        if body is not None:
            trusted.append((ver, number, body))
    return trusted


def _found_state(version: str, number: int, body: str, stages: Sequence[str]) -> StateBlock:
    """The state block in open tracking issue #*number*'s *body*, checked against *stages*.

    A block that is missing or malformed, names a version other than the
    issue's, or records a stage the running tooling does not have is a
    refusal naming the block, never a fallback to the checklist (spec §4.2-§4.3
    of epic vergil-project/.github#1440). This read only finds the target:
    preflight reads the block again under the identity rule before any stage
    relies on it.
    """
    command = f"gh issue view {number} --json body"
    try:
        found = state.parse_state(body)
    except state.StateBlockError as exc:
        raise ReleaseError(
            phase="resume",
            command=command,
            message=f"Tracking issue #{number}'s release state block: {exc}",
        ) from exc
    if found.version != version:
        raise ReleaseError(
            phase="resume",
            command=command,
            message=(
                f"Tracking issue #{number}'s release state block names version "
                f"{found.version!r}, but the issue is 'release: {version}'."
            ),
        )
    unknown = [name for name in found.done if name not in stages]
    if unknown:
        raise ReleaseError(
            phase="resume",
            command=command,
            message=(
                f"Tracking issue #{number}'s release state block records stage(s) "
                f"{', '.join(unknown)} as done, which this vrg-release does not have; "
                "it was written by another tooling version. Resume with that version."
            ),
        )
    return found


def find_resume_target(
    repo: str, stages: Sequence[str], *, version: str | None = None
) -> tuple[str, int]:
    """Return ``(version, issue_number)`` of the in-flight release to resume.

    Also validates the issue's state block (:func:`_found_state`). Raises
    ``ReleaseError`` when there is no in-flight release, when the choice is
    ambiguous (and *version* did not disambiguate), or when the state block is
    missing, malformed or written by a different tooling version.
    """
    issues = _open_release_issues(repo)
    if version is not None:
        issues = [issue for issue in issues if issue[0] == version]
    if not issues:
        msg = (
            f"No open release issue for {version}."
            if version
            else "No in-flight release to resume."
        )
        raise ReleaseError(phase="resume", command="gh issue list", message=msg)
    if len(issues) > 1:
        # Semantic-version order, never string order (#4877).
        versions = (issue[0] for issue in issues)
        names = ", ".join(sorted(versions, key=functools.cmp_to_key(host_versions.compare)))
        raise ReleaseError(
            phase="resume",
            command="gh issue list",
            message=(
                f"Multiple in-flight releases ({names}); name one with vrg-release --resume X.Y.Z."
            ),
        )
    found_version, number, body = issues[0]
    _found_state(found_version, number, body, stages)
    return found_version, number


def first_undone(current: StateBlock, stages: Sequence[str]) -> str | None:
    """The first of *stages* that *current* does not record as done, or None when all are.

    The :data:`ALWAYS_RUN` stages run on every run and are never recorded
    done, so they are ignored.
    """
    return next(
        (name for name in stages if name not in ALWAYS_RUN and name not in current.done),
        None,
    )


def restore_context(ctx: ReleaseContext, current: StateBlock) -> None:
    """Rebuild *ctx*'s recorded facts from *current*, for the stages a resume skips.

    Only what the state block holds is restored; :func:`verify_facts` adds
    the merge commits it confirms against GitHub.
    """
    ctx.release_pr_url = current.release_pr
    ctx.bump_pr_url = current.bump_pr
    ctx.tag = current.tag
    ctx.tag_commit = current.tag_commit
    if current.tag is not None:
        ctx.release_url = f"https://github.com/{ctx.repo}/releases/tag/{current.tag}"
    if current.main_cd_run is not None:
        ctx.cd_run_id = current.main_cd_run
        ctx.cd_run_url = _run_url(ctx.repo, current.main_cd_run)
    if current.develop_cd_run is not None:
        ctx.develop_cd_run_id = current.develop_cd_run
        ctx.develop_cd_run_url = _run_url(ctx.repo, current.develop_cd_run)


def _branch_version(ctx: ReleaseContext, *, committed: bool) -> str:
    """The release worktree's VERSION, as committed at ``HEAD`` or as in the working tree."""
    where = "HEAD:VERSION" if committed else "VERSION"
    try:
        return version.show(ctx.work_root, ref="HEAD" if committed else None)
    except (
        FileNotFoundError,
        version.VersionSyncError,
        version.CargoVersionError,
        version.VersionFileError,
        subprocess.CalledProcessError,
    ) as exc:
        raise ReleaseError(
            phase="resume",
            command=f"version.show {where}",
            message=(
                f"Cannot read the VERSION of '{ctx.release_branch}' in its worktree "
                f"{ctx.work_root}: {exc}"
            ),
        ) from exc


def check_release_version(ctx: ReleaseContext, current: StateBlock) -> None:
    """Hold a resume on the release branch to the target version the state block records (#4899).

    Run in the release worktree, before ``merge-release`` is done. The
    branch's committed VERSION must be ``current.version``. When it is the
    VERSION that ``current.version_bump`` moves to that target and
    ``prepare`` is not done, the stopped run never committed the bump:
    ``ctx.version_override`` is set to it, so ``prepare`` completes it. Any
    other VERSION, and a working tree whose VERSION differs from the
    committed one, is a refusal naming both versions: a resume never
    continues at a version other than the target.
    """
    target = current.version
    branch = ctx.release_branch
    committed = _branch_version(ctx, committed=True)
    working = _branch_version(ctx, committed=False)
    if working != committed:
        raise ReleaseError(
            phase="resume",
            command="git status --porcelain",
            message=(
                f"The worktree of '{branch}' has VERSION {working} uncommitted over the "
                f"committed {committed}; the release's target version is {target}. "
                "Discard the uncommitted version change (git checkout) and resume, so "
                "prepare makes the bump in full."
            ),
        )
    if committed == target:
        return
    bump = current.version_bump
    if (
        bump is not None
        and "prepare" not in current.done
        and version.next_version(committed, bump) == target
    ):
        ctx.version_override = bump
        print(
            f"'{branch}' is still at VERSION {committed}: prepare completes its {bump} "
            f"bump to the target version {target}."
        )
        return
    recorded = f"a {bump} bump" if bump is not None else "no bump"
    raise ReleaseError(
        phase="resume",
        command="version.show",
        message=(
            f"'{branch}' has VERSION {committed}, but the release state records the "
            f"target version {target} ({recorded}). A resume never continues at another "
            "version. Find out what changed the branch's VERSION before resuming."
        ),
    )


def _run_url(repo: str, run_id: str) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}"


def _fact_error(fact: str, command: str, message: str) -> ReleaseError:
    return ReleaseError(
        phase="resume",
        command=command,
        message=(
            f"The release state's {fact} does not hold: {message} Inspect the tracking "
            "issue and the release before resuming."
        ),
    )


def _merged_commit(fact: str, pr: str | None) -> str:
    """The merge commit of recorded PR *pr*, refusing unless it is recorded and merged."""
    command = f"gh pr view {pr} --json state,mergeCommit"
    if pr is None:
        raise _fact_error(fact, command, "the stage that merges it is done, but no PR is recorded.")
    pr_state = github.pr_state(pr)
    if pr_state != "MERGED":
        raise _fact_error(fact, command, f"{pr} is {pr_state}, not MERGED.")
    sha = github.merge_commit_sha(pr)
    if not sha:
        raise _fact_error(fact, command, f"GitHub reports no merge commit for {pr}.")
    return sha


def _check_run_head(fact: str, repo: str, run_id: str, expected: str | None, what: str) -> None:
    """Refuse unless CD run *run_id*'s head SHA is *expected*, the merge commit of *what*."""
    command = f"gh run view {run_id} --repo {repo} --json headSha"
    if expected is None:
        raise _fact_error(fact, command, f"run {run_id} is recorded, but {what} is not merged.")
    head = github.read_output(
        "run", "view", run_id, "--repo", repo, "--json", "headSha", "--jq", ".headSha"
    )
    if head != expected:
        raise _fact_error(
            fact,
            command,
            f"run {run_id} is for {head[:_SHORT]}, not {what}'s merge commit {expected[:_SHORT]}.",
        )


def _check_tag(tag: str, tag_commit: str | None, release_merge: str | None) -> None:
    """Refuse unless *tag* peels to *tag_commit*, the release merge or a freeze commit on it.

    By ``promote``'s merged-work rule, the tag is on the release PR's merge
    commit or on a single-parent freeze commit whose parent it is.
    """
    command = f"git rev-parse {tag}^{{commit}}"
    if tag_commit is None:
        raise _fact_error("tag", command, f"{tag} is recorded with no tag_commit.")
    if release_merge is None:
        raise _fact_error("tag", command, f"{tag} is recorded, but the release PR is not merged.")
    git.run("fetch", "--tags", "--force", "origin")
    if not git.ref_exists(f"refs/tags/{tag}"):
        raise _fact_error("tag", command, f"{tag} does not exist on origin.")
    peeled = git.read_output("rev-parse", f"{tag}^{{commit}}")
    if peeled != tag_commit:
        raise _fact_error(
            "tag",
            command,
            f"{tag} peels to {peeled[:_SHORT]}, not the recorded {tag_commit[:_SHORT]}.",
        )
    if tag_commit == release_merge:
        return
    parents = git.read_output("rev-list", "--parents", "-n", "1", tag_commit).split()[1:]
    if parents != [release_merge]:
        raise _fact_error(
            "tag",
            command,
            f"{tag} ({tag_commit[:_SHORT]}) is neither the release PR's merge commit "
            f"{release_merge[:_SHORT]} nor a single-parent commit on top of it.",
        )


def verify_facts(ctx: ReleaseContext, current: StateBlock) -> None:
    """Confirm *current*'s recorded facts against GitHub and git before a resume relies on them.

    The release PR is merged once ``merge-release`` is done, and the
    back-merge PR once ``back-merge-bump`` is; ``main_cd_run``'s head SHA is
    the release PR's merge commit; the tag peels to ``tag_commit``, which is
    that merge commit or a single-parent freeze commit on top of it; and
    ``develop_cd_run``'s head SHA is the back-merge PR's merge commit. A
    mismatch raises :class:`ReleaseError` naming the fact (spec §4.3 of epic
    vergil-project/.github#1440). The confirmed merge commits are set on *ctx*,
    for the stages after them (``confirm-develop`` waits on the back-merge's).
    """
    release_merge: str | None = None
    if "merge-release" in current.done:
        release_merge = _merged_commit("release_pr", current.release_pr)
        ctx.release_merge_sha = release_merge
    bump_merge: str | None = None
    if "back-merge-bump" in current.done:
        bump_merge = _merged_commit("bump_pr", current.bump_pr)
        ctx.bump_merge_sha = bump_merge
    if current.main_cd_run is not None:
        _check_run_head(
            "main_cd_run", ctx.repo, current.main_cd_run, release_merge, "the release PR"
        )
    if current.tag is not None:
        _check_tag(current.tag, current.tag_commit, release_merge)
    if current.develop_cd_run is not None:
        _check_run_head(
            "develop_cd_run", ctx.repo, current.develop_cd_run, bump_merge, "the back-merge PR"
        )


# --- the release lock on a resume (spec §5.3 and §5.6 of epic vergil-project/.github#1664) ---


def holds_lock_at(first: str | None, stages: Sequence[str]) -> bool:
    """Whether a resume whose first undone stage is *first* runs before ``unlock``.

    Such a resume holds the release lock: it adopts its own or re-takes it.
    One at ``unlock`` deletes what is left of its own in that stage, and one
    past it (or with nothing left) takes, adopts and reads no lock.
    """
    return first is not None and stages.index(first) < stages.index(UNLOCK_STAGE)


def describe_holder(rec: LockRecord) -> str:
    """Who holds the lock *rec* and for what, for a refusal that names it."""
    issue = rec.tracking_issue or "none recorded"
    return (
        f"release {rec.version}, held by {rec.holder_identity} on {rec.holder_host}"
        f" (stage {rec.stage}, health {rec.health}, tracking issue {issue})"
    )


def is_own_lock(rec: LockRecord, *, identity: str, tracking_issue: str) -> bool:
    """Whether *rec* is this release's lock: its identity and its tracking issue.

    The host is recorded for information only, so a resume on another
    machine finds its lock its own too (spec §5.3).
    """
    return rec.holder_identity == identity and rec.tracking_issue == tracking_issue


def _lock_refusal(message: str) -> ReleaseError:
    return ReleaseError(phase="preflight", command=f"read {LOCK_REF}", message=message)


def judge_resume_lock(
    repo: str,
    found: LockState,
    *,
    identity: str,
    tracking_issue: str,
    version: str,
    now: datetime,
) -> ResumeLock:
    """What a resume before ``unlock`` does with the lock *found* on *repo*, or a refusal.

    No lock: ``retake`` (a break or a reclaim removed it). This release's
    lock (:func:`is_own_lock`) that is ``broken`` or whose heartbeat is
    stale: ``adopt``. Its own lock ``running`` with a fresh heartbeat is
    refused, naming its host and heartbeat age: another run of the release
    may be alive. A lock that cannot be read, or that another release holds,
    is refused naming it. A refusal raises :class:`ReleaseError` and writes
    nothing.
    """
    if not found.held:
        return "retake"
    rec = found.record
    if rec is None:
        raise _lock_refusal(
            f"The release lock on {repo} is held but cannot be read ({found.error}), so"
            f" whether it is release {version}'s own cannot be told, and a resume does"
            f" not adopt it. Check it with `vrg-release-lock status`; once no release run"
            f" is alive, remove it with `{_BREAK}`, then re-run"
            f" `vrg-release --resume {version}`."
        )
    if not is_own_lock(rec, identity=identity, tracking_issue=tracking_issue):
        raise _lock_refusal(
            f"The release lock on {repo} is held by another release: {describe_holder(rec)}."
            f" This resume of release {version} ({identity}, tracking issue"
            f" {tracking_issue}) does not adopt it. Finish that release first (see"
            f" `vrg-release-lock status`), or once no run of it is alive remove its lock"
            f" with `{_BREAK}`."
        )
    if heartbeat_fresh(rec, now=now):
        age = heartbeat_age_secs(rec, now=now)
        raise _lock_refusal(
            f"The release lock on {repo} is release {version}'s own, and it is running on"
            f" {rec.holder_host}: its heartbeat is {age}s old (stale after"
            f" {STALE_AFTER_SECS}s). Another run of this release may still be alive, and"
            " two runs of one release never both go on. Wait for that run to stop and its"
            f" heartbeat to go stale, or once no run of it is alive remove the lock with"
            f" `{_BREAK}`; then re-run `vrg-release --resume {version}`."
        )
    return "adopt"
