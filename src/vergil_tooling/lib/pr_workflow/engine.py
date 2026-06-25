"""The transport-agnostic state machine.

Pure functions over a WorkflowState: they mutate the passed state in place and
return it (the oracle loads a fresh state per CLI call, so there is no aliasing
across calls). All wall-clock and git facts are passed in as arguments, keeping
every function deterministic and unit-testable.

Run-and-done since #1872: a worktree initializes, records PR metadata, and is
marked submitted once the human opens the PR. No turn-taking, no audit.
"""

from __future__ import annotations

from vergil_tooling.lib.commit_message import find_autoclose
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState


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
    head_sha: str,
    base_sha: str,
    now: str,
) -> WorkflowState:
    """Create a fresh run-and-done workflow with no PR metadata yet."""
    return WorkflowState(
        issue=str(issue),
        branch=branch,
        base=base,
        status="implementing",
        created_at=now,
        updated_at=now,
        git={"base_sha": base_sha, "head_sha": head_sha},
    )


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
    """Record the PR metadata and mark the workflow ready to submit.

    Idempotent: there is no turn-taking to guard, so re-running overwrites the
    metadata. An agent can correct a mistaken title/summary by calling
    ``report-ready`` again any time before the human submits."""
    _reject_autoclose("report-ready", title=title, summary=summary, notes=notes)
    state.pr_metadata = {"title": title, "summary": summary, "notes": notes, "linkage": linkage}
    state.git["head_sha"] = head_sha
    state.status = "ready"
    state.updated_at = now
    return state


def apply_submitted(
    state: WorkflowState, *, pr_url: str, pr_number: int | None, now: str
) -> WorkflowState:
    """Mark the workflow submitted after ``vrg-submit-pr`` opens the PR.

    The state file is retained (not deleted) so the worktree scanner can report
    the worktree as in-flight rather than re-submitting it."""
    state.submitted = {"pr_url": pr_url, "pr_number": pr_number, "at": now}
    state.updated_at = now
    return state
