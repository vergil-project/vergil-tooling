"""End-to-end tests for the run-and-done vrg-pr-workflow CLI (#1872)."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_pr_workflow

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base commit")
    _git(repo, "checkout", "-b", "feature/42-x")
    (repo / "feature.py").write_text("x = 1\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "feature work")
    return repo


@pytest.fixture()
def in_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temp repo on a feature branch and chdir into it."""
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    return repo


def test_report_ready_initializes_and_records(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # in_git_repo: fixture that chdirs into a temp repo on a feature branch with
    # develop branch reachable as a local branch.
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "status": "ready"}

    rc = vrg_pr_workflow.main(["--base", "develop", "status"])
    assert rc == 0
    state = json.loads(capsys.readouterr().out)
    assert state["issue"] == "42"
    assert state["status"] == "ready"
    assert state["pr_metadata"]["title"] == "t"


def test_report_ready_rejects_epic(in_git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Guard parity with vrg-submit-pr: report-ready refuses an epic linkage at
    # the point the value is entered, instead of only failing later at submit.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_epic_linkage", return_value=True),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "72",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    assert "epic" in capsys.readouterr().err.lower()


def test_report_ready_rejects_operational_task(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Guard parity with vrg-submit-pr: report-ready refuses an operational task —
    # it is not PR-workable (its result is a comment, not a PR).
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_epic_linkage", return_value=False),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_operational_task", return_value=True),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "120",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    assert "operational task" in capsys.readouterr().err.lower()


def test_report_ready_allows_non_epic_task(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The guard resolves the repo and finds a task (not an epic): proceeds.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_epic_linkage", return_value=False),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}


def test_report_ready_defers_when_epicness_unresolvable(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If GitHub is unreachable, the guard defers (does not block report-ready);
    # vrg-submit-pr's authoritative check still catches a real epic later.
    with patch(
        "vergil_tooling.bin.vrg_pr_workflow.github.current_repo",
        side_effect=subprocess.CalledProcessError(1, "gh"),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "ready"}


def test_report_ready_rerun_overwrites(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t1",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    capsys.readouterr()
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t2",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    capsys.readouterr()
    vrg_pr_workflow.main(["--base", "develop", "status"])
    state = json.loads(capsys.readouterr().out)
    assert state["pr_metadata"]["title"] == "t2"


def test_report_ready_rejects_stale_issue(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    capsys.readouterr()
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "99",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )
    assert rc == 1  # stale workflow file guard


def test_status_with_no_file(in_git_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = vrg_pr_workflow.main(["--base", "develop", "status"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"exists": False}


def test_report_ready_rejects_invalid_linkage_keyword(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
            "--linkage",
            "Refs #42",
        ]
    )
    assert rc == 1
    assert "error" in capsys.readouterr().err


def test_report_ready_strips_issue_number_from_linkage_and_warns(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            "42",
            "--title",
            "t",
            "--summary",
            "s",
            "--notes",
            "n",
            "--linkage",
            "Ref #99",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert "warning" in out


def test_report_ready_rejects_linkage_keyword_in_notes(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_epic_linkage", return_value=False),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "42",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "Ref #157",
            ]
        )
    assert rc == 1
    assert "Ref #157" in capsys.readouterr().err
