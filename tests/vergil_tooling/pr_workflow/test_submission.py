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


def _write_state(root: Path, *, with_metadata: bool) -> None:
    state = engine.init_state(
        issue="1534",
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
        "title": "feat: x",
        "summary": "did x",
        "notes": "n",
        "linkage": "Ref",
        "base": "develop",
    }


def test_read_pr_fields_errors_when_state_has_no_metadata(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=False)
    with pytest.raises(WorkflowError, match="no PR metadata"):
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
