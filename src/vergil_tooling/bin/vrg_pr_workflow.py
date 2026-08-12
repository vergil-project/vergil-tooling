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
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, git, github
from vergil_tooling.lib.linkage import (
    find_linkage_keyword,
    freetext_linkage_error,
    normalize_linkage,
)
from vergil_tooling.lib.pr_body import normalize_issue_ref
from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.github_transport import GitHubTransport
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _reject_cross_repo_issue(issue_ref: str) -> None:
    """Refuse a cross-repo --issue at report time — a PR closes only same-repo issues.

    Mirrors vrg-submit-pr's guard so the error surfaces where the value is entered
    (report time) rather than later at submit. A PR can only ``Closes`` an issue
    in its own repo; because issue numbers are not unique across repos, a
    cross-repo close would shut an unrelated same-numbered issue — a genuine
    mis-close hazard. Best-effort: if the current repo cannot be resolved (no
    remote, no gh auth — an offline run), defer silently to vrg-submit-pr's
    authoritative check. *issue_ref* is the canonical ref (``#N`` or
    ``owner/repo#N``); a same-repo ``#N`` resolves to the current repo and always
    passes; the compare is case-insensitive so a differently cased spelling of the
    current repo is not a false refusal.
    """
    try:
        current = github.current_repo()
    except (subprocess.CalledProcessError, OSError):
        return
    if not current:
        return
    # *issue_ref* is already the canonical form (normalize_issue_ref ran upstream)
    # and *current* is non-empty, so parse_issue_ref cannot raise here — a bare
    # number was the only unparseable shape, and it is now '#N' before this guard.
    ref = epics.parse_issue_ref(issue_ref, default_repo=current)
    if f"{ref.owner}/{ref.repo}".lower() != current.lower():
        raise WorkflowError(
            f"--issue ({ref.slug}) is in a different repo than this PR ({current}); "
            "a PR can only close an issue in its own repo. File the task in this repo "
            "and reference the other issue via a comment or a Ref line."
        )


def _reject_epic_issue(issue_ref: str) -> None:
    """Refuse an epic linkage at report time — a PR links a task, not an epic.

    Mirrors vrg-submit-pr's guard so the error surfaces where the value is
    entered (report time) rather than later at submit time. *issue_ref* is the
    canonical ref (``#N`` or ``owner/repo#N``) so a bare number no longer slips
    past ``parse_issue_ref`` (#2213). Best-effort: if epic-ness cannot be
    determined (no remote, no gh auth — e.g. an offline run), it defers silently
    to vrg-submit-pr's authoritative check rather than blocking report-ready.
    """
    try:
        links_epic = epics.is_epic_linkage(issue_ref, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError):
        return
    if links_epic:
        raise WorkflowError(
            f"--issue links an epic ({issue_ref}); link a task, not an epic "
            "(epics are closed by rollup when their tasks complete)."
        )


def _reject_operational_issue(issue_ref: str) -> None:
    """Refuse an operational-task linkage at report time — it is not PR-workable.

    Mirrors vrg-submit-pr's guard so the error surfaces where the value is
    entered. *issue_ref* is the canonical ref (``#N`` or ``owner/repo#N``) so a
    bare number no longer slips past ``parse_issue_ref`` (#2213). Best-effort: if
    operational-ness cannot be determined (no remote, no gh auth — e.g. an offline
    run), defer silently to vrg-submit-pr's authoritative check rather than
    blocking report-ready.
    """
    try:
        is_operational = epics.is_operational_task(issue_ref, default_repo=github.current_repo())
    except (subprocess.CalledProcessError, OSError):
        return
    if is_operational:
        raise WorkflowError(
            f"--issue ({issue_ref}) is an operational task (validation/deployment), which "
            "is not PR-workable; record the Outcome as a comment (issue-validate / "
            "issue-deploy) instead of preparing a PR."
        )


def _push_relay_ref(state: WorkflowState, base: str) -> None:
    """Mirror the ready state onto the reserved relay ref, after the local write.

    Unconditional by design (Task 3, epic vergil-project/.github#148): every
    ``report-ready`` also force-pushes the state to
    ``refs/vergil/pr-workflow/<branch>`` via ``GitHubTransport`` — no
    off-platform detection — so a cloud VM's report-ready is visible to the Mac
    that later runs ``vrg-submit-pr`` (the two never share a disk). The durable
    local file has already been written and stays put; a push failure is
    therefore surfaced loudly on stderr but never rolls the local state back.

    ``report-ready`` speaks a JSON-on-stdout contract; ``GitHubTransport.write``
    is quiet-by-design (#2793), routing the relay push's git chatter to stderr
    itself, so no caller-side ``redirect_stdout`` workaround is needed here to
    keep stdout a pure JSON document.
    """
    relay = GitHubTransport(git.current_branch(), base=base)
    try:
        relay.write(state)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(
            f"warning: could not push pr-workflow relay ref {relay.ref}: {exc}",
            file=sys.stderr,
        )


def cmd_report_ready(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    # Resolve --issue to a canonical ref (bare number -> '#N') before the guards,
    # mirroring vrg-submit-pr. The guards call epics.parse_issue_ref, which rejects
    # a bare number (no '#') and would silently return False — letting a
    # bare-number epic/operational issue bypass the report-time guard (#2213). The
    # raw args.issue is still what gets *stored* (vrg-submit-pr's resolve_issue_ref
    # re-normalizes it and rejects a leading '#'), so only the guards see the
    # canonical form.
    try:
        issue_ref = normalize_issue_ref(str(args.issue))
    except ValueError as exc:
        raise WorkflowError(f"report-ready: {exc}") from exc
    _reject_cross_repo_issue(issue_ref)
    _reject_epic_issue(issue_ref)
    _reject_operational_issue(issue_ref)
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
    _push_relay_ref(state, args.base)
    response: dict[str, object] = {"ok": True, "status": state.status}
    if linkage_warning:
        response["warning"] = linkage_warning
    _emit(response)
    return 0


def cmd_unfreeze(_args: argparse.Namespace, transport: LocalFileTransport) -> int:
    """Deliberately reopen a frozen (reported-ready) branch for more commits.

    The sanctioned escape hatch from the post-report-ready freeze: it is a
    distinct, explicit subcommand precisely so reopening a branch is never a
    silent side effect of another action.
    """
    state = transport.read()
    if state is None:
        raise WorkflowError(
            "no workflow file to unfreeze; run report-ready first (there is nothing frozen here)."
        )
    engine.apply_unfreeze(state, now=_now())
    transport.write(state)
    _emit({"ok": True, "status": state.status})
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

    p_unfreeze = sub.add_parser(
        "unfreeze",
        help="Deliberately reopen a frozen (reported-ready) branch for more commits",
    )
    p_unfreeze.set_defaults(func=cmd_unfreeze)

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
