"""The transport-agnostic state machine.

Pure functions over a WorkflowState: they mutate the passed state in place and
return it (the oracle loads a fresh state per CLI call, so there is no aliasing
across calls). All wall-clock and git facts are passed in as arguments, keeping
every function deterministic and unit-testable.
"""

from __future__ import annotations

from typing import Any

from vergil_tooling.lib.commit_message import find_autoclose
from vergil_tooling.lib.pr_workflow import registry
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import CHECK_STATUSES, MODES, WorkflowState


def _reject_autoclose(verb: str, **fields: str | None) -> None:
    """Reject any PR-metadata field that carries a GitHub auto-close keyword.

    On merge, GitHub auto-closes the linked issue when the PR body contains
    ``Closes/Fixes/Resolves #N`` — violating the fleet policy that an issue
    stays open until its post-merge workflows succeed. The structured
    ``--issue`` already emits ``Ref #N``; the free-text fields must never carry
    an issue-*closing* reference. Rejecting at entry (before state is written)
    keeps the keyword from ever reaching ``.vergil/pr-workflow.json`` or the
    rendered PR body; the submit-time check stays as defense-in-depth."""
    for flag, value in fields.items():
        if value is None:
            continue
        match = find_autoclose(value)
        if match:
            raise WorkflowError(
                f'{verb}: --{flag} contains an auto-close keyword ("{match}"). '
                "Issues must stay open until post-merge workflows succeed; the "
                'structured --issue already emits "Ref #N". '
                'Use "Ref #N" or drop the reference.'
            )


def init_state(
    *,
    issue: str,
    branch: str,
    base: str,
    mode: str,
    head_sha: str,
    base_sha: str,
    user_token: str,
    now: str,
) -> WorkflowState:
    """Create a fresh workflow. Paired starts owned by AUDIT (handshake
    rendezvous); solo starts owned by USER and skips the handshake."""
    if mode not in MODES:
        raise WorkflowError(f"invalid mode {mode!r}; must be one of {MODES}")
    owner = "user" if mode == "solo" else "audit"
    return WorkflowState(
        issue=str(issue),
        branch=branch,
        base=base,
        mode=mode,
        owner=owner,
        status="implementing",
        round=0,
        created_at=now,
        updated_at=now,
        participants={
            "user": {"token": user_token, "present_at": now},
            "audit": None,
        },
        git={"base_sha": base_sha, "head_sha": head_sha, "last_reviewed_sha": None},
        history=[{"round": 0, "at": now, "actor": "user", "action": "init", "mode": mode}],
    )


def audit_ack(state: WorkflowState, *, issue: str, audit_token: str, now: str) -> WorkflowState:
    """AUDIT confirms presence: it records its token and hands the turn back to
    USER. AUDIT is the current owner here, so this is an owner write."""
    if state.mode == "solo":
        raise WorkflowError("cannot audit a solo (--no-audit) workflow")
    if str(issue) != state.issue:
        msg = (
            f"issue mismatch: workflow file is for #{state.issue}, "
            f"you asked to audit #{issue} — are both sessions in the same worktree?"
        )
        raise WorkflowError(msg)
    state.participants["audit"] = {"token": audit_token, "present_at": now}
    state.owner = "user"
    state.updated_at = now
    state.history.append({"round": state.round, "at": now, "actor": "audit", "action": "ack"})
    return state


def _require_owner(state: WorkflowState, role: str) -> None:
    if state.owner != role:
        msg = f"out-of-turn: {role} cannot write while owner is {state.owner!r}"
        raise WorkflowError(msg)


def apply_report_ready(
    state: WorkflowState,
    *,
    title: str,
    summary: str,
    notes: str,
    linkage: str,
    head_sha: str,
    now: str,
) -> WorkflowState:
    """USER's initial done-signal. In paired mode it hands the turn to AUDIT; in
    solo mode there is no audit, so it goes straight to approved."""
    _require_owner(state, "user")
    _reject_autoclose("report-ready", title=title, summary=summary, notes=notes)
    state.pr_metadata = {"title": title, "summary": summary, "notes": notes, "linkage": linkage}
    state.git["head_sha"] = head_sha
    if state.mode == "solo":
        state.status = "approved"
        state.owner = "user"
    else:
        state.status = "reviewing"
        state.owner = "audit"
    state.updated_at = now
    state.history.append(
        {
            "round": state.round,
            "at": now,
            "actor": "user",
            "action": "report-ready",
            "head_sha": head_sha,
        }
    )
    return state


