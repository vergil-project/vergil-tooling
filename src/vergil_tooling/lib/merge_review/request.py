"""The merge-review request builder's rules: what it checks and what it writes.

``vrg-merge-review-request`` (the CLI) gathers the inputs; this module holds
every refusal and builds the request object of
``docs/specs/merge-review-protocol.md`` ("The request"). A refusal is a
:class:`RequestError` naming the reason, raised through :func:`refuse`; its
``exit_code`` is 1, or :data:`EXIT_REBASED` when a check's fix base is no
longer an ancestor of ``HEAD``, so the caller can tell that stop apart.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from vergil_tooling.lib import github
from vergil_tooling.lib.epics import parse_issue_ref
from vergil_tooling.lib.merge_review.result import ResultError, parse_result
from vergil_tooling.lib.merge_review.schema import REQUEST_SCHEMA, earlier_steps
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import fetch_comments
from vergil_tooling.lib.rulings.draft_ref import require_remote_is
from vergil_tooling.lib.rulings.identity import human_login
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.epics import IssueRef
    from vergil_tooling.lib.merge_review.result import Result
    from vergil_tooling.lib.rulings.comments import IssueComment
    from vergil_tooling.lib.validate_record import ValidateRecord

#: The exit code of a check whose fix base was rebased away (the skill's ``B1``).
EXIT_REBASED = 3

_EPIC_TASK_FIELDS = frozenset({"ref", "title"})

#: A full commit SHA, SHA-1 or SHA-256, as ``git rev-parse`` prints it.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class RequestError(Exception):
    """The builder refuses; ``exit_code`` is what the CLI exits with."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def refuse(message: str, exit_code: int = 1) -> NoReturn:
    """Raise the builder's one refusal, :class:`RequestError`."""
    raise RequestError(message, exit_code)


def request_file(step: str) -> str:
    """The request's file name for *step*."""
    return f"request-{step}.json"


def diff_file(step: str) -> str:
    """The whole branch's diff file name for *step*."""
    return f"diff-{step}.patch"


def fix_file(step: str) -> str:
    """A check's fix diff file name for *step*."""
    return f"fix-{step}.patch"


def parse_task_issue(ref: str) -> IssueRef:
    """Parse ``--issue``: exactly ``owner/name#N`` with ``N`` at least 1."""
    try:
        issue = parse_issue_ref(ref, default_repo="")
    except ValueError:
        refuse(f"--issue must be owner/name#N, not {ref!r}")
    if issue.number < 1:
        refuse(f"--issue must name an issue number of 1 or more, not {ref!r}")
    return issue


def check_clone_is_issue_repo(issue: IssueRef) -> None:
    """Refuse unless the current clone's ``origin`` is *issue*'s repository.

    The private directory is resolved from the current directory's main
    clone, so a run from a clone of another repository would write there,
    where a resumed run never looks. The check is
    :func:`vergil_tooling.lib.rulings.draft_ref.require_remote_is`, the one
    ``vrg-ruling draft`` makes: every push URL of ``origin`` must name the
    issue's repository on github.com. The refusal names both repositories
    and never a URL.
    """
    try:
        require_remote_is(f"{issue.owner}/{issue.repo}", command="vrg-merge-review-request")
    except RulingError as exc:
        refuse(str(exc))


def check_validated(record: ValidateRecord, head: str, *, worktree_clean: bool) -> None:
    """Refuse unless ``HEAD`` is committed and passed a full, clean vrg-validate run."""
    rerun = "run vrg-container-run -- vrg-validate on the committed branch"
    if not worktree_clean:
        refuse("the worktree has uncommitted or untracked changes: commit them first")
    if record.head != head:
        refuse(f"vrg-validate last ran at {record.head[:12]}, not at HEAD {head[:12]}: {rerun}")
    if record.exit_code != 0:
        refuse(f"the last vrg-validate run exited {record.exit_code}: {rerun}")
    if not record.full:
        refuse(f"the last vrg-validate run was partial (--check): {rerun} in full")
    if not record.clean_at_start:
        refuse(f"the last vrg-validate run started with uncommitted changes: {rerun}")
    if record.not_passed:
        names = ", ".join(record.not_passed)
        refuse(f"the last vrg-validate run did not pass every stage ({names}): {rerun}")


