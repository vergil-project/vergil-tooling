"""The batch rules: the hard rules and the one-commit-per-issue invariant.

A *batch* is one PR that implements and closes 2 to 8 same-epic, same-repo
issues, one commit per issue (epic vergil-project/.github#1019). Its two
checks are shared by ``vrg-pr-workflow batch-start``, ``report-ready``,
``vrg-submit-pr`` and ``vrg-finalize-pr``, so every stage refuses the same
batch with the same words:

- :func:`check_hard_rules` reads each issue on GitHub and refuses a batch
  that breaks a hard rule (spec §5.1).
- :func:`check_commit_per_issue` reads the branch's commits since the base and
  refuses unless there is exactly one per issue, in the listed order, each
  subject ending in its issue's ref, and no merge commit.

Both **fail closed**. A read that fails (a label, a parent, a blocker, an
author, the trusted-author list, the git history) refuses and names what
could not be read; no rule is ever skipped. Every refusal is a
:class:`BatchRuleError` naming the rule and the issue or commit.
"""

from __future__ import annotations

import re
import subprocess
from functools import partial
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, git, github
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Callable

MIN_BATCH = 2
MAX_BATCH = 8

_APP_PREFIX = "app/"

# The errors a GitHub or configuration read can end in. Each is a refusal,
# never a pass: a GitHubAPIError is a CalledProcessError, GraphQL errors are
# RuntimeError, a malformed response or configuration is ValueError (JSON
# decoding included), and a malformed GraphQL node is KeyError or TypeError.
_READ_ERRORS: tuple[type[Exception], ...] = (
    subprocess.CalledProcessError,
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    TrustedAuthorsNotConfiguredError,
)

# A commit subject's trailing issue ref: " (#N)" at the very end.
_SUBJECT_REF_RE = re.compile(r" \(#([0-9]+)\)$")

# The subject prefixes git's autosquash folds into an earlier commit.
_FOLD_PREFIXES = ("fixup! ", "squash! ", "amend! ")

# One rev-list record: SHA, parent SHAs, subject, split by a unit separator.
_FIELD_SEP = "\x1f"
_RECORD_FIELDS = 3


class BatchRuleError(WorkflowError):
    """A batch breaks a hard rule or the commit-per-issue invariant.

    The message names the rule and the issue or commit that breaks it.
    """

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"batch refused ({rule}): {detail}")


def is_batch(issues: list[str]) -> bool:
    """True when *issues* is a batch (more than one issue)."""
    return len(issues) > 1


def _read[T](what: str, rule: str, read: Callable[[], T]) -> T:
    """Run one GitHub or configuration read; a failure refuses, naming *what*.

    A missing GitHub token propagates unchanged, so the CLI's auth-error
    boundary prints its configuration fix.
    """
    try:
        return read()
    except github.MissingGitHubTokenError:
        raise
    except _READ_ERRORS as exc:
        raise BatchRuleError(rule, f"could not read {what}: {error_text(exc)}") from exc


def _parse_refs(issues: list[str], repo: str) -> list[epics.IssueRef]:
    """Parse each canonical ref, refusing a malformed one or one in another repo."""
    parsed: list[epics.IssueRef] = []
    for ref in issues:
        try:
            issue = epics.parse_issue_ref(ref, default_repo=repo)
        except ValueError as exc:
            raise BatchRuleError("issue ref", f"{ref!r} is not an issue ref: {exc}") from exc
        if f"{issue.owner}/{issue.repo}".lower() != repo.lower():
            raise BatchRuleError(
                "same repo",
                f"{ref} is in {issue.owner}/{issue.repo}, not in this PR's repo {repo}",
            )
        parsed.append(issue)
    return parsed


def _check_count_and_duplicates(issues: list[str], parsed: list[epics.IssueRef]) -> None:
    count = len(issues)
    if count < MIN_BATCH or count > MAX_BATCH:
        raise BatchRuleError(
            "batch size",
            f"a batch has {MIN_BATCH} to {MAX_BATCH} issues, got {count} ({', '.join(issues)})",
        )
    seen: dict[tuple[str, str, int], str] = {}
    for ref, issue in zip(issues, parsed, strict=True):
        key = (issue.owner.lower(), issue.repo.lower(), issue.number)
        if key in seen:
            raise BatchRuleError(
                "no duplicate", f"{ref} is listed more than once (first as {seen[key]})"
            )
        seen[key] = ref


