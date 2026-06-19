"""Bridge ``vrg-submit-pr`` to the PR workflow state file.

``read_pr_fields`` returns the submission fields from the oracle's state file
(``.vergil/pr-workflow.json``); that file is the sole source.

After a successful submission the state file is *retained* and marked submitted
(``record_submission``), not deleted, so the worktree scanner can report the
worktree as in-flight rather than as "not ready". The file lives inside the
worktree (``.vergil/`` is git-ignored), so it is cleaned up for free when
``vrg-finalize-pr`` removes the worktree once the branch lands.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import AlreadySubmittedError, WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.state import WorkflowState

if TYPE_CHECKING:
    from pathlib import Path

_DIR = ".vergil"
_STATE_FILE = "pr-workflow.json"


def _state_path(worktree_root: Path) -> Path:
    return worktree_root / _DIR / _STATE_FILE


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pr_number_from_url(pr_url: str) -> int | None:
    """Parse the trailing PR number from a GitHub PR URL (``.../pull/312``)."""
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def read_pr_fields(worktree_root: Path) -> dict[str, str]:
    """Return the PR submission fields (``issue``/``title``/``summary``/``notes``/
    ``linkage``/``base``) from the workflow state file.

    The ``base`` key carries the base ref the oracle recorded for this branch
    (e.g. ``origin/develop``) so submission targets the branch the agent
    intended rather than re-inferring it from the branch name.

    Raises ``FileNotFoundError`` if the state file does not exist,
    ``AlreadySubmittedError`` if it is marked submitted (its PR is in flight),
    and ``WorkflowError`` if it carries no PR metadata yet (the USER agent must
    ``report-ready`` first).
    """
    path = _state_path(worktree_root)
    if not path.is_file():
        raise FileNotFoundError(path)
    state = WorkflowState.from_json(path.read_text())
    if state.submitted is not None:
        raise AlreadySubmittedError(
            pr_url=state.submitted.get("pr_url", ""),
            pr_number=state.submitted.get("pr_number"),
        )
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


def record_submission(worktree_root: Path, *, pr_url: str) -> None:
    """Record that the worktree's PR was submitted: mark the state file
    submitted (retain it) so the scanner can report the worktree as in-flight.

    Raises ``FileNotFoundError`` if the state file is absent — submission is
    only recorded after ``read_pr_fields`` has already read it, so a missing
    file here is a programming error, not a normal path.
    """
    path = _state_path(worktree_root)
    if not path.is_file():
        raise FileNotFoundError(path)
    state = WorkflowState.from_json(path.read_text())
    engine.apply_submitted(
        state,
        pr_url=pr_url,
        pr_number=_pr_number_from_url(pr_url),
        now=_now(),
    )
    LocalFileTransport(worktree_root, base=state.base).write(state)