def read_issue_body(path: Path) -> str:
    """The issue body file's text, refusing an unreadable file."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        refuse(f"cannot read the issue body file {path}: {exc}")


def read_epic_tasks(path: Path) -> list[dict[str, str]]:
    """The epic tasks file: a JSON list of ``{"ref": "owner/name#N", "title": "..."}``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"cannot read the epic tasks file {path}: {exc}")
    if not isinstance(data, list):
        refuse(f"the epic tasks file {path} must hold a JSON list")
    for position, task in enumerate(data, start=1):
        if (
            not isinstance(task, dict)
            or set(task) != _EPIC_TASK_FIELDS
            or not isinstance(task["ref"], str)
            or not isinstance(task["title"], str)
        ):
            refuse(f"the epic tasks file {path}: entry {position} must be {{ref, title}} strings")
        try:
            parse_issue_ref(task["ref"], default_repo="")
        except ValueError:
            refuse(f"the epic tasks file {path}: entry {position}'s ref must be owner/name#N")
    return data


@dataclass(frozen=True)
class HumanComments:
    """The human's unedited comments on the issue, and how many edited ones were left out."""

    comments: list[dict[str, str]]
    excluded_edited: int


def human_comments(comments: Sequence[IssueComment], human: str) -> HumanComments:
    """The comments by *human*, oldest first, as ``{"author", "created_at", "body"}``.

    Only a comment authenticated as the human login can amend the scope a
    merge review judges against, so every other author (an agent, a third
    party, a deleted account) is dropped. An edited human comment is left
    out too, as ``vrg-ruling`` refuses an edited reply, and counted. A kept
    comment whose ``created_at`` is not an ISO 8601 timestamp with a time
    zone is refused by its comment ID, never echoing a value.
    """
    kept: list[dict[str, str]] = []
    edited = 0
    for comment in comments:
        if comment.author != human:
            continue
        if comment.edited:
            edited += 1
            continue
        if not _is_zoned_timestamp(comment.created_at):
            refuse(
                f"comment {comment.id} on the issue has a created_at that is not"
                " an ISO 8601 timestamp with a time zone"
            )
        kept.append(
            {"author": comment.author, "created_at": comment.created_at, "body": comment.body}
        )
    return HumanComments(comments=kept, excluded_edited=edited)


def fetch_human_comments(issue: IssueRef) -> HumanComments:
    """Read every page of *issue*'s comments and keep the human's unedited ones.

    The human login comes from the trusted-authors configuration, which must
    list exactly one; the comments come from
    :func:`vergil_tooling.lib.rulings.comments.fetch_comments`, the reader
    ``vrg-ruling`` uses. A configuration or read failure is a refusal, but
    a missing GitHub token propagates to the CLI's auth-error boundary. An
    invalid configuration's ``ValueError`` propagates too; the CLI reports it.
    """
    try:
        human = human_login(load_trusted_authors())
        with github.target_org(issue.owner):
            fetched = fetch_comments(f"{issue.owner}/{issue.repo}", issue.number)
    except github.MissingGitHubTokenError:
        raise
    except (
        RulingError,
        TrustedAuthorsNotConfiguredError,
        github.GitHubAPIError,
        RuntimeError,  # a GraphQL error response
    ) as exc:
        refuse(f"cannot read the issue's comments: {exc}")
    return human_comments(fetched, human)


def _is_zoned_timestamp(text: str) -> bool:
    """Whether *text* is an ISO 8601 date and time that carries a time zone."""
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return False
    return stamp.tzinfo is not None


def design_source_paths(paths: Sequence[str]) -> list[str]:
    """Each design source as an absolute path, refusing one that is not a file."""
    resolved: list[str] = []
    for raw in paths:
        path = Path(raw).resolve()
        if not path.is_file():
            refuse(f"design source {raw} is not a readable file")
        resolved.append(str(path))
    return resolved


def read_earlier_results(
    private_dir: Path, step: str, result_paths: Sequence[str]
) -> list[tuple[str, Result]]:
    """Check every earlier step's result against that step's stored request.

    *result_paths* are in step order, one per step before *step*. Each is
    checked by :func:`parse_result` against ``request-<earlier>.json`` in
    *private_dir*. Returns ``(absolute path, checked result)`` pairs.
    """
    steps = earlier_steps(step)
    if len(result_paths) != len(steps):
        refuse(
            f"a {step} needs one --earlier-result per earlier step, in order"
            f" ({', '.join(steps)}); got {len(result_paths)}"
        )
    checked: list[tuple[str, Result]] = []
    for earlier, raw in zip(steps, result_paths, strict=True):
        path = Path(raw).resolve()
        request_path = private_dir / request_file(earlier)
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            refuse(f"cannot read the {earlier} request or result: {exc}")
        if not isinstance(request, dict):
            refuse(f"{request_path} must hold a JSON object")
        try:
            checked.append((str(path), parse_result(text, request)))
        except ResultError as exc:
            refuse(f"the {earlier} result {path} fails the result check: {exc}")
    return checked


