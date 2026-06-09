"""Drive the local pre-PR workflow: the oracle CLI.

Both agent skills reduce to ``vrg-pr-workflow next`` (role resolved from
``vrg-whoami``); the directive names the report verb to call next. The oracle owns
every write, snapshots git itself, and blocks until it is the caller's turn. See
docs/specs/2026-06-08-pr-workflow-oracle-design.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, identity_mode
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.pr_workflow import engine, registry, settings
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


# All agent-to-agent waits are patient: the sequential workflow means a side may
# wait a long time for its counterpart, and a silent block reads as a hang, so the
# transport heartbeats while waiting (issue #1572). Both the timeout and the poll
# interval are env-overridable and read at call time — production keeps the long,
# once-a-second defaults; tests set a small timeout and a 0 poll so the suite
# never sleeps on real time (issue #1572).
def _wait_timeout() -> float:
    return float(os.environ.get("VRG_PR_WORKFLOW_TIMEOUT", "86400.0"))


def _poll_interval() -> float:
    return float(os.environ.get("VRG_PR_WORKFLOW_POLL_INTERVAL", "1.0"))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _require_state(transport: LocalFileTransport) -> WorkflowState:
    state = transport.read()
    if state is None:
        raise WorkflowError("no workflow file; run `vrg-pr-workflow next` first")
    return state


def _agent_role() -> str:
    """Resolve the calling agent's role (``user``/``audit``) from the identity
    mode — the CLI detects it the same way the agent does, so no `--as` flag."""
    mode = identity_mode.current_mode()
    if mode is IdentityMode.USER:
        return "user"
    if mode is IdentityMode.AUDIT:
        return "audit"
    raise WorkflowError(
        f"this command runs as the USER or AUDIT agent, but the resolved identity "
        f"is {mode.value!r}; check `vrg-whoami --explain`"
    )


def _require_human() -> None:
    mode = identity_mode.current_mode()
    if mode is not IdentityMode.HUMAN:
        raise WorkflowError(
            f"this command is human-only, but the resolved identity is {mode.value!r}; "
            "run it from a human-identity (host) shell"
        )


def cmd_next(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    if _agent_role() == "user":
        return _next_user(args, transport)
    return _next_audit(args, transport)


def _next_user(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        if not args.issue:
            raise WorkflowError("the first `next` (as USER) must pass --issue to initialize")
        mode = "solo" if args.no_audit else "paired"
        state = engine.init_state(
            issue=args.issue,
            branch=git.current_branch(),
            base=transport.base,
            mode=mode,
            head_sha=transport.head_sha(),
            base_sha=transport.merge_base(),
            user_token=_token("u"),
            now=_now(),
        )
        transport.write(state)
        if mode == "paired":
            state = transport.wait_until_owner(
                "user",
                timeout=_wait_timeout(),
                waiting_for="the audit agent to join — start it now in the audit window",
            )
    else:
        if args.issue and str(args.issue) != state.issue:
            raise WorkflowError(
                f"stale workflow file for issue #{state.issue}; you passed #{args.issue}. "
                "Delete .vergil/pr-workflow.json to start fresh."
            )
        if state.owner != "user":
            state = transport.wait_until_owner(
                "user", timeout=_wait_timeout(), waiting_for="the audit to finish its review"
            )
    _emit(engine.directive_for(state, "user"))
    return 0


def _next_audit(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        state = transport.wait_until_present(
            timeout=_wait_timeout(),
            waiting_for="the implement session to create the workflow file",
        )
    if state.mode == "solo":
        _emit(
            {"done": True, "reason": "solo", "note": "workflow running --no-audit; nothing to do"}
        )
        return 0
    if state.status == "approved":
        _emit({"done": True, "reason": "approved", "note": "review complete; the USER will submit"})
        return 0
    if state.participants.get("audit") is None:
        # The audit is launched against the worktree (it cd's into the path the
        # implement session handed off), so it need not know the issue number —
        # take it from the state when --issue is omitted (issue #1572).
        engine.audit_ack(
            state, issue=args.issue or state.issue, audit_token=_token("a"), now=_now()
        )
        transport.write(state)
    state = transport.wait_until_owner(
        "audit", timeout=_wait_timeout(), waiting_for="the user to report ready (your review turn)"
    )
    directive = engine.directive_for(state, "audit")
    directive["prompt"] = registry.check_prompt(directive["check"])
    _emit(directive)
    return 0


def cmd_report_ready(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    engine.apply_report_ready(
        state,
        title=args.title,
        summary=args.summary,
        notes=args.notes,
        linkage=args.linkage,
        head_sha=transport.head_sha(),
        now=_now(),
    )
    transport.write(state)
    _emit({"ok": True, "status": state.status, "owner": state.owner})
    return 0


def cmd_report_fixes(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    engine.apply_report_fixes(
        state,
        head_sha=transport.head_sha(),
        note=args.note,
        now=_now(),
        title=args.title,
        summary=args.summary,
        notes=args.notes,
        max_rounds=settings.max_rounds(transport.worktree_root),
    )
    transport.write(state)
    _emit({"ok": True, "round": state.round, "owner": state.owner})
    return 0


def cmd_submit_check(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    try:
        payload = json.loads(Path(args.payload).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot read check payload {args.payload!r}: {exc}") from exc
    engine.apply_check(
        state,
        check_id=payload.get("id"),
        status=payload.get("status"),
        findings=payload.get("findings"),
        reason=payload.get("reason"),
        head_sha=transport.head_sha(),
        now=_now(),
    )
    transport.write(state)
    _emit(
        {
            "ok": True,
            "status": state.status,
            "owner": state.owner,
            "pending": engine.next_pending_check(state),
        }
    )
    return 0


def cmd_escalate(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    by = _agent_role()
    state = _require_state(transport)
    engine.apply_escalate(state, by=by, reason=args.reason, now=_now())
    transport.write(state)
    _emit({"ok": True, "owner": state.owner})
    return 0


def cmd_abort(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    by = _agent_role()
    state = _require_state(transport)
    engine.apply_error(state, by=by, reason=args.reason, now=_now())
    transport.write(state)
    _emit({"ok": True, "status": state.status})
    return 0


def cmd_resolve(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    _require_human()
    state = _require_state(transport)
    engine.apply_resolve(state, to_role=args.to, note=args.note, now=_now())
    transport.write(state)
    _emit({"ok": True, "owner": state.owner})
    return 0


def cmd_status(_args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        _emit({"exists": False})
        return 0
    print(state.to_json())
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive the local pre-PR workflow oracle.")
    parser.add_argument("--base", default="origin/develop", help="Base ref for the delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p_next = sub.add_parser("next", help="Block until your turn, then print the next directive")
    p_next.add_argument("--issue", help="Issue number (required on the first call)")
    p_next.add_argument("--no-audit", action="store_true", help="Solo mode: skip the local audit")
    p_next.set_defaults(func=cmd_next)

    p_ready = sub.add_parser("report-ready", help="USER: initial done-signal with PR metadata")
    p_ready.add_argument("--title", required=True)
    p_ready.add_argument("--summary", required=True)
    p_ready.add_argument("--notes", required=True)
    p_ready.add_argument("--linkage", default="Ref")
    p_ready.set_defaults(func=cmd_report_ready)

    p_fixes = sub.add_parser("report-fixes", help="USER: report fixes for the last findings")
    p_fixes.add_argument("--note", default=None)
    p_fixes.add_argument("--title", default=None, help="Revise the PR title (e.g. for fidelity)")
    p_fixes.add_argument("--summary", default=None, help="Revise the PR summary")
    p_fixes.add_argument("--notes", default=None, help="Revise the PR notes")
    p_fixes.set_defaults(func=cmd_report_fixes)

    p_check = sub.add_parser("submit-check", help="AUDIT: submit one check's result")
    p_check.add_argument("--payload", required=True, help="Path to a check.v1 JSON file")
    p_check.set_defaults(func=cmd_submit_check)

    p_esc = sub.add_parser("escalate", help="Hand control to the human")
    p_esc.add_argument("--reason", required=True)
    p_esc.set_defaults(func=cmd_escalate)

    p_abort = sub.add_parser("abort", help="Record a terminal error (graceful give-up)")
    p_abort.add_argument("--reason", required=True)
    p_abort.set_defaults(func=cmd_abort)

    p_res = sub.add_parser("resolve", help="HUMAN: hand control back to an agent")
    p_res.add_argument("--to", required=True, choices=["user", "audit"])
    p_res.add_argument("--note", default=None)
    p_res.set_defaults(func=cmd_resolve)

    p_status = sub.add_parser("status", help="Print the current workflow state")
    p_status.set_defaults(func=cmd_status)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transport = LocalFileTransport(git.repo_root(), base=args.base, poll_interval=_poll_interval())
    try:
        return int(args.func(args, transport))
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