def apply_report_fixes(
    state: WorkflowState,
    *,
    head_sha: str,
    note: str | None,
    now: str,
    title: str | None = None,
    summary: str | None = None,
    notes: str | None = None,
    max_rounds: int = 10,
) -> WorkflowState:
    """USER reports it addressed findings. A round counts only if it carries
    something new to re-review: a new HEAD **or** a PR-metadata revision (so a
    ``pr-description-fidelity`` fail is actionable — the USER can rewrite the
    summary/notes/title with no code commit). An empty round (neither) is
    rejected so the loop cannot spin. When the new round exceeds ``max_rounds``
    the workflow auto-escalates to the human instead of looping forever (the
    runaway-round cap, spec §9). ``max_rounds`` is supplied by the CLI from
    ``settings.max_rounds``; the default keeps the engine usable standalone."""
    _require_owner(state, "user")
    _reject_autoclose("report-fixes", title=title, summary=summary, notes=notes)
    revisions = {
        key: value
        for key, value in (("title", title), ("summary", summary), ("notes", notes))
        if value is not None
    }
    new_head = head_sha != state.git.get("last_reviewed_sha")
    if not new_head and not revisions:
        raise WorkflowError(
            "no new commits and no metadata revision since the last review; nothing to re-review"
        )
    if revisions:
        state.pr_metadata = {**(state.pr_metadata or {}), **revisions}
    state.git["head_sha"] = head_sha
    state.round += 1
    state.updated_at = now
    entry: dict[str, Any] = {
        "round": state.round,
        "at": now,
        "actor": "user",
        "action": "report-fixes",
        "head_sha": head_sha,
    }
    if note:
        entry["note"] = note
    if revisions:
        entry["revised"] = sorted(revisions)
    state.history.append(entry)
    if state.round > max_rounds:
        state.owner = "human"
        state.status = "escalated"
        state.escalation = {
            "by": "user",
            "check": None,
            "reason": (
                f"runaway-round cap reached: round {state.round} exceeds max_rounds={max_rounds}"
            ),
            "raised_at": now,
        }
        return state
    state.status = "reviewing"
    state.owner = "audit"
    return state


def apply_submitted(
    state: WorkflowState, *, pr_url: str, pr_number: int | None, now: str
) -> WorkflowState:
    """Mark the workflow submitted after ``vrg-submit-pr`` opens the PR.

    The state file is retained (not deleted) so the worktree scanner can report
    the worktree as in-flight rather than re-submitting it. This is a terminal
    annotation written by the human's submit step, not an agent turn, so it does
    not require ownership and leaves ``status``/``owner`` untouched."""
    state.submitted = {"pr_url": pr_url, "pr_number": pr_number, "at": now}
    state.updated_at = now
    state.history.append(
        {
            "round": state.round,
            "at": now,
            "actor": "human",
            "action": "submitted",
            "pr_url": pr_url,
            "pr_number": pr_number,
        }
    )
    return state


def apply_error(state: WorkflowState, *, by: str, reason: str, now: str) -> WorkflowState:
    """Record a terminal error (a graceful give-up). The counterpart's wait
    detects ``state.error`` and stops with a complementary exception (spec §9)."""
    state.status = "error"
    state.error = {"by": by, "at": now, "reason": reason}
    state.updated_at = now
    state.history.append(
        {"round": state.round, "at": now, "actor": by, "action": "abort", "reason": reason}
    )
    return state


def rollup_status(checks: list[dict[str, Any]]) -> str:
    """Roll a check ledger up to a workflow status: any escalate -> escalated;
    else any fail -> changes-requested; else approved."""
    statuses = [c.get("status") for c in checks]
    if "escalate" in statuses:
        return "escalated"
    if "fail" in statuses:
        return "changes-requested"
    return "approved"


def next_pending_check(state: WorkflowState) -> str | None:
    """Return the first registry check not yet evaluated for the current round,
    or None once every check has a result for ``state.round``.

    The CLI hands these out one at a time so the audit agent's working set stays
    bounded to a single check prompt and a single result per round-trip — robust
    as the number (or complexity) of checks grows."""
    by_id = {c["id"]: c for c in state.checks}
    for cid in registry.check_ids():
        entry = by_id.get(cid)
        if entry is None or entry.get("round") != state.round:
            return cid
    return None


