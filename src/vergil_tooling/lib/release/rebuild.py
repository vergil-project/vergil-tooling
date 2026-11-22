"""Rebuild a breached release from GitHub and git facts (vergil-project/vergil-tooling#4878).

A tracking issue that a non-human identity authored, edited, closed or
reopened cannot be cleaned up so that it passes the identity rule again:
GitHub keeps the editor of a body edit even once its revision is deleted,
and keeps the issue's close and reopen events. So ``vrg-release --resume``
cannot finish such a release, and its state block is never trusted again
(spec §4.2 of epic vergil-project/.github#1440).

``vrg-release --supersede X.Y.Z`` replaces it without reading that state
block at all. :func:`rebuild_breached` reads instead what GitHub and git
record about release ``X.Y.Z``:

- its release PR, ``release/X.Y.Z`` into ``main``, which must be merged;
  one that never merged left nothing on ``main``, so there is nothing to
  supersede;
- whether ``vX.Y.Z`` is tagged on ``origin``. A tagged release's tag must be
  on the release PR's merge commit, or on a single-parent freeze commit on
  it, and its back-merge PR, ``release/post-X.Y.Z`` into ``develop``, must
  be merged. An untagged release's ``main`` CD runs for that merge commit
  must all have completed, with no release job that succeeded;
- its issue list: the issues the ``feature/`` PRs of its release PR's range
  close (:func:`pending_release.gather_issue_list`), and, when it superseded
  a release itself, that release's issue list, which it absorbed. That
  release is found by the closing comment a human login posted on its
  tracking issue, and its state is read under the identity rule, or rebuilt
  the same way when its own issue is breached.

The superseding release's own fresh-start checks then judge the rest, as
for any supersede. Every read fails loudly: a lookup that fails or returns
something unexpected refuses the supersede, never passes as "no such fact".
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import git, github
from vergil_tooling.lib.cd_runs import is_release_job
from vergil_tooling.lib.pending_release import PendingReleaseError, gather_issue_list
from vergil_tooling.lib.process_errors import command_text, error_text
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.guard import TrackingIssueBreach
from vergil_tooling.lib.release.state import IssueEntry
from vergil_tooling.lib.release.tracking import _human_logins, read_state
from vergil_tooling.lib.trusted_authors import bare_login

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_PHASE = "preflight"
_CD_WORKFLOW = "cd.yml"
_TITLE_RE = re.compile(r"^release: (\d+\.\d+\.\d+)$")
# Upper bounds on the lists read here; reaching one refuses, since the list
# may then be truncated.
_PR_LIMIT = 10
_RUN_LIMIT = 50
_ISSUE_LIMIT = 100
_SHORT = 12


@dataclass(frozen=True)
class Rebuilt:
    """What a breached release's facts give a ``--supersede`` run.

    *issues* is its issue list, the issues it absorbed included.
    *main_commit* is its release PR's merge commit when it is untagged,
    the commit it left on ``main``, or ``None`` when it is tagged, and
    *tag* is its tag then.
    """

    issues: tuple[IssueEntry, ...]
    main_commit: str | None
    tag: str | None


@dataclass(frozen=True)
class _MergedPR:
    url: str
    merge_commit: str
    head: str


def absorb(old: Sequence[IssueEntry], new: Sequence[IssueEntry]) -> tuple[IssueEntry, ...]:
    """*old*'s issues, then *new*'s: an issue on both appears once, at its place in *old*.

    Its closing PRs are *old*'s, followed by the new ones it lacks.
    """
    merged: dict[str, list[int]] = {}
    for issue in (*old, *new):
        prs = merged.setdefault(issue.ref, [])
        prs.extend(pr for pr in issue.prs if pr not in prs)
    return tuple(IssueEntry(ref=ref, prs=tuple(prs)) for ref, prs in merged.items())


def _refuse(version: str, reason: str) -> ReleaseError:
    return ReleaseError(
        phase=_PHASE,
        command=f"vrg-release --supersede {version}",
        message=f"Release {version} cannot be superseded: {reason}",
    )


def _lookup_error(version: str, exc: subprocess.CalledProcessError, what: str) -> ReleaseError:
    return ReleaseError(
        phase=_PHASE,
        command=command_text(exc.cmd),
        message=f"Release {version} cannot be superseded: {what} could not be read.",
        detail=error_text(exc),
    )


def _merged_pr(repo: str, version: str, head: str, base: str) -> _MergedPR | None:
    """The merged PR of *head* into *base*, or None; more than one, or a bad answer, refuses."""
    cmd = (
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        head,
        "--base",
        base,
        "--state",
        "merged",
        "--limit",
        str(_PR_LIMIT),
        "--json",
        "url,mergeCommit,headRefOid",
    )
    what = f"the merged PR of {head} into {base}"
    try:
        found = github.read_json(*cmd)
    except subprocess.CalledProcessError as exc:
        raise _lookup_error(version, exc, what) from exc
    if not isinstance(found, list):
        raise _refuse(version, f"{what} is not a list: {found!r}.")
    if not found:
        return None
    if len(found) > 1:
        urls = ", ".join(str(cast("dict[str, object]", pr).get("url")) for pr in found)
        raise _refuse(version, f"{len(found)} PRs of {head} into {base} are merged ({urls}).")
    pr = found[0]
    url = pr.get("url") if isinstance(pr, dict) else None
    merge = pr.get("mergeCommit") if isinstance(pr, dict) else None
    oid = merge.get("oid") if isinstance(merge, dict) else None
    head_oid = pr.get("headRefOid") if isinstance(pr, dict) else None
    if not (isinstance(url, str) and url and isinstance(oid, str) and oid):
        raise _refuse(version, f"{what} has no URL or merge commit: {pr!r}.")
    if not (isinstance(head_oid, str) and head_oid):
        raise _refuse(version, f"{what} has no head commit: {pr!r}.")
    return _MergedPR(url=url, merge_commit=oid, head=head_oid)


def _git(version: str, *args: str) -> str:
    try:
        return git.read_output(*args)
    except subprocess.CalledProcessError as exc:
        raise _lookup_error(version, exc, f"`git {' '.join(args)}`") from exc


def _tag_commit(version: str) -> str | None:
    """The commit ``v<version>`` peels to on ``origin``, or None when it is not tagged."""
    tag = f"v{version}"
    if not _git(version, "ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
        return None
    try:
        git.run("fetch", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    except subprocess.CalledProcessError as exc:
        raise _lookup_error(version, exc, f"tag {tag}") from exc
    return _git(version, "rev-parse", f"{tag}^{{commit}}")


def _check_tag_on_release(version: str, tag_commit: str, release: _MergedPR) -> None:
    """Refuse unless the tag is on the release merge or a single-parent commit on it."""
    merge = release.merge_commit
    if tag_commit == merge:
        return
    parents = _git(version, "rev-list", "--parents", "-n", "1", tag_commit).split()[1:]
    if parents != [merge]:
        raise _refuse(
            version,
            f"v{version} ({tag_commit[:_SHORT]}) is neither the merge commit "
            f"{merge[:_SHORT]} of its release PR {release.url} nor a single-parent "
            "commit on top of it.",
        )


def _check_release_job_failed(repo: str, version: str, merge: str) -> None:
    """Refuse unless every ``main`` CD run for *merge* completed with no release job passing."""
    cmd = (
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        _CD_WORKFLOW,
        "--branch",
        "main",
        "--commit",
        merge,
        "--limit",
        str(_RUN_LIMIT),
        "--json",
        "databaseId,status",
    )
    what = f"the main CD runs for {merge[:_SHORT]}"
    try:
        runs = github.read_json(*cmd)
    except subprocess.CalledProcessError as exc:
        raise _lookup_error(version, exc, what) from exc
    if not isinstance(runs, list) or len(runs) >= _RUN_LIMIT:
        raise _refuse(version, f"{what} could not be listed in full: {runs!r}.")
    if not runs:
        raise _refuse(
            version,
            f"it is untagged, and no main CD run exists for its release PR's merge commit "
            f"{merge[:_SHORT]}, so its CD release job is not known to have failed.",
        )
    for run in runs:
        run_id = run.get("databaseId") if isinstance(run, dict) else None
        status = run.get("status") if isinstance(run, dict) else None
        if not isinstance(run_id, int) or not isinstance(status, str):
            raise _refuse(version, f"{what} include an unreadable run: {run!r}.")
        if status != "completed":
            raise _refuse(
                version,
                f"it is untagged, and main CD run {run_id} for its release PR's merge "
                f"commit is {status}, not completed. Wait for it to finish.",
            )
        _check_no_release_job_passed(repo, version, str(run_id))


def _check_no_release_job_passed(repo: str, version: str, run_id: str) -> None:
    cmd = ("run", "view", run_id, "--repo", repo, "--json", "jobs")
    try:
        view = github.read_json(*cmd)
    except subprocess.CalledProcessError as exc:
        raise _lookup_error(version, exc, f"the jobs of CD run {run_id}") from exc
    jobs = view.get("jobs") if isinstance(view, dict) else None
    if not isinstance(jobs, list):
        raise _refuse(version, f"CD run {run_id}'s jobs could not be read: {view!r}.")
    for job in jobs:
        name = job.get("name") if isinstance(job, dict) else None
        if not isinstance(name, str):
            raise _refuse(version, f"CD run {run_id} has an unreadable job: {job!r}.")
        if is_release_job(name) and cast("dict[str, object]", job).get("conclusion") == "success":
            raise _refuse(
                version,
                f"it is untagged, but CD run {run_id}'s release job {name!r} succeeded. "
                f"Inspect the run and whether v{version} was published before superseding it.",
            )


def _predecessor(repo: str, version: str, number: int) -> tuple[str, int] | None:
    """The ``(version, number)`` of the release *version* superseded, or None when none.

    It is the closed ``release: X.Y.Z`` issue that a human login of
    ``vrg-trusted-authors`` closed with the comment ``prepare`` posts,
    ``Superseded by release <version>: <the issue URL of #number>``. More
    than one such issue refuses.
    """
    marker = f"Superseded by release {version}: https://github.com/{repo}/issues/{number}"
    cmd = (
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "closed",
        "--search",
        f'"Superseded by release {version}" in:comments',
        "--limit",
        str(_ISSUE_LIMIT),
        "--json",
        "number,title,comments",
    )
    what = f"the release that {version} superseded"
    try:
        found = github.read_json(*cmd)
    except subprocess.CalledProcessError as exc:
        raise _lookup_error(version, exc, what) from exc
    if not isinstance(found, list) or len(found) >= _ISSUE_LIMIT:
        raise _refuse(version, f"{what} could not be searched in full: {found!r}.")
    humans = set(_human_logins())
    matches: list[tuple[str, int]] = []
    for item in found:
        if not isinstance(item, dict):
            raise _refuse(version, f"the search for {what} returned {item!r}.")
        title = _TITLE_RE.match(str(item.get("title", "")))
        comments = item.get("comments")
        if title is None:
            continue
        if not isinstance(comments, list):
            raise _refuse(version, f"issue {item.get('number')!r}'s comments are {comments!r}.")
        if any(_closes_as_superseded(comment, humans, marker) for comment in comments):
            matches.append((title.group(1), int(str(item["number"]))))
    if len(matches) > 1:
        numbers = ", ".join(f"#{n}" for _, n in matches)
        raise _refuse(version, f"more than one release says {version} superseded it: {numbers}.")
    return matches[0] if matches else None


def _closes_as_superseded(comment: object, humans: set[str], marker: str) -> bool:
    if not isinstance(comment, dict):
        return False
    comment = cast("dict[str, object]", comment)
    author = comment.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    body = comment.get("body")
    return (
        isinstance(login, str)
        and bare_login(login) in humans
        and isinstance(body, str)
        and body.startswith(marker)
    )


def _issue_list(
    repo: str, repo_root: Path, version: str, number: int, release: _MergedPR
) -> tuple[IssueEntry, ...]:
    """*version*'s issue list from facts: its release PR's range, after any it absorbed."""
    base = _git(version, "rev-parse", f"{release.merge_commit}^1")
    try:
        own = gather_issue_list(base=base, head=release.head)
    except PendingReleaseError as exc:
        raise _refuse(version, f"its issue list could not be rebuilt: {exc}") from exc
    found = _predecessor(repo, version, number)
    if found is None:
        return own
    old_version, old_number = found
    print(f"Release {version} superseded release {old_version} (#{old_number}).")
    old = ReleaseContext(
        repo=repo,
        version=old_version,
        repo_root=repo_root,
        version_override=None,
        release_branch=f"release/{old_version}",
        issue_number=old_number,
        issue_url=f"https://github.com/{repo}/issues/{old_number}",
    )
    try:
        old_issues = read_state(old).issues
    except TrackingIssueBreach as exc:
        _warn(old_version, old_number, exc)
        old_release = _merged_pr(repo, old_version, f"release/{old_version}", "main")
        if old_release is None:
            raise _refuse(
                version,
                f"the release it superseded, {old_version}, has no merged release PR, so "
                "the issues it absorbed cannot be rebuilt.",
            ) from exc
        old_issues = _issue_list(repo, repo_root, old_version, old_number, old_release)
    return absorb(old_issues, own)


def _warn(version: str, number: int, breach: TrackingIssueBreach) -> None:
    print(
        f"WARNING: tracking issue #{number} of release {version} fails the identity rule "
        f"({breach.login} {breach.action}). Its state block is not read; the release is "
        "rebuilt from GitHub and git facts instead.",
        file=sys.stderr,
    )


def rebuild_breached(
    repo: str, repo_root: Path, version: str, number: int, breach: TrackingIssueBreach
) -> Rebuilt:
    """Rebuild breached release *version* (tracking issue #*number*) from its facts.

    *breach* is the identity-rule failure that stopped the read of its state
    block, which is never read here. Refuses with :class:`ReleaseError` when
    its release PR is not merged, or the facts fit neither a tagged release
    whose back-merge is merged nor an untagged one whose CD release job did
    not succeed.
    """
    _warn(version, number, breach)
    release = _merged_pr(repo, version, f"release/{version}", "main")
    if release is None:
        raise _refuse(
            version,
            f"its tracking issue #{number} fails the identity rule, and its release PR "
            f"(release/{version} into main) is not merged, so nothing of it reached "
            f"main. Close its release PR and tracking issue #{number}, delete "
            f"release/{version} locally and on origin, then start a fresh release.",
        )
    try:
        git.run("fetch", "origin", "main")
    except subprocess.CalledProcessError as exc:
        raise _lookup_error(version, exc, "origin/main") from exc
    tag_commit = _tag_commit(version)
    if tag_commit is not None:
        _check_tag_on_release(version, tag_commit, release)
        if _merged_pr(repo, version, f"release/post-{version}", "develop") is None:
            raise _refuse(
                version,
                f"it is tagged v{version}, but its back-merge PR (release/post-{version} "
                "into develop) is not merged, so develop's version was never bumped past "
                "it. Merge the back-merge by hand before superseding it.",
            )
    else:
        _check_release_job_failed(repo, version, release.merge_commit)
    issues = _issue_list(repo, repo_root, version, number, release)
    print(
        f"Rebuilt release {version} from its facts: release PR {release.url}, "
        + (f"tagged v{version}" if tag_commit is not None else "untagged")
        + f", {len(issues)} issue(s)."
    )
    return Rebuilt(
        issues=issues,
        main_commit=None if tag_commit is not None else release.merge_commit,
        tag=None if tag_commit is None else f"v{version}",
    )
