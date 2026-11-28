"""Build a merge-review request from a clean, fully validated branch.

``vrg-merge-review-request --issue <owner/name#N> --base <ref> --step
review|check|check-<k> [--design-source <path>]...
[--pr <N|URL>] [--fix-base <sha> --earlier-result <path>... --fix-id <ID>...
[--ruled-id <ID>]...]`` writes ``request-<step>.json``, ``diff-<step>.patch`` and, for a check,
``fix-<step>.patch`` (and, for a batch PR, ``commits-<step>.patch``) into the task's
private directory, ``build/.vergil-state/issue-implement/<N>/merge-review/``
in the main clone, and prints the request's path. See
``docs/specs/merge-review-protocol.md``.

The request also carries each diff's line count (``diff_lines``, and
``fix_diff_lines`` for a check), which the reviewer's closing coverage line
must report in full. A diff over 2000 lines, one ``Read`` call's limit, is
also written in numbered parts, ``diff-<step>.part-<NN>.patch`` (and
``fix-<step>.part-<NN>.patch``), listed in the request as ``diff_parts``
(``fix_diff_parts``) with each part's line count.

Before it reads or writes anything, in either mode, it refuses unless the
current clone's ``origin`` is the ``--issue`` repository (every push URL
names it on github.com); the refusal names both repositories.

It refuses, naming the reason, unless the worktree is clean and the last
``vrg-validate`` run (``.vergil/vrg-validate-last.json``) was a full run
that started clean at ``HEAD``, exited 0 and passed every stage. The base
SHA is the merge base of ``--base`` and ``HEAD``; the change class comes
from the files changed since it, against the allow-list committed there.

One issue of a batch before handoff is the exception: when ``--pr`` is not
given and the ``--issue`` has a batch marker, the request is scoped to that
issue's own commit, with its ``fixup!`` commits folded in. Its ``base_sha`` is
the parent of that commit, read from the branch (the marker's ``base_sha`` and
``--base`` are not used), so a clean rebase changes no issue's request;
``diff-<step>.patch`` holds that change alone, and the change class is judged
from the parent. ``head_sha`` stays ``HEAD``, so a check's fix diff is
unchanged. A batch member whose own commit cannot be found, or whose fixup
sits behind another commit, is refused.

The builder reads the body of the ``--issue`` itself, and derives the
epic's open tasks from its native parent: when the parent carries ``epic``
and not ``ad-hoc``, its open sub-issues, every page, as ``{"ref",
"title"}``, without the task; otherwise none. When the parent is a proxy
epic, they are the open children of its primary instead, its own and every
proxy's, as ``vrg-epic-audit --epic`` lists them. It refuses when either
cannot be read, or when a proxy's link to its primary is broken.

It also reads the comments of the ``--issue`` itself, every page, and
keeps only those posted by the one human login of the trusted-authors
configuration, oldest first, as ``issue_comments`` (``{"author",
"created_at", "body"}``). An edited human comment is left out and counted
in ``issue_comments_excluded_edited``. It refuses when the configuration
lists no human login or several, or the comments cannot be read.

``--pr`` names the open PR of a fix to an open PR, by number or URL; the
PR must be in the ``--issue`` repository, open, and headed by the current
branch. When its body has more than one ``Closes`` line it is a batch PR:
the ``--issue`` must be one of them, and the request adds
``batch_issues`` (every other issue it closes, each with its body and the
human's comments, read as for the ``--issue``), ``batch_commits`` (every
commit under review, ``{"sha", "subject", "issue"}``, its issue the one
its subject's trailing `` (#N)`` names) and ``batch_commits_path`` (each
commit's own patch), and leaves the batch's issues out of
``epic_open_tasks``. It refuses a merge commit, a commit that names no issue
of the PR, and an issue with no commit. A single-issue PR must link the
``--issue``; apart from the fix fields below, its request is the one built
without ``--pr``. A check refuses batch issues other than those its previous
step's request carried.

Every ``--pr`` request, single-issue or batch, also says which commits are
the fix to the open PR: ``pr_head_sha`` is the PR's head commit as GitHub
reports it, and ``fix_commits`` the commits of ``pr_head_sha..HEAD``, oldest
first, each ``{"sha", "subject"}`` (empty when ``HEAD`` is the PR's head).
When ``pr_head_sha`` is no longer an ancestor of ``HEAD`` (the branch was
rebased since submit), ``fix_commits`` is JSON ``null``, and the reviewer
judges no commit as a fix. A request without ``--pr`` carries neither field.
The reviewer judges each fix commit against the fix it was meant to make, as
the protocol's "A fix to an open PR" sets out.

A check (``check`` or ``check-<k>``) also takes ``--fix-base``, the ``HEAD``
the previous step judged, refused unless it is the ``head_sha`` of that
step's stored request; one ``--earlier-result`` per earlier step, in
step order, each checked against that step's stored request and its
capture record, as ``vrg-merge-review-check`` checks it; ``--fix-id``
for each earlier ID asked to change; and ``--ruled-id`` for each earlier
defect the human excused, which is not sent to the reviewer. It refuses an
ID no earlier result raised, an ID given to both, and a defect named by
neither unless a later result marks it ``fixed``.

``vrg-merge-review-request --issue <owner/name#N> --ensure-dir`` only checks
the private directory level by level (creating absent levels mode 700) and
prints it. The caller runs it before every write it makes there.

Exit codes: ``0`` done, ``1`` a refusal or error, ``2`` a usage error, ``3``
a check's ``--fix-base`` is no longer an ancestor of ``HEAD`` (the branch was
rebased).
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github
from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.build_dir import BuildDirError
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.merge_review import open_pr, schema
from vergil_tooling.lib.merge_review import record as record_module
from vergil_tooling.lib.merge_review import request as req
from vergil_tooling.lib.merge_review.change_class import classify_between, classify_branch
from vergil_tooling.lib.merge_review.schema import STEP_RE, is_check
from vergil_tooling.lib.merge_review.state import StateDirError, ensure_private_dir
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.validate_record import ValidateRecordError, read_record

if TYPE_CHECKING:
    from vergil_tooling.lib.epics import IssueRef
    from vergil_tooling.lib.merge_review.record import BatchSpan

EXIT_OK = 0
EXIT_ERROR = 1

_CHECK_ONLY = ("fix_base", "earlier_result", "fix_id", "ruled_id")


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _check_usage(parser, args)
    try:
        issue = req.parse_task_issue(args.issue)
        req.check_clone_is_issue_repo(issue)
        if args.ensure_dir:
            print(ensure_private_dir(git.main_worktree_root(), issue.number))
            return EXIT_OK
        print(_build(args, issue))
    except req.RequestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code
    except github.MissingGitHubTokenError:
        # A CalledProcessError too: cli_main reports it, not the git handler.
        raise
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: git failed (exit {exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        return EXIT_ERROR
    except (
        BuildDirError,
        StateDirError,
        ValidateRecordError,
        ConfigError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vrg-merge-review-request",
        description="Build a merge-review request from a clean, fully validated branch.",
    )
    parser.add_argument("--issue", required=True, help="the task issue, owner/name#N")
    parser.add_argument(
        "--ensure-dir",
        action="store_true",
        help="only check the private directory, creating absent levels, and print it",
    )
    parser.add_argument("--base", help="the base branch, such as origin/develop")
    parser.add_argument("--step", help="review, check or check-<k> (k from 2 to 9)")
    parser.add_argument(
        "--design-source", action="append", default=[], help="a design document (repeatable)"
    )
    parser.add_argument(
        "--pr", help="a fix to an open PR: the PR, by number or URL, in the --issue's repository"
    )
    parser.add_argument("--fix-base", help="a check: the HEAD the previous step judged")
    parser.add_argument(
        "--earlier-result",
        action="append",
        default=[],
        help="a check: each earlier step's result file, in step order (repeatable)",
    )
    parser.add_argument(
        "--fix-id", action="append", default=[], help="a check: an earlier ID asked to change"
    )
    parser.add_argument(
        "--ruled-id",
        action="append",
        default=[],
        help="a check: an earlier defect the human excused (not sent to the reviewer)",
    )
    return parser


def _check_usage(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    building = ("base", "step")
    if args.ensure_dir:
        given = [name for name in (*building, "pr", *_CHECK_ONLY) if getattr(args, name)]
        if given or args.design_source:
            parser.error("--ensure-dir takes only --issue")
        return
    missing = [name for name in building if getattr(args, name) is None]
    if missing:
        flags = ", ".join("--" + name.replace("_", "-") for name in missing)
        parser.error(f"building a request needs {flags}")
    if STEP_RE.fullmatch(args.step) is None:
        parser.error("--step must be review, check or check-<k> with k from 2 to 9")
    if is_check(args.step):
        if args.fix_base is None or not args.earlier_result:
            parser.error(f"a {args.step} needs --fix-base and --earlier-result")
    elif any(getattr(args, name) for name in _CHECK_ONLY):
        parser.error("--fix-base, --earlier-result, --fix-id and --ruled-id are for a check only")


def _build(args: argparse.Namespace, issue: IssueRef) -> Path:
    """Run every check, write the step's files, and return the request's path."""
    step: str = args.step
    worktree = git.repo_root()
    head = git.commit_sha("HEAD")
    record = read_record(worktree)
    req.check_validated(record, head, worktree_clean=git.working_tree_status() == "")
    span = _member_span(args, issue, worktree, head)
    if span is None:
        base_sha, change_class = classify_branch(args.base, head)
        reviewed_end = head
    else:
        base_sha = span.parent
        change_class = classify_between(span.parent, span.last)
        reviewed_end = span.last
    pr = (
        None
        if args.pr is None
        else open_pr.read_pr(open_pr.parse_pr(args.pr, issue), issue, git.current_branch())
    )
    batch = [] if pr is None else pr.batch
    task = req.fetch_task_context(issue, batch)
    design_sources = req.design_source_paths(args.design_source)
    issue_comments = req.fetch_human_comments(issue)
    fix_base_sha = _fix_base(args.fix_base, head) if is_check(step) else None

    others = [ref for ref in batch if ref.number != issue.number]
    batch_fields = _batch_fields(others, batch, base_sha, head)
    fix_fields = None if pr is None else _fix_fields(pr.head_sha, head)

    private_dir = ensure_private_dir(git.main_worktree_root(), issue.number)
    check: req.CheckFields | None = None
    fix_patch = b""
    fix_parts: list[tuple[Path, bytes]] = []
    if fix_base_sha is not None:
        earlier = req.read_earlier_results(private_dir, step, args.earlier_result)
        req.check_fix_base(private_dir, step, fix_base_sha)
        open_pr.check_same_batch(private_dir, step, others)
        finding_ids = req.earlier_finding_ids(
            [result for _path, result in earlier], args.fix_id, args.ruled_id
        )
        fix_patch = git.diff_patch(fix_base_sha, head)
        fix_parts = req.plan_parts(
            fix_patch, lambda number: private_dir / schema.fix_part_file(step, number)
        )
        check = req.CheckFields(
            fix_base_sha=fix_base_sha,
            fix_diff_path=str(private_dir / req.fix_file(step)),
            fix_diff_lines=fix_patch.count(b"\n"),
            fix_diff_parts=req.part_manifest(fix_parts),
            earlier_result_paths=[path for path, _result in earlier],
            earlier_finding_ids=finding_ids,
        )
    diff_bytes = git.diff_patch(base_sha, reviewed_end)
    diff_parts = req.plan_parts(
        diff_bytes, lambda number: private_dir / schema.diff_part_file(step, number)
    )
    diff_path = private_dir / req.diff_file(step)
    commits_path = private_dir / req.commits_file(step)
    batch_request = (
        None
        if batch_fields is None
        else req.BatchFields(
            issues=batch_fields[0], commits=batch_fields[1], commits_path=str(commits_path)
        )
    )
    request = req.build_request(
        req.RequestFields(
            request_id=secrets.token_hex(16),
            step=step,
            issue=issue,
            issue_body=task.issue_body,
            issue_comments=issue_comments,
            base_sha=base_sha,
            head_sha=head,
            worktree=str(worktree),
            diff_path=str(diff_path),
            diff_lines=diff_bytes.count(b"\n"),
            diff_parts=req.part_manifest(diff_parts),
            change_class=change_class,
            design_sources=design_sources,
            validate_stages=list(record.passed),
            epic_open_tasks=task.epic_open_tasks,
            batch=batch_request,
            fix=fix_fields,
        ),
        check,
    )
    # The request names the diffs and their parts, so it is written last.
    atomic_write(diff_path, diff_bytes)
    for part_path, chunk in diff_parts:
        atomic_write(part_path, chunk)
    if batch_request is not None:
        atomic_write(commits_path, git.log_patch(base_sha, head))
    if check is not None:
        atomic_write(Path(check.fix_diff_path), fix_patch)
        for part_path, chunk in fix_parts:
            atomic_write(part_path, chunk)
    request_path = private_dir / req.request_file(step)
    atomic_write(request_path, json.dumps(request, indent=2) + "\n")
    return request_path


def _member_span(
    args: argparse.Namespace, issue: IssueRef, repo: Path, head: str
) -> BatchSpan | None:
    """The span of a batch member's own commit, or ``None`` when the request is not one.

    A request is a batch member's when it has no ``--pr`` and the ``--issue``
    has a batch marker in the main clone. A marker that cannot be read, or a
    member whose own commit cannot be found, is a refusal.
    """
    if args.pr is not None:
        return None
    issue_ref = f"#{issue.number}"
    try:
        marker = batch_marker.read_marker(git.main_worktree_root(), issue_ref)
        if marker is None:
            return None
        return record_module.batch_member_span(repo, marker, issue_ref, head)
    except (batch_marker.BatchMarkerError, record_module.RecordError) as exc:
        req.refuse(str(exc))


def _batch_fields(
    others: list[IssueRef], batch: list[IssueRef], base_sha: str, head: str
) -> tuple[list[dict[str, object]], list[dict[str, str]]] | None:
    """A batch PR's ``batch_issues`` and ``batch_commits``; ``None`` on any other PR.

    Each of *others* (the batch's issues but the ``--issue``) has its body and
    the human's comments read as the ``--issue``'s are.
    """
    if not batch:
        return None
    commits = open_pr.batch_commits(base_sha, head, batch)
    issues: list[dict[str, object]] = []
    for ref in others:
        comments = req.fetch_human_comments(ref)
        issues.append(
            {
                "issue": ref.slug,
                "issue_body": req.fetch_issue_body(ref),
                "issue_comments": comments.comments,
                "issue_comments_excluded_edited": comments.excluded_edited,
            }
        )
    return issues, commits


def _fix_fields(pr_head_sha: str, head: str) -> req.FixFields:
    """A ``--pr`` request's fix fields: the PR's head and the commits after it, or ``None``."""
    return req.FixFields(pr_head_sha=pr_head_sha, commits=open_pr.fix_commits(pr_head_sha, head))


def _fix_base(fix_base: str, head: str) -> str:
    """The fix base's SHA; exit 3 when it is not an ancestor of *head*."""
    if not fix_base or fix_base.startswith("-"):
        req.refuse("--fix-base must be a commit")
    sha = git.commit_sha(f"{fix_base}^{{commit}}")
    if not git.is_ancestor(sha, head):
        req.refuse(
            f"--fix-base {sha[:12]} is not an ancestor of HEAD: the branch was rebased"
            " since the previous step",
            req.EXIT_REBASED,
        )
    return sha


if __name__ == "__main__":
    sys.exit(main())
