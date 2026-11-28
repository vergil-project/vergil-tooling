"""A fix to an open PR: the PR's head, the issues it closes and each commit of the fix.

A fix to an open, submitted PR is merge-reviewed as the whole branch against
``origin/develop`` (epic vergil-project/.github#1218, spec §6.8). On a batch
PR that diff holds the commits of every issue the PR closes, so a request
carrying one issue's scope would make every other issue's work read as
out of scope. ``vrg-merge-review-request --pr`` therefore reads the PR
itself and, on a batch PR, carries each issue's scope and maps each commit
to its issue (``docs/specs/merge-review-protocol.md``, "A batch PR").

Every ``--pr`` request also says which commits are the fix: the PR's head
commit as GitHub reports it (``pr_head_sha``) and the commits after it
(``fix_commits``), so the reviewer judges each one against the fix it was
meant to make, not its commit's original description (the protocol's "A fix
to an open PR"). :func:`read_pr` and :func:`fix_commits` build both.

The issues come from the PR's body, never from the agent: a PR whose body
has more than one ``Closes`` line is a batch PR, as ``vrg-pr-workflow
batch-check --pr`` and the ``merge-review`` skill read it. The PR is found
through the trust layer (epic vergil-project/.github#1467, spec §5.2): it must
be a same-repo PR from the current branch with a trusted author
(:func:`~vergil_tooling.lib.trust.prs_for_branch`), and its body counts only
when the layer trusts it (:func:`~vergil_tooling.lib.trust.read_issue` with
``require_body=True``), so a linkage line an outsider wrote or edited in never
names an issue. Every refusal is the builder's
:class:`~vergil_tooling.lib.merge_review.request.RequestError`.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, linkage, trust
from vergil_tooling.lib.epics import IssueRef, parse_issue_ref
from vergil_tooling.lib.merge_review.request import refuse, request_file
from vergil_tooling.lib.merge_review.schema import earlier_steps
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

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
#: A fix commit's record: SHA and subject.
_FIX_FIELDS = 2


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


@dataclass(frozen=True)
class OpenPRRead:
    """An open PR read for a fix: its head commit and, on a batch PR, the issues it closes.

    ``head_sha`` is the PR's ``headRefOid`` as GitHub reports it. ``batch`` is
    every issue the PR closes when it is a batch PR, and ``[]`` otherwise.
    """

    head_sha: str
    batch: list[IssueRef]


def read_pr(number: int, issue: IssueRef, branch: str) -> OpenPRRead:
    """Read the open PR *number*: its head commit, and its issues when it is a batch PR.

    The PR is read from *issue*'s repository and must be open, a same-repo PR
    with a trusted author and *branch* (the current branch) as its head, and
    its body must be trusted. A batch PR's issues are its body's
    ``Closes`` refs, in body order: each in the PR's repository, listed once,
    *issue* among them. A PR with one ``Closes`` line or none is a
    single-issue PR and must link *issue* (``Ref`` or ``Closes``); its
    ``batch`` is then ``[]``.
    """
    pr = f"{issue.owner}/{issue.repo}#{number}"
    state, body, head_sha = _read_pr(issue, number, branch, pr)
    if state.upper() != "OPEN":
        refuse(
            f"PR {pr} is {state.lower()}, not open: a fix is reviewed this way only on an open PR"
        )
    refs = linkage.extract_tracking_refs(body)
    closes = [ref for keyword, ref in refs if keyword == "Closes"]
    if len(closes) <= 1:
        linked = {_resolve(ref, issue).slug.lower() for _keyword, ref in refs}
        if issue.slug.lower() not in linked:
            refuse(f"PR {pr} does not link {issue.slug}: its body has no Ref or Closes line for it")
        return OpenPRRead(head_sha=head_sha, batch=[])
    issues: list[IssueRef] = []
    for raw in closes:
        ref = _in_repo(raw, issue, pr)
        if any(ref.number == seen.number for seen in issues):
            refuse(f"batch PR {pr} lists Closes #{ref.number} more than once")
        issues.append(ref)
    if all(ref.number != issue.number for ref in issues):
        listed = ", ".join(ref.slug for ref in issues)
        refuse(f"batch PR {pr} does not close {issue.slug}: its Closes lines name {listed}")
    return OpenPRRead(head_sha=head_sha, batch=issues)


def _read_pr(issue: IssueRef, number: int, branch: str, pr: str) -> tuple[str, str, str]:
    """The PR's ``state``, trusted body and ``headRefOid``, refusing a failed or untrusted read.

    The PR must be among *branch*'s same-repo PRs with a trusted author; each
    PR the branch lookup skips (a fork, an untrusted author) is reported on
    stderr, metadata only. An untrusted body is a refusal naming only its
    metadata.
    """
    repo = f"{issue.owner}/{issue.repo}"
    try:
        with github.target_org(issue.owner):
            prs, skipped = trust.prs_for_branch(repo, branch, state="all")
            trust.report_skipped(skipped, stream=sys.stderr)
            found = [ref for ref in prs if ref.number == number]
            if not found:
                refuse(
                    f"PR {pr} is not a same-repo PR with a trusted author whose head is"
                    f" the current branch {branch!r}"
                )
            content = trust.read_issue(repo, number, require_body=True)
    except github.MissingGitHubTokenError:
        raise
    except (
        TrustedAuthorsNotConfiguredError,
        github.GitHubAPIError,
        RuntimeError,  # a GraphQL error
        trust.UntrustedContentError,
        trust.TrustReadError,
    ) as exc:
        refuse(f"cannot read PR {pr}: {exc}")
    return found[0].state, content.body or "", found[0].head_oid


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


def fix_commits(pr_head_sha: str, head_sha: str) -> list[dict[str, str]] | None:
    """Every commit of ``pr_head_sha..head_sha``, oldest first, as ``{sha, subject}``.

    These are the commits a fix to the open PR added. ``[]`` when *head_sha*
    is *pr_head_sha*. ``None`` when *pr_head_sha* is not an ancestor of
    *head_sha*: the branch was rebased since the PR's head, so no commit can
    be told apart as the fix, and the caller carries that as the field's
    ``null``. A git failure, such as a *pr_head_sha* this clone does not
    hold, or a line of a shape other than the one asked for, is a refusal.
    """
    try:
        ancestor = git.is_ancestor(pr_head_sha, head_sha)
    except (subprocess.CalledProcessError, OSError) as exc:
        refuse(f"cannot read the PR head {pr_head_sha[:12]}: {exc}")
    if not ancestor:
        return None
    try:
        raw = git.read_output(
            "rev-list",
            "--reverse",
            "--no-commit-header",
            f"--format=%H{_FIELD_SEP}%s",
            f"{pr_head_sha}..{head_sha}",
            "--",
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        refuse(f"cannot read the fix commits {pr_head_sha[:12]}..{head_sha[:12]}: {exc}")
    commits: list[dict[str, str]] = []
    for line in raw.splitlines():
        fields = line.split(_FIELD_SEP, 1)
        if len(fields) != _FIX_FIELDS:
            refuse(f"cannot parse the git rev-list line {line!r}")
        sha, subject = fields
        commits.append({"sha": sha, "subject": subject})
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
