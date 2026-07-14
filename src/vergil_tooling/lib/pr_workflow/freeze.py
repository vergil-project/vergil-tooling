"""Post-report-ready freeze: a branch reported ready is done — never touch it.

Once ``report-ready`` records the PR metadata, the worktree's branch is the
single deliverable for its issue and must not change before the human submits.
Committing to (or advancing) it afterwards produces the flagship straggler
(issue #1719): the PR merges at the reported commit, the branch tip moves past
it, and worktree cleanup — correctly — refuses to delete unmerged work, stranding
the tree forever.

This module is the shared predicate + message builder for that freeze. The
enforcement chokepoints (``vrg-commit`` and the ``vrg-git`` push path) call
:func:`check_worktree`; the pure helpers are unit-testable in isolation.

"Frozen" mirrors ``worktrees._probe_pr_workflow``'s ``pr_prepared`` — the exact
``vrg-submit-pr`` ready gate: ``status == "ready"`` and not yet ``submitted``.
The only sanctioned way to lift it is the deliberate ``vrg-pr-workflow unfreeze``
action (``engine.apply_unfreeze``), which drops ``status`` back to
``implementing``; correcting PR prose by re-running ``report-ready`` is metadata,
not code, and stays allowed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.pr_workflow.state import WorkflowState


@dataclass(frozen=True)
class FreezeCheck:
    """The result of checking whether a worktree is frozen against mutation.

    - ``frozen`` — the branch was reported ready and not yet submitted; the
      caller must refuse the commit/push.
    - ``message`` — the actionable refusal text (``None`` when not frozen).
    - ``read_error`` — a captured (never swallowed) reason the state file could
      not be read; the caller surfaces it as a warning and proceeds rather than
      hard-blocking legitimate work over a corrupt file.
    - ``drifted`` — HEAD has already advanced past the reported commit; the
      straggler state, flagged loudly in ``message``.
    """

    frozen: bool
    message: str | None = None
    read_error: str | None = None
    drifted: bool = False


def is_frozen(state: WorkflowState | None) -> bool:
    """Return True when *state* marks the worktree frozen after report-ready.

    Frozen == ``status == "ready"`` and not yet ``submitted`` — the same signal
    ``worktrees._probe_pr_workflow`` reports as ``pr_prepared`` and the gate
    ``vrg-submit-pr`` uses to pick up a ready worktree. A deliberate
    ``unfreeze`` drops ``status`` back to ``implementing``, so this returns
    False again without discarding the recorded PR metadata.
    """
    if state is None:
        return False
    return state.status == "ready" and state.submitted is None


def has_drifted(state: WorkflowState, current_head: str | None) -> bool:
    """Return True when HEAD has moved past the commit ``report-ready`` recorded.

    A missing recorded ``head_sha`` or an unresolvable current HEAD yields False
    — drift is asserted only on positive evidence that the two SHAs differ.
    """
    recorded = state.git.get("head_sha")
    if not recorded or not current_head:
        return False
    return bool(recorded != current_head)


def build_refusal(state: WorkflowState, *, action: str, current_head: str | None) -> str:
    """Build the actionable refusal message for a frozen-branch mutation.

    *action* is the verb phrase for what is being refused (e.g. ``"add a
    commit"``). Assumes the caller already confirmed :func:`is_frozen`.
    """
    lines = [
        f"vrg: refusing to {action} — branch reported ready for issue "
        f"#{state.issue} and is FROZEN.",
        "",
        "A task is exactly one PR: once its work is reported ready the branch is",
        "done and must not change before the human submits it.",
        "",
        "More work is a NEW follow-up issue, never a change to this branch:",
        '  vrg-issue-create --repo <owner>/<repo> --kind task --title "<what>"',
        "  (or file it under the epic)",
        "",
        "Correcting the PR title/summary/notes is still fine — just re-run",
        "vrg-pr-workflow report-ready (it overwrites the metadata).",
        "",
        "If this branch genuinely must reopen for more commits (rare, deliberate),",
        "explicitly unfreeze it first:",
        "  vrg-pr-workflow unfreeze",
    ]
    if has_drifted(state, current_head):
        recorded = str(state.git.get("head_sha") or "unknown")
        lines[0:0] = [
            f"vrg: DRIFT DETECTED — HEAD ({(current_head or 'unknown')[:8]}) has already "
            f"moved past the commit reported ready ({recorded[:8]}).",
            "This is the reused-branch straggler (issue #1719): the merged PR still",
            f"points at {recorded[:8]}, so worktree cleanup will refuse this tree. Do not",
            "push it; open a follow-up issue for any further work and unfreeze only if",
            "you deliberately intend to reopen this branch.",
            "",
        ]
    return "\n".join(lines)


def _current_head(worktree_root: Path) -> str | None:
    """Resolve *worktree_root*'s HEAD SHA best-effort (None if unresolvable).

    Drift detection is advisory: an unresolvable HEAD must not turn a real
    freeze into a silent pass, so failure here only drops the drift note, never
    the refusal itself.
    """
    try:
        return git.read_output("-C", str(worktree_root), "rev-parse", "HEAD")
    except (subprocess.CalledProcessError, OSError):
        return None


def check_worktree(worktree_root: Path, *, action: str) -> FreezeCheck:
    """Read *worktree_root*'s workflow state and report its freeze status.

    A read error is captured in ``read_error`` (never swallowed) with
    ``frozen=False`` so the caller can warn and proceed rather than hard-block
    every commit on a corrupt state file.
    """
    try:
        state = LocalFileTransport(worktree_root).read()
    except (WorkflowError, OSError) as exc:
        return FreezeCheck(frozen=False, read_error=str(exc))
    if state is None or not is_frozen(state):
        return FreezeCheck(frozen=False)
    current_head = _current_head(worktree_root)
    drifted = has_drifted(state, current_head)
    message = build_refusal(state, action=action, current_head=current_head)
    return FreezeCheck(frozen=True, message=message, drifted=drifted)