def _check_task(ref: str, issue: epics.IssueRef, repo: str, authors: TrustedAuthors) -> None:
    """The per-issue refusals: a task, not queued, trusted author, not operational."""
    kind = _read(f"the labels of {ref}", "task", lambda: epics.non_task_kind(issue))
    if kind is not None:
        raise BatchRuleError("task", epics.non_task_refusal_message("--issue", ref, kind))

    queue = _read(
        f"the parent of {ref}",
        "not queued",
        lambda: epics.queue_item_kind(issue.slug, default_repo=repo),
    )
    if queue is not None:
        raise BatchRuleError("not queued", epics.queue_refusal_message(ref, queue))

    login = _read(f"the author of {ref}", "trusted author", lambda: epics.issue_author(issue))
    bare = login.removeprefix(_APP_PREFIX)
    if bare not in authors.human and bare not in authors.agents:
        raise BatchRuleError(
            "trusted author",
            f"{ref} was opened by {login}, who is not listed by vrg-trusted-authors",
        )

    operational = _read(
        f"the labels of {ref}", "not operational", lambda: epics.is_operational(issue)
    )
    if operational:
        raise BatchRuleError(
            "not operational",
            f"{ref} is an operational task (validation/deployment), which is not PR-workable",
        )

    blockers = _read(
        f"the Blocked-by lines of {ref}",
        "no open deployment blocker",
        lambda: epics.blockers_of(issue),
    )
    for blocker in blockers:
        blocker_kind = _read(
            f"the labels of {ref}'s blocker {blocker.slug}",
            "no open deployment blocker",
            partial(epics.operational_kind, blocker),
        )
        if blocker_kind != "deployment":
            continue
        state = _read(
            f"the state of {ref}'s blocker {blocker.slug}",
            "no open deployment blocker",
            partial(epics.issue_state, blocker),
        )
        if state != "CLOSED":
            raise BatchRuleError(
                "no open deployment blocker",
                f"{ref} is Blocked-by {blocker.slug}, an open deployment task;"
                " a batch lands all at once, so deploy it first",
            )


def _check_shared_parent(issues: list[str], parsed: list[epics.IssueRef]) -> None:
    """One parent for every issue: a live epic that is not a queue epic."""
    parents: list[epics.IssueRef] = []
    for ref, issue in zip(issues, parsed, strict=True):
        parent = _read(f"the parent of {ref}", "same epic", partial(epics.parent_of, issue))
        if parent is None:
            raise BatchRuleError(
                "same epic", f"{ref} has no parent epic; every batch issue is a task under one"
            )
        parents.append(parent)

    first = parents[0]
    for ref, parent in zip(issues, parents, strict=True):
        if parent != first:
            raise BatchRuleError(
                "same epic",
                f"{ref} is under {parent.slug}, but {issues[0]} is under {first.slug};"
                " a batch's issues share one parent epic",
            )

    if not _read(f"the labels of {first.slug}", "same epic", lambda: epics.is_epic(first)):
        raise BatchRuleError(
            "same epic", f"the parent {first.slug} is not an epic (it has no epic label)"
        )
    queue = _read(f"the labels of {first.slug}", "same epic", lambda: epics.queue_kind(first))
    if queue is not None:
        raise BatchRuleError(
            "same epic",
            f"the parent {first.slug} is the {queue} queue epic; only a finite epic"
            " or the repo's ad-hoc epic qualifies",
        )