def check_fix_base(private_dir: Path, step: str, fix_base_sha: str) -> None:
    """Refuse a check's fix base unless it is the ``HEAD`` the previous step judged.

    The previous step's ``head_sha`` is read from its stored
    ``request-<previous>.json`` in *private_dir*, so the check's fix diff
    always starts where the judged change ended.
    """
    previous = earlier_steps(step)[-1]
    request_path = private_dir / request_file(previous)
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        refuse(f"cannot read the {previous} request: {exc}")
    judged = request.get("head_sha") if isinstance(request, dict) else None
    if not isinstance(judged, str) or _COMMIT_SHA_RE.fullmatch(judged) is None:
        refuse(f"{request_path} must hold a head_sha that is a full commit SHA")
    if fix_base_sha != judged:
        refuse(
            f"--fix-base {fix_base_sha[:12]} is not the HEAD the {previous} judged"
            f" ({judged[:12]}, the head_sha of {request_path.name}): give that commit"
        )


def earlier_finding_ids(
    results: Sequence[Result], fix_ids: Sequence[str], ruled_ids: Sequence[str]
) -> list[str]:
    """The IDs a check asks about: *fix_ids*, once every rule on them holds.

    Every ID in *fix_ids* (asked to change) and *ruled_ids* (an earlier
    defect the human excused) must have been raised by an earlier result,
    and none may be in both. Every earlier defect must be named by one of
    the two, unless its latest status in a later result is ``fixed``.
    *ruled_ids* are not sent to the reviewer.
    """
    if not fix_ids:
        refuse("nothing to check: give --fix-id for each earlier finding asked to change")
    for flag, ids in (("--fix-id", fix_ids), ("--ruled-id", ruled_ids)):
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            refuse(f"{flag} names {', '.join(repeated)} more than once")
    both = sorted(set(fix_ids) & set(ruled_ids))
    if both:
        refuse(f"{', '.join(both)} given to both --fix-id and --ruled-id")
    raised = {finding.id: finding for result in results for finding in result.findings}
    unknown = [i for i in (*fix_ids, *ruled_ids) if i not in raised]
    if unknown:
        refuse(f"no earlier result raised {', '.join(unknown)}")
    latest: dict[str, str] = {}
    for result in results:
        latest.update(result.fix_status)
    excused = {*fix_ids, *ruled_ids, *(i for i, status in latest.items() if status == "fixed")}
    unnamed = [i for i, finding in raised.items() if finding.kind == "defect" and i not in excused]
    if unnamed:
        refuse(f"earlier defect {', '.join(unnamed)} named by neither --fix-id nor --ruled-id")
    return list(fix_ids)


@dataclass(frozen=True)
class CheckFields:
    """The fields a check adds to the request."""

    fix_base_sha: str
    fix_diff_path: str
    earlier_result_paths: list[str]
    earlier_finding_ids: list[str]


@dataclass(frozen=True)
class RequestFields:
    """Every field a review's request carries."""

    request_id: str
    step: str
    issue: IssueRef
    issue_body: str
    issue_comments: HumanComments
    base_sha: str
    head_sha: str
    worktree: str
    diff_path: str
    change_class: str
    design_sources: list[str]
    validate_stages: list[str]
    epic_open_tasks: list[dict[str, str]]


def build_request(fields: RequestFields, check: CheckFields | None) -> dict[str, object]:
    """The request object, a check's fields added when *check* is given."""
    request: dict[str, object] = {
        "schema": REQUEST_SCHEMA,
        "request_id": fields.request_id,
        "step": fields.step,
        "repo": f"{fields.issue.owner}/{fields.issue.repo}",
        "issue": fields.issue.slug,
        "issue_body": fields.issue_body,
        "issue_comments": fields.issue_comments.comments,
        "issue_comments_excluded_edited": fields.issue_comments.excluded_edited,
        "base_sha": fields.base_sha,
        "head_sha": fields.head_sha,
        "worktree": fields.worktree,
        "diff_path": fields.diff_path,
        "change_class": fields.change_class,
        "design_sources": fields.design_sources,
        "validate_stages": fields.validate_stages,
        "epic_open_tasks": fields.epic_open_tasks,
    }
    if check is not None:
        request["fix_base_sha"] = check.fix_base_sha
        request["fix_diff_path"] = check.fix_diff_path
        request["earlier_result_paths"] = check.earlier_result_paths
        request["earlier_finding_ids"] = check.earlier_finding_ids
    return request
