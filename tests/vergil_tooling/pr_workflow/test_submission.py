"""Tests for vergil_tooling.lib.pr_workflow.submission."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import pr_template
from vergil_tooling.lib.pr_workflow import engine, submission
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-06-08T00:00:00Z"


def _write_state(root: Path, *, with_metadata: bool) -> None:
    state = engine.init_state(
        issue="1534",
        branch="feature/1534-x",
        base="develop",
        mode="solo",
        head_sha="h0",
        base_sha="b0",
        user_token="u-1",
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
    }


def test_read_pr_fields_errors_when_state_has_no_metadata(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=False)
    with pytest.raises(WorkflowError, match="no PR metadata"):
        submission.read_pr_fields(tmp_path)


def test_read_pr_fields_falls_back_to_template(tmp_path: Path) -> None:
    pr_template.write_template(
        tmp_path,
        issue="42",
        title="fix: y",
        summary="did y",
        notes="m",
    )
    fields = submission.read_pr_fields(tmp_path)
    assert fields["issue"] == "42"
    assert fields["title"] == "fix: y"


def test_read_pr_fields_raises_when_neither_exists(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        submission.read_pr_fields(tmp_path)


def test_delete_submission_removes_the_state_file(tmp_path: Path) -> None:
    _write_state(tmp_path, with_metadata=True)
    submission.delete_submission(tmp_path)
    assert not (tmp_path / ".vergil" / "pr-workflow.json").exists()


def test_delete_submission_removes_the_template_when_no_state(tmp_path: Path) -> None:
    pr_template.write_template(
        tmp_path,
        issue="42",
        title="fix: y",
        summary="did y",
        notes="m",
    )
    submission.delete_submission(tmp_path)
    assert not (tmp_path / ".vergil" / "pr-template.yml").exists()
