"""In-flight detection for ``vrg-queue adhoc``: what shows an ad-hoc task already under way.

A task is *in flight* when any of these **marks** holds for it, in this order:

- ``open-pr`` — an open PR closes it (``closingIssuesReferences``), or has
  ``feature/<N>-…`` as its head branch;
- ``report-ready`` — a relay ref ``refs/vergil/pr-workflow/<branch>`` on the
  remote whose payload has status ``ready`` and names it (a batch's payload
  names every member, so each one is marked);
- ``branch`` — a ``feature/<N>-…`` branch on the remote;
- ``worktree`` — a ``feature/<N>-…`` branch (every worktree's branch is one)
  in the repo's main clone;
- ``merge-review`` — ``build/.vergil-state/issue-implement/<N>/merge-review``
  in the main clone;
- ``batch`` — the batch marker
  ``build/.vergil-state/issue-implement/<N>/batch`` in the main clone, which
  marks a batch member that has no ``feature/<N>-`` branch of its own.

A branch matches issue *N* only by the exact prefix ``feature/<N>-``, so
``feature/12-x`` is never a mark for issue 1.

The marks rank into a **disposition**: ``handed-off`` when an open PR or a
ready record exists, whatever local marks remain (a reported-ready branch keeps
its worktree until it lands); otherwise ``resume`` when a local worktree,
merge-review directory or batch marker exists; otherwise ``handed-off`` when
only the remote branch exists; with no marks, none.

The relay payloads are read through the Git Data API by the ref's SHA (commit,
tree, then the ``pr-workflow.json`` blob), so no fetch runs and nothing is
written, and are parsed with :meth:`WorkflowState.from_dict`, the one parser.
Every repo with open ad-hoc tasks must have a main clone under the repo roots
for its local marks: :func:`find_clones` refuses one without. Any read that
fails, or returns something malformed, raises :class:`InFlightError` naming the
repo and the source, never a mark silently assumed absent.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from vergil_tooling.lib import epics, git, github, live_branches, pr_body
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

MARKS = ("open-pr", "report-ready", "branch", "worktree", "merge-review", "batch")
Disposition = Literal["handed-off", "resume"]

# The marks that hand a task off, and the local ones that mean its run can resume.
_HANDED_OFF = frozenset({"open-pr", "report-ready"})
_LOCAL = frozenset({"worktree", "merge-review", "batch"})

_FEATURE_PREFIX = "feature/"
_HEADS = "refs/heads/"
_RELAY_PREFIX = "refs/vergil/pr-workflow/"
_RELAY_FILE = "pr-workflow.json"
_READY = "ready"
_MERGE_REVIEW_DIR = "merge-review"
_FEATURE_NUMBER_RE = re.compile(r"^feature/([0-9]+)-")


class InFlightError(RuntimeError):
    """An in-flight source could not be read, or a repo has no main clone to read."""


@dataclass(frozen=True)
class RepoSignals:
    """Everything read once per repo that the marks of its tasks are drawn from."""

    repo: str
    clone: Path
    open_pr_issues: frozenset[int]
    remote_branches: frozenset[str]
    ready_relay_issues: frozenset[int]
    local_branches: frozenset[str]


def _same_repo(a: str, b: str) -> bool:
    """Whether two ``owner/name`` slugs name one GitHub repo (GitHub names ignore case)."""
    return a.casefold() == b.casefold()


def _feature_number(branch: str) -> int | None:
    """The issue number of a ``feature/<N>-…`` branch, or None for any other name."""
    match = _FEATURE_NUMBER_RE.match(branch)
    return int(match.group(1)) if match else None


def _issue_dir(clone: Path, issue: int) -> Path:
    return clone / "build" / ".vergil-state" / "issue-implement" / str(issue)


def find_clones(repos: Iterable[str], roots: Sequence[Path]) -> dict[str, Path]:
    """Map each ``owner/name`` in *repos* to its main clone under *roots*.

    A clone is a repo :func:`live_branches.discover_repos` finds whose
    ``origin`` is that GitHub repo. A repo with no clone, or with two, raises
    :class:`InFlightError` naming the repo and the roots searched; a root that
    does not exist raises :class:`live_branches.RepoSetError`. A discovered
    checkout whose remotes cannot be read raises too, since it might be the
    clone.
    """
    wanted = sorted(set(repos), key=str.casefold)
    if not wanted:
        return {}
    where = ", ".join(str(root) for root in roots)
    found: dict[str, list[Path]] = {}
    for checkout in live_branches.discover_repos(roots):
        try:
            if "origin" not in git.remote_names(checkout):
                continue
            slug = github.github_remote_repo(git.remote_url("origin", checkout))
        except subprocess.CalledProcessError as exc:
            msg = f"cannot read the origin remote of {checkout}: {_detail(exc)}"
            raise InFlightError(msg) from exc
        if slug is not None:
            found.setdefault(slug.casefold(), []).append(checkout)
    clones: dict[str, Path] = {}
    for repo in wanted:
        matches = found.get(repo.casefold(), [])
        if not matches:
            msg = (
                f"{repo} has open ad-hoc tasks but no main clone under the repo roots "
                f"({where}); clone it there, or pass --root for the directory that holds it"
            )
            raise InFlightError(msg)
        if len(matches) > 1:
            listed = ", ".join(str(path) for path in matches)
            msg = (
                f"{repo} has more than one clone under the repo roots ({listed}); "
                "pass --root so that only its main clone is found"
            )
            raise InFlightError(msg)
        clones[repo] = matches[0]
    return clones


def _detail(exc: Exception) -> str:
    """A one-line reason for *exc*."""
    return " ".join(str(exc).split())


def _open_pr_issues(repo: str) -> frozenset[int]:
    """The issues of *repo* that an open PR closes or carries on a ``feature/<N>-`` head."""
    try:
        raw: Any = github.read_json(
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(epics.LISTING_LIMIT),
            "--json",
            "number,headRefName,closingIssuesReferences",
        )
        epics.refuse_truncated(raw, epics.LISTING_LIMIT, f"the open PRs of {repo}")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise InFlightError(f"{repo}: cannot read its open PRs: {_detail(exc)}") from exc
    if not isinstance(raw, list):
        raise InFlightError(f"{repo}: unexpected 'gh pr list' response: {raw!r}")
    issues: set[int] = set()
    for row in raw:
        if not isinstance(row, dict):
            raise InFlightError(f"{repo}: unexpected 'gh pr list' row: {row!r}")
        number = _feature_number(str(row.get("headRefName") or ""))
        if number is not None:
            issues.add(number)
        for closing in row.get("closingIssuesReferences") or []:
            issues.update(_closing_issue(repo, row, closing))
    return frozenset(issues)


def _closing_issue(repo: str, row: dict[str, Any], closing: Any) -> set[int]:
    """The issue *closing* names when it is in *repo*; none for another repo's issue."""
    try:
        owner = closing["repository"]["owner"]["login"]
        name = closing["repository"]["name"]
        number = int(closing["number"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = (
            f"{repo}: PR #{row.get('number')} has a malformed closing issue reference: "
            f"{closing!r}"
        )
        raise InFlightError(msg) from exc
    return {number} if _same_repo(f"{owner}/{name}", repo) else set()


def _ls_remote(repo: str, clone: Path) -> tuple[frozenset[str], dict[str, str]]:
    """The remote's ``feature/`` branches, and its relay refs as ``{branch: sha}``.

    Runs from *clone* so the call authenticates as the repo's own org.
    """
    try:
        output = git.read_output(
            "-C",
            str(clone),
            "ls-remote",
            f"https://github.com/{repo}.git",
            f"{_HEADS}{_FEATURE_PREFIX}*",
            f"{_RELAY_PREFIX}*",
        )
    except subprocess.CalledProcessError as exc:
        raise InFlightError(f"{repo}: cannot list its remote refs: {_detail(exc)}") from exc
    branches: set[str] = set()
    relays: dict[str, str] = {}
    for line in output.splitlines():
        sha, _tab, ref = line.partition("\t")
        if ref.startswith(_HEADS):
            branches.add(ref.removeprefix(_HEADS))
        elif ref.startswith(_RELAY_PREFIX):
            relays[ref.removeprefix(_RELAY_PREFIX)] = sha
    return frozenset(branches), relays


def _api_object(repo: str, path: str, source: str) -> dict[str, Any]:
    try:
        raw: Any = github.read_json("api", f"repos/{repo}/git/{path}")
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise InFlightError(f"{repo}: cannot read {source}: {_detail(exc)}") from exc
    if not isinstance(raw, dict):
        raise InFlightError(f"{repo}: unexpected response reading {source}: {raw!r}")
    return raw


def read_relay(repo: str, branch: str, sha: str) -> WorkflowState:
    """The workflow state the relay ref of *branch* (at commit *sha*) carries.

    Read by SHA through the Git Data API — the commit, its tree, then the
    ``pr-workflow.json`` blob — with no clone and nothing written.
    """
    source = f"the relay ref of {branch}"
    try:
        commit = _api_object(repo, f"commits/{sha}", source)
        tree = _api_object(repo, f"trees/{commit['tree']['sha']}", source)
        [entry] = [e for e in tree["tree"] if e.get("path") == _RELAY_FILE]
        blob = _api_object(repo, f"blobs/{entry['sha']}", source)
        if blob.get("encoding") != "base64":
            msg = f"unexpected blob encoding {blob.get('encoding')!r}"
            raise ValueError(msg)
        data = json.loads(base64.b64decode(blob["content"]))
        if not isinstance(data, dict):
            msg = f"the payload is not a JSON object: {data!r}"
            raise TypeError(msg)
        return WorkflowState.from_dict(data)
    except InFlightError:
        raise
    except (KeyError, TypeError, ValueError, binascii.Error, WorkflowError) as exc:
        msg = f"{repo}: cannot read {source}: malformed relay payload ({_detail(exc)})"
        raise InFlightError(msg) from exc


def _relay_issues(repo: str, branch: str, state: WorkflowState) -> set[int]:
    """The issues of *repo* that *state* hands off; another repo's entry marks nothing."""
    issues: set[int] = set()
    for entry in state.all_issues:
        try:
            ref = epics.parse_issue_ref(pr_body.normalize_issue_ref(entry), default_repo=repo)
        except ValueError as exc:
            msg = f"{repo}: the relay ref of {branch} names an unparsable issue {entry!r}"
            raise InFlightError(msg) from exc
        if _same_repo(f"{ref.owner}/{ref.repo}", repo):
            issues.add(ref.number)
    return issues


def _local_branches(repo: str, clone: Path) -> frozenset[str]:
    """The clone's local branches; a worktree's branch is always among them."""
    try:
        return frozenset(git.local_branches(clone))
    except subprocess.CalledProcessError as exc:
        msg = f"{repo}: cannot read the local branches of {clone}: {_detail(exc)}"
        raise InFlightError(msg) from exc


def repo_signals(repo: str, clone: Path) -> RepoSignals:
    """Read every in-flight source of *repo* once: its PRs, remote refs, relays and clone."""
    open_prs = _open_pr_issues(repo)
    remote, relays = _ls_remote(repo, clone)
    ready: set[int] = set()
    for branch, sha in sorted(relays.items()):
        state = read_relay(repo, branch, sha)
        if state.status == _READY:
            ready |= _relay_issues(repo, branch, state)
    return RepoSignals(
        repo=repo,
        clone=clone,
        open_pr_issues=open_prs,
        remote_branches=remote,
        ready_relay_issues=frozenset(ready),
        local_branches=_local_branches(repo, clone),
    )


def _path_exists(repo: str, path: Path) -> bool:
    try:
        return path.exists()
    except OSError as exc:
        raise InFlightError(f"{repo}: cannot check {path}: {_detail(exc)}") from exc


def marks_for(issue: int, signals: RepoSignals) -> tuple[str, ...]:
    """The marks that show *issue* of ``signals.repo`` in flight, in :data:`MARKS` order."""
    prefix = f"{_FEATURE_PREFIX}{issue}-"
    found = {
        "open-pr": issue in signals.open_pr_issues,
        "report-ready": issue in signals.ready_relay_issues,
        "branch": any(b.startswith(prefix) for b in signals.remote_branches),
        "worktree": any(b.startswith(prefix) for b in signals.local_branches),
        "merge-review": _path_exists(
            signals.repo, _issue_dir(signals.clone, issue) / _MERGE_REVIEW_DIR
        ),
        "batch": _path_exists(signals.repo, batch_marker.marker_path(signals.clone, f"#{issue}")),
    }
    return tuple(mark for mark in MARKS if found[mark])


def disposition(marks: Sequence[str]) -> Disposition | None:
    """Rank *marks*: handed off by a PR or ready record, else resumable locally, else remote."""
    present = set(marks)
    if present & _HANDED_OFF:
        return "handed-off"
    if present & _LOCAL:
        return "resume"
    if "branch" in present:
        return "handed-off"
    return None
