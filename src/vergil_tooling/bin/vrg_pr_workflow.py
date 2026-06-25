"""Record PR metadata for the human's submit step: the oracle CLI.

Run-and-done since #1872. The implementing agent calls ``report-ready`` when its
work is green; that writes ``.vergil/pr-workflow.json`` with the PR metadata,
and ``vrg-submit-pr`` (human-run) reads it. ``status`` prints the current state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from vergil_tooling.lib import git
from vergil_tooling.lib.linkage import normalize_linkage
from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def cmd_report_ready(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    try:
        linkage, linkage_warning = normalize_linkage(args.linkage)
    except ValueError as exc:
        raise WorkflowError(f"report-ready: {exc}") from exc
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
