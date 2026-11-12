"""Tests for vergil_tooling.lib.pr_workflow.submission."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.pr_workflow import engine, submission
from vergil_tooling.lib.pr_workflow.errors import AlreadySubmittedError, WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-06-08T00:00:00Z"


def _write_state(root: Path, *, with_metadata: bool, issues: list[str] | None = None) -> None:
    state = engine.init_state(
        issues=issues or ["1534"],
        branch="feature/1534-x",
        base="develop",
        head_sha="h0",
        base_sha="b0",
        now=_NOW,
    )
    if with_metadata:
        engine.apply_report_ready(
            state,
            title="feat: x",
            summary="did x",
            notes="n",
            linkage="Ref",
            head_sha="h0",
            now=_NOW,
        )
    LocalFileTransport(root, base="develop").write(state)


def test_read_pr_fields_prefers_the_state_file(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=True)
    fields = submission.read_pr_fields(tmp_path)
    assert fields == {
        "issue": "1534",
        "issues": "1534",
        "title": "feat: x",
        "summary": "did x",
        "notes": "n",
        "linkage": "Ref",
        "base": "develop",
        "head_sha": "h0",
    }


def test_read_pr_fields_joins_a_batch_issue_list_in_order(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=True, issues=["1534", "1536", "1535"])
    fields = submission.read_pr_fields(tmp_path)
    assert fields["issue"] == "1534"
    assert fields["issues"] == "1534,1536,1535"


def test_read_pr_fields_refuses_a_state_with_no_reported_head(tmp_path: Path) -> None:
    """vrg-submit-pr holds the PR to the recorded head (issue #3815), so a
    ready state without one is refused rather than submitted at any head."""
    _write_state(tmp_path, with_metadata=True)
    state = LocalFileTransport(tmp_path, base="develop").read()
    assert state is not None
    state.git.pop("head_sha")
    LocalFileTransport(tmp_path, base="develop").write(state)
    with pytest.raises(WorkflowError, match="no reported head_sha"):
        submission.read_pr_fields(tmp_path)


def test_read_pr_fields_errors_when_state_has_no_metadata(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=False)
    with pytest.raises(WorkflowError, match="no PR metadata"):
        submission.read_pr_fields(tmp_path)


def test_read_pr_fields_excludes_unfrozen_worktree_that_kept_metadata(tmp_path: Path) -> None:
    """Regression for issue #2741: a worktree reported ready and then reopened
    via ``vrg-pr-workflow unfreeze`` drops to ``status == "implementing"`` while
    *retaining* its ``pr_metadata``. That mid-flight state must be classified as
    not-ready so ``vrg-submit-pr --all`` never sweeps it into a batch."""
    _write_state(tmp_path, with_metadata=True)
    state = LocalFileTransport(tmp_path, base="develop").read()
    assert state is not None
    engine.apply_unfreeze(state, now=_NOW)
    LocalFileTransport(tmp_path, base="develop").write(state)
    # Metadata is retained (unfreeze keeps it), but status is now implementing.
    assert state.status == "implementing"
    assert state.pr_metadata is not None
    with pytest.raises(WorkflowError, match="not ready"):
        submission.read_pr_fields(tmp_path)


def test_read_pr_fields_raises_when_state_file_absent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        submission.read_pr_fields(tmp_path)


def test_record_submission_retains_and_marks_the_state_file(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=True)
    submission.record_submission(tmp_path, pr_url="https://github.com/o/r/pull/312")
    # The file is kept (not deleted) so the scanner can report it as in-flight.
    state_path = tmp_path / ".vergil" / "pr-workflow.json"
    assert state_path.is_file()
    from vergil_tooling.lib.pr_workflow.state import WorkflowState

    state = WorkflowState.from_json(state_path.read_text())
    assert state.submitted is not None
    assert state.submitted["pr_url"] == "https://github.com/o/r/pull/312"
    assert state.submitted["pr_number"] == 312


def test_record_submission_makes_read_pr_fields_raise_already_submitted(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=True)
    submission.record_submission(tmp_path, pr_url="https://github.com/o/r/pull/312")
    with pytest.raises(AlreadySubmittedError) as exc:
        submission.read_pr_fields(tmp_path)
    assert exc.value.pr_number == 312
    assert exc.value.pr_url == "https://github.com/o/r/pull/312"


def test_record_submission_raises_when_state_file_absent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        submission.record_submission(tmp_path, pr_url="https://github.com/o/r/pull/9")
