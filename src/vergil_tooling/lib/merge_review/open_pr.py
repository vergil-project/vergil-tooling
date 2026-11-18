"""A fix to an open PR: the issues the PR closes and, on a batch PR, each one's commits.

A fix to an open, submitted PR is merge-reviewed as the whole branch against
``origin/develop`` (epic vergil-project/.github#1218, spec §6.8). On a batch
PR that diff holds the commits of every issue the PR closes, so a request
carrying one issue's scope would make every other issue's work read as
out of scope. ``vrg-merge-review-request --pr`` therefore reads the PR
itself and, on a batch PR, carries each issue's scope and maps each commit
to its issue (``docs/specs/merge-review-protocol.md``, "A batch PR").

The issues come from the PR's body, never from the agent: a PR whose body
has more than one ``Closes`` line is a batch PR, as ``vrg-pr-workflow
batch-check --pr`` and the ``merge-review`` skill read it. Every refusal is the builder's
:class:`~vergil_tooling.lib.merge_review.request.RequestError`.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import git, github, linkage
from vergil_tooling.lib.epics import IssueRef, parse_issue_ref
from vergil_tooling.lib.merge_review.request import refuse, request_file
from vergil_tooling.lib.merge_review.schema import earlier_steps

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: ``--pr``: a PR number, or its github.com URL.
_PR_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)"
    r"/pull/(?P<number>[1-9][0-9]*)/?$"
)

#: A commit subject's trailing issue ref, `` (#N)``, as a batch commit carries it.
_SUBJECT_REF_RE = re.compile(r" \(#([0-9]+)\)$")

#: One rev-list record: SHA, parent SHAs, subject, split by a unit separator.
_FIELD_SEP = "\x1f"
_RECORD_FIELDS = 3

_PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state
      headRefName
      body
    }
  }
}
"""


def parse_pr(raw: str, issue: IssueRef) -> int:
    """``--pr``'s number: a bare number, or a URL of a PR in *issue*'s repository."""
    if _PR_NUMBER_RE.fullmatch(raw):
        return int(raw)
    match = _PR_URL_RE.fullmatch(raw)
    if match is None:
        refuse(
            f"--pr must be a PR number or https://github.com/<owner>/<name>/pull/<N>, not {raw!r}"
        )
    repo = f"{match['owner']}/{match['repo']}"
    if repo.lower() != f"{issue.owner}/{issue.repo}".lower():
        refuse(
            f"--pr names a PR in {repo}, not in the --issue's repository {issue.owner}/{issue.repo}"
        )
    return int(match["number"])


def batch_issues(number: int, issue: IssueRef, branch: str) -> list[IssueRef]:
    """Every issue the open PR *number* closes when it is a batch PR; ``[]`` otherwise.

    The PR is read from *issue*'s repository and must be open, with *branch*
    (the current branch) as its head. A batch PR's issues are its body's
    ``Closes`` refs, in body order: each in the PR's repository, listed once,
    *issue* among them. A PR with one ``Closes`` line or none is a
    single-issue PR and must link *issue* (``Ref`` or ``Closes``); the
    request then carries nothing more.
    """
    pr = f"{issue.owner}/{issue.repo}#{number}"
    node = _read_pr(issue, number, pr)
    state, head, body = node.get("state"), node.get("headRefName"), node.get("body")
    if not isinstance(state, str) or not isinstance(head, str) or not isinstance(body, str):
        refuse(f"cannot read PR {pr}: it has no state, head branch or body")
    if state.upper() != "OPEN":
        refuse(
            f"PR {pr} is {state.lower()}, not open: a fix is reviewed this way only on an open PR"
        )
    if head != branch:
        refuse(f"PR {pr} is the head of branch {head!r}, not of the current branch {branch!r}")
    refs = linkage.extract_tracking_refs(body)
    closes = [ref for keyword, ref in refs if keyword == "Closes"]
    if len(closes) <= 1:
        linked = {_resolve(ref, issue).slug.lower() for _keyword, ref in refs}
        if issue.slug.lower() not in linked:
            refuse(f"PR {pr} does not link {issue.slug}: its body has no Ref or Closes line for it")
        return []
    issues: list[IssueRef] = []
    for raw in closes:
        ref = _in_repo(raw, issue, pr)
        if any(ref.number == seen.number for seen in issues):
            refuse(f"batch PR {pr} lists Closes #{ref.number} more than once")
        issues.append(ref)
    if all(ref.number != issue.number for ref in issues):
        listed = ", ".join(ref.slug for ref in issues)
        refuse(f"batch PR {pr} does not close {issue.slug}: its Closes lines name {listed}")
    return issues


