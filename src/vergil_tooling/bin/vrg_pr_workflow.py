"""Record PR metadata for the human's submit step: the oracle CLI.

Run-and-done since #1872. The implementing agent calls ``report-ready`` when its
work is green; that writes ``.vergil/pr-workflow.json`` with the PR metadata,
and ``vrg-submit-pr`` (human-run) reads it. ``status`` prints the current state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime

from vergil_tooling.lib import epics, git, github
from vergil_tooling.lib.linkage import (
    find_linkage_keyword,
    freetext_linkage_error,
    normalize_linkage,
)
from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _reject_epic_issue(issue: str) -> None:
    """Refuse an epic linkage at report time — a PR links a task, not an epic.

    Mirrors vrg-submit-pr's guard so the error surfaces where the value is
    entered (report time) rather than later at submit time. Best-effort: if
    epic-ness cannot be determined (no remote, no gh auth — e.g. an offline
    run), it defers silently to vrg-submit-pr's authoritative check rather than
    blocking report-ready.
    """
    try:
        links_epic = epics.is_epic_linkage(issue, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError):
        return
    if links_epic:
        raise WorkflowError(
            f"--issue links an epic (#{issue}); link a task, not an epic "
            "(epics are closed by rollup when their tasks complete)."
        )


def _reject_validation_issue(issue: str) -> None:
    """Refuse a validation-task linkage at report time — it is not PR-workable.

    Mirrors vrg-submit-pr's guard so the error surfaces where the value is
    entered. Best-effort: if validation-ness cannot be determined (no remote, no
    gh auth — e.g. an offline run), defer silently to vrg-submit-pr's
    authoritative check rather than blocking report-ready.
    """
    try:
        is_validation = epics.is_validation_task(issue, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError):
        return
    if is_validation:
        raise WorkflowError(
            f"--issue (#{issue}) is a validation task, which is not PR-workable; "
            "record the result as a comment (issue-validate skill) instead of "
            "preparing a PR."
        )


def cmd_report_ready(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    _reject_epic_issue(str(args.issue))
    _reject_validation_issue(str(args.issue))
    try:
        linkage, linkage_warning = normalize_linkage(args.linkage)
    except ValueError as exc:
        raise WorkflowError(f"report-ready: {exc}") from exc
    for value in (args.notes, args.summary):
        found = find_linkage_keyword(value)
        if found:
            raise WorkflowError(freetext_linkage_error(found, str(args.issue)))
    state = transport.read()
    if state is None:
        state = engine.init_state(
            issue=args.issue,
            branch=git.current_branch(),
            base=transport.base,
            head_sha=transport.head_sha(),
            base_sha=transport.merge_base(),
            now=_now(),
        )
    elif str(args.issue) != state.issue:
        raise WorkflowError(
            f"stale workflow file for issue #{state.issue}; you passed #{args.issue}. "
            "Delete .vergil/pr-workflow.json to start fresh."
        )
    engine.apply_report_ready(
        state,
        title=args.title,
        summary=args.summary,
        notes=args.notes,
        linkage=linkage,
        head_sha=transport.head_sha(),
        now=_now(),
    )
    transport.write(state)
    response: dict[str, object] = {"ok": True, "status": state.status}
    if linkage_warning:
        response["warning"] = linkage_warning
    _emit(response)
    return 0


def cmd_status(_args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        _emit({"exists": False})
        return 0
    print(state.to_json())
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record PR metadata for the human submit step.")
    parser.add_argument("--base", default="origin/develop", help="Base ref for the delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ready = sub.add_parser("report-ready", help="Record the PR metadata for this worktree")
    p_ready.add_argument("--issue", required=True)
    p_ready.add_argument("--title", required=True)
    p_ready.add_argument("--summary", required=True)
    p_ready.add_argument("--notes", required=True)
    p_ready.add_argument("--linkage", default="Ref")
    p_ready.set_defaults(func=cmd_report_ready)

    p_status = sub.add_parser("status", help="Print the current workflow state")
    p_status.set_defaults(func=cmd_status)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transport = LocalFileTransport(git.repo_root(), base=args.base)
    try:
        return int(args.func(args, transport))
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
