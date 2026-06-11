"""Bridge ``vrg-submit-pr`` to the PR workflow state file.

The oracle's state file (``.vergil/pr-workflow.json``) subsumes the legacy
``.vergil/pr-template.yml``. ``read_pr_fields`` returns the submission fields from
the state file when it exists, falling back to the legacy template so both flows
work during the transition; ``delete_submission`` removes whichever was used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import pr_template
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState

if TYPE_CHECKING:
    from pathlib import Path

_DIR = ".vergil"
_STATE_FILE = "pr-workflow.json"


def _state_path(worktree_root: Path) -> Path:
    return worktree_root / _DIR / _STATE_FILE


def read_pr_fields(worktree_root: Path) -> dict[str, str]:
    """Return the PR submission fields (``issue``/``title``/``summary``/``notes``/
    ``linkage``/``base``) from the workflow state file if present, else the
    legacy ``pr-template.yml``.

    The ``base`` key carries the base ref the oracle recorded for this branch
    (e.g. ``origin/develop``) so submission targets the branch the agent
    intended rather than re-inferring it from the branch name. The legacy
    template path omits ``base`` (it never recorded one).

    Raises ``FileNotFoundError`` if neither exists, and ``WorkflowError`` if the
    state file carries no PR metadata yet (the USER agent must ``report-ready``
    first).
    """
    path = _state_path(worktree_root)
    if path.is_file():
        state = WorkflowState.from_json(path.read_text())
        meta = state.pr_metadata
        if meta is None:
            raise WorkflowError(
                "the workflow has no PR metadata yet; the USER agent must run "
                "`report-ready` before the PR can be submitted"
            )
        return {
            "issue": state.issue,
            "title": meta["title"],
            "summary": meta["summary"],
            "notes": meta.get("notes", ""),
            "linkage": meta.get("linkage", "Ref"),
            "base": state.base,
        }
    return pr_template.read_template(worktree_root)


def delete_submission(worktree_root: Path) -> None:
    """Delete the workflow state file if present, else the legacy template."""
    path = _state_path(worktree_root)
    if path.is_file():
        path.unlink()
        return
    pr_template.delete_template(worktree_root)
