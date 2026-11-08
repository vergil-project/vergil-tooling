"""Build a merge-review request from a clean, fully validated branch.

``vrg-merge-review-request --issue <owner/name#N> --base <ref> --step
review|check|check-<k> [--design-source <path>]...
[--fix-base <sha> --earlier-result <path>... --fix-id <ID>... [--ruled-id <ID>]...]``
writes ``request-<step>.json``, ``diff-<step>.patch`` and, for a check,
``fix-<step>.patch`` into the task's
private directory, ``build/.vergil-state/issue-implement/<N>/merge-review/``
in the main clone, and prints the request's path. See
``docs/specs/merge-review-protocol.md``.

Before it reads or writes anything, in either mode, it refuses unless the
current clone's ``origin`` is the ``--issue`` repository (every push URL
names it on github.com); the refusal names both repositories.

It refuses, naming the reason, unless the worktree is clean and the last
``vrg-validate`` run (``.vergil/vrg-validate-last.json``) was a full run
that started clean at ``HEAD``, exited 0 and passed every stage. The base
SHA is the merge base of ``--base`` and ``HEAD``; the change class comes
from the files changed since it, against the allow-list committed there.

The builder reads the body of the ``--issue`` itself, and derives the
epic's open tasks from its native parent: when the parent carries ``epic``
and not ``ad-hoc``, its open sub-issues, every page, as ``{"ref",
"title"}``, without the task; otherwise none. It refuses when either cannot
be read.

It also reads the comments of the ``--issue`` itself, every page, and
keeps only those posted by the one human login of the trusted-authors
configuration, oldest first, as ``issue_comments`` (``{"author",
"created_at", "body"}``). An edited human comment is left out and counted
in ``issue_comments_excluded_edited``. It refuses when the configuration
lists no human login or several, or the comments cannot be read.

A check (``check`` or ``check-<k>``) also takes ``--fix-base``, the ``HEAD``
the previous step judged, refused unless it is the ``head_sha`` of that
step's stored request; one ``--earlier-result`` per earlier step, in
step order, each checked against that step's stored request; ``--fix-id``
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
from vergil_tooling.lib.merge_review import request as req
from vergil_tooling.lib.merge_review.change_class import classify_branch
from vergil_tooling.lib.merge_review.schema import STEP_RE, is_check
from vergil_tooling.lib.merge_review.state import StateDirError, ensure_private_dir
from vergil_tooling.lib.validate_record import ValidateRecordError, read_record

if TYPE_CHECKING:
    from vergil_tooling.lib.epics import IssueRef

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
        given = [name for name in (*building, *_CHECK_ONLY) if getattr(args, name)]
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
    base_sha, change_class = classify_branch(args.base, head)
    task = req.fetch_task_context(issue)
    design_sources = req.design_source_paths(args.design_source)
    issue_comments = req.fetch_human_comments(issue)
    fix_base_sha = _fix_base(args.fix_base, head) if is_check(step) else None

    private_dir = ensure_private_dir(git.main_worktree_root(), issue.number)
    check: req.CheckFields | None = None
    fix_patch = b""
    if fix_base_sha is not None:
        earlier = req.read_earlier_results(private_dir, step, args.earlier_result)
        req.check_fix_base(private_dir, step, fix_base_sha)
        finding_ids = req.earlier_finding_ids(
            [result for _path, result in earlier], args.fix_id, args.ruled_id
        )
        fix_patch = git.diff_patch(fix_base_sha, head)
        check = req.CheckFields(
            fix_base_sha=fix_base_sha,
            fix_diff_path=str(private_dir / req.fix_file(step)),
            earlier_result_paths=[path for path, _result in earlier],
            earlier_finding_ids=finding_ids,
        )
    diff_path = private_dir / req.diff_file(step)
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
            change_class=change_class,
            design_sources=design_sources,
            validate_stages=list(record.passed),
            epic_open_tasks=task.epic_open_tasks,
        ),
        check,
    )
    # The request names the diffs, so it is written last.
    atomic_write(diff_path, git.diff_patch(base_sha, head))
    if check is not None:
        atomic_write(Path(check.fix_diff_path), fix_patch)
    request_path = private_dir / req.request_file(step)
    atomic_write(request_path, json.dumps(request, indent=2) + "\n")
    return request_path


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