def _read_pr(issue: IssueRef, number: int, pr: str) -> dict[str, Any]:
    """The PR's ``state``, ``headRefName`` and ``body``, refusing a failed read."""
    try:
        with github.target_org(issue.owner):
            data = github.graphql(_PR_QUERY, owner=issue.owner, name=issue.repo, number=number)
    except github.MissingGitHubTokenError:
        raise
    except (github.GitHubAPIError, RuntimeError) as exc:  # RuntimeError: a GraphQL error
        refuse(f"cannot read PR {pr}: {exc}")
    repository = data.get("repository") if isinstance(data, dict) else None
    node = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(node, dict):
        refuse(f"cannot read PR {pr}: no such pull request")
    return node


def _resolve(raw: str, issue: IssueRef) -> IssueRef:
    """A linkage ref of the PR body, a bare ``#N`` read in *issue*'s repository."""
    return parse_issue_ref(raw, default_repo=f"{issue.owner}/{issue.repo}")


def _in_repo(raw: str, issue: IssueRef, pr: str) -> IssueRef:
    """A batch PR's ``Closes`` ref, refused unless it is in *issue*'s repository."""
    ref = _resolve(raw, issue)
    if f"{ref.owner}/{ref.repo}".lower() != f"{issue.owner}/{issue.repo}".lower():
        refuse(f"batch PR {pr} closes {raw}, an issue outside {issue.owner}/{issue.repo}")
    return IssueRef(owner=issue.owner, repo=issue.repo, number=ref.number)


def batch_commits(base_sha: str, head_sha: str, issues: Sequence[IssueRef]) -> list[dict[str, str]]:
    """Every commit of ``base_sha..head_sha``, oldest first, as ``{sha, subject, issue}``.

    Each commit belongs to the issue its subject's trailing `` (#N)`` names,
    a ``fixup!`` of an issue's commit included, and that issue must be one of
    *issues*. A merge commit, a commit naming no issue or another one, and an
    issue with no commit each refuse: the reviewer could not judge that work
    against its own issue's scope.
    """
    by_number = {ref.number: ref for ref in issues}
    try:
        raw = git.read_output(
            "rev-list",
            "--reverse",
            "--no-commit-header",
            f"--format=%H{_FIELD_SEP}%P{_FIELD_SEP}%s",
            f"{base_sha}..{head_sha}",
            "--",
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        refuse(f"cannot read the commits {base_sha[:12]}..{head_sha[:12]}: {exc}")
    commits: list[dict[str, str]] = []
    for line in raw.splitlines():
        fields = line.split(_FIELD_SEP, _RECORD_FIELDS - 1)
        if len(fields) != _RECORD_FIELDS:
            refuse(f"cannot parse the git rev-list line {line!r}")
        sha, parents, subject = fields
        if len(parents.split()) > 1:
            refuse(f"commit {sha[:12]} is a merge commit: a batch PR's branch is linear")
        match = _SUBJECT_REF_RE.search(subject)
        ref = by_number.get(int(match.group(1))) if match is not None else None
        if ref is None:
            refuse(
                f"commit {sha[:12]} ({subject}) names no issue of the batch PR:"
                " each commit's subject must end in (#N) for one of its issues"
            )
        commits.append({"sha": sha, "subject": subject, "issue": ref.slug})
    named = {commit["issue"] for commit in commits}
    missing = [ref.slug for ref in issues if ref.slug not in named]
    if missing:
        refuse(f"the batch PR's branch has no commit for {', '.join(missing)}")
    return commits


def check_same_batch(private_dir: Path, step: str, others: Sequence[IssueRef]) -> None:
    """Refuse a check whose batch issues differ from those the previous step carried.

    The previous step's ``batch_issues`` are read from its stored request in
    *private_dir* (none when it has no such field), so a run cannot add or
    drop an issue's scope between its review and a check.
    """
    previous = earlier_steps(step)[-1]
    request_path = private_dir / request_file(previous)
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"cannot read the {previous} request: {exc}")
    stored = request.get("batch_issues", []) if isinstance(request, dict) else None
    if not isinstance(stored, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("issue"), str) for entry in stored
    ):
        refuse(f"{request_path} has a malformed batch_issues")
    judged = [entry["issue"] for entry in stored]
    current = [ref.slug for ref in others]
    if judged != current:
        refuse(
            f"the {previous} request carried the batch issues {', '.join(judged) or 'none'},"
            f" this {step} {', '.join(current) or 'none'}: give the same --pr as the {previous}"
        )