def check_hard_rules(issues: list[str], *, repo: str) -> None:
    """Refuse *issues* unless they form a valid batch for a PR in *repo*.

    *issues* are canonical refs (``#N`` or ``owner/repo#N``) in batch order;
    *repo* is the PR's ``owner/name``. The rules (spec §5.1): 2 to 8 issues,
    none listed twice, each in *repo*, each a task (not an epic or archive)
    and not a queue item, each opened by a trusted author, none operational,
    none Blocked-by an open deployment task, and one shared parent that is a
    finite epic or the repo's ad-hoc epic. Raises :class:`BatchRuleError` at
    the first rule broken, and on any read that fails.
    """
    parsed = _parse_refs(issues, repo)
    _check_count_and_duplicates(issues, parsed)
    authors = _read(
        "the trusted authors (vrg-trusted-authors)", "trusted author", load_trusted_authors
    )
    for ref, issue in zip(issues, parsed, strict=True):
        _check_task(ref, issue, repo, authors)
    _check_shared_parent(issues, parsed)


def _issue_number(ref: str) -> int:
    match = re.fullmatch(r"(?:[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)?#([0-9]+)", ref.strip())
    if match is None:
        raise BatchRuleError("issue ref", f"{ref!r} is not an issue ref")
    return int(match.group(1))


def _commits(base: str, head: str) -> list[tuple[str, list[str], str]]:
    """``(sha, parents, subject)`` for each commit in ``base..head``, oldest first."""
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            raise BatchRuleError("one commit per issue", f"not a git revision: {rev!r}")
    try:
        raw = git.read_output(
            "rev-list",
            "--reverse",
            "--no-commit-header",
            f"--format=%H{_FIELD_SEP}%P{_FIELD_SEP}%s",
            f"{base}..{head}",
            "--",
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise BatchRuleError(
            "one commit per issue",
            f"could not read the commits {base}..{head}: {error_text(exc)}",
        ) from exc
    commits: list[tuple[str, list[str], str]] = []
    for line in raw.splitlines():
        fields = line.split(_FIELD_SEP, _RECORD_FIELDS - 1)
        if len(fields) != _RECORD_FIELDS:
            raise BatchRuleError(
                "one commit per issue", f"could not parse the git rev-list line {line!r}"
            )
        sha, parents, subject = fields
        commits.append((sha, parents.split(), subject))
    return commits


def check_commit_per_issue(issues: list[str], *, base: str, head: str) -> None:
    """Refuse unless ``base..head`` holds exactly one commit per issue, in order.

    Commit *i* (oldest first) must have a subject ending in a space and
    ``(#N)`` for ``issues[i]``. A merge commit, a fixup left unfolded, a
    missing or extra commit, a commit out of order and a subject with no or
    another issue's ref each refuse, naming the commit's short SHA.
    """
    numbers = [_issue_number(ref) for ref in issues]
    commits = _commits(base, head)

    for sha, parents, subject in commits:
        if len(parents) > 1:
            raise BatchRuleError(
                "no merge commits",
                f"commit {sha[:12]} ({subject}) is a merge commit; a batch branch is"
                f" linear, so rebase it onto {base} instead of merging",
            )
    for sha, _parents, subject in commits:
        if subject.startswith(_FOLD_PREFIXES):
            raise BatchRuleError(
                "one commit per issue",
                f"commit {sha[:12]} ({subject}) is an unfolded fixup; fold it with"
                f" vrg-git rebase -i --autosquash {base}",
            )
    if len(commits) != len(issues):
        listed = ", ".join(f"{sha[:12]} ({subject})" for sha, _p, subject in commits) or "none"
        raise BatchRuleError(
            "one commit per issue",
            f"{len(issues)} issues ({', '.join(issues)}) need {len(issues)} commits"
            f" since {base}, found {len(commits)}: {listed}",
        )
    for (sha, _parents, subject), ref, number in zip(commits, issues, numbers, strict=True):
        match = _SUBJECT_REF_RE.search(subject)
        if match is not None and int(match.group(1)) == number:
            continue
        if match is None:
            found = "carries no issue ref"
        elif int(match.group(1)) in numbers:
            found = f"carries (#{match.group(1)}), out of the listed order"
        else:
            found = f"carries (#{match.group(1)}), an issue not in the batch"
        raise BatchRuleError(
            "one commit per issue",
            f"commit {sha[:12]} ({subject}) {found}; it should end in (#{number}) for {ref}",
        )