def apply_check(
    state: WorkflowState,
    *,
    check_id: str,
    status: str,
    findings: list[dict[str, Any]] | None,
    reason: str | None,
    head_sha: str,
    now: str,
) -> WorkflowState:
    """AUDIT submits one check's judgment. Once every check has a result for the
    current round, the oracle rolls the ledger up to the next owner/status."""
    _require_owner(state, "audit")
    if check_id not in registry.check_ids():
        known = sorted(registry.check_ids())
        raise WorkflowError(f"unknown check id {check_id!r}; known checks: {known}")
    if status not in CHECK_STATUSES:
        raise WorkflowError(f"check {check_id!r} has invalid status {status!r}")
    entry: dict[str, Any] = {"id": check_id, "status": status, "round": state.round}
    if findings:
        entry["findings"] = findings
    if reason:
        entry["reason"] = reason
    by_id = {c["id"]: c for c in state.checks}
    by_id[check_id] = entry
    state.checks = [by_id[cid] for cid in registry.check_ids() if cid in by_id]
    state.updated_at = now
    state.history.append(
        {
            "round": state.round,
            "at": now,
            "actor": "audit",
            "action": "submit-check",
            "check": check_id,
            "status": status,
        }
    )
    if next_pending_check(state) is None:
        _complete_review(state, head_sha=head_sha, now=now)
    return state


def _complete_review(state: WorkflowState, *, head_sha: str, now: str) -> None:
    rollup = rollup_status(state.checks)
    state.status = rollup
    state.git["last_reviewed_sha"] = head_sha
    if rollup == "escalated":
        state.owner = "human"
        escalated = next(c for c in state.checks if c.get("status") == "escalate")
        state.escalation = {
            "by": "audit",
            "check": escalated["id"],
            "reason": escalated.get("reason", ""),
            "raised_at": now,
        }
    else:
        state.owner = "user"
    state.history.append(
        {
            "round": state.round,
            "at": now,
            "actor": "audit",
            "action": "review-complete",
            "rollup": rollup,
        }
    )


def apply_escalate(state: WorkflowState, *, by: str, reason: str, now: str) -> WorkflowState:
    """USER or AUDIT escalates to the human. The escalator must hold the turn."""
    _require_owner(state, by)
    state.owner = "human"
    state.status = "escalated"
    state.escalation = {"by": by, "reason": reason, "raised_at": now}
    state.updated_at = now
    state.history.append(
        {"round": state.round, "at": now, "actor": by, "action": "escalate", "reason": reason}
    )
    return state


def apply_resolve(
    state: WorkflowState, *, to_role: str, note: str | None, now: str
) -> WorkflowState:
    """The human hands control back to an agent after an escalation."""
    if state.owner != "human":
        raise WorkflowError("cannot resolve: the workflow is not awaiting the human")
    if to_role not in ("user", "audit"):
        raise WorkflowError(f"invalid --to {to_role!r}; must be 'user' or 'audit'")
    state.owner = to_role
    state.status = "implementing" if to_role == "user" else "reviewing"
    state.escalation = None
    state.updated_at = now
    entry: dict[str, Any] = {
        "round": state.round,
        "at": now,
        "actor": "human",
        "action": "resolve",
        "to": to_role,
    }
    if note:
        entry["note"] = note
    state.history.append(entry)
    return state


def directive_for(state: WorkflowState, role: str) -> dict[str, Any]:
    """Return the single instruction the given role should act on next, or a
    DONE marker. Assumes it is already this role's turn (the transport blocks
    until then)."""
    if role == "user":
        return _user_directive(state)
    if role == "audit":
        check_id = next_pending_check(state)
        since = state.git.get("last_reviewed_sha")
        head = state.git["head_sha"]
        rng = f"{state.base}..{head}"
        focus = since or state.git["base_sha"]
        return {
            "phase": state.phase,
            "role": "audit",
            "round": state.round,
            "check": check_id,
            "do": (
                f"Review the cumulative delta {rng} (focus since {focus}) for the "
                f"'{check_id}' check only, then report its result."
            ),
            "range": rng,
            "since": since,
            "then": {"verb": "submit-check", "schema": "check.v1"},
        }
    raise WorkflowError(f"unknown role {role!r}")


def _user_directive(state: WorkflowState) -> dict[str, Any]:
    if state.status == "approved":
        return {"done": True, "reason": "approved", "next_human_action": "run vrg-submit-pr"}
    if state.pr_metadata is None:
        return {
            "phase": state.phase,
            "role": "user",
            "round": state.round,
            "do": (
                f"Implement issue #{state.issue} on branch {state.branch}. "
                "Validate green. Then report PR metadata."
            ),
            "then": {"verb": "report-ready", "schema": "pr-metadata.v1"},
        }
    if state.status == "changes-requested":
        findings: list[dict[str, Any]] = []
        for entry in state.checks:
            if entry.get("status") == "fail":
                for finding in entry.get("findings", []):
                    findings.append({"check": entry["id"], **finding})
        return {
            "phase": state.phase,
            "role": "user",
            "round": state.round,
            "do": "Address these findings, commit fixes, validate green, then report.",
            "findings": findings,
            "then": {"verb": "report-fixes"},
        }
    raise WorkflowError(f"no user directive for status {state.status!r}")
