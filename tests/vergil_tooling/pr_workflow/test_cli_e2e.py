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


def test_report_ready_pushes_relay_ref(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Task 3 (#2367): report-ready always mirrors the ready state onto the relay
    # ref via GitHubTransport, in addition to the local file write. The trigger
    # is unconditional (no off-platform detection).
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as mock_transport:
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
    # The relay was written with the ready state, after the local write.
    mock_transport.assert_called_once()
    write = mock_transport.return_value.write
    write.assert_called_once()
    (pushed_state,) = write.call_args.args
    assert pushed_state.status == "ready"
    assert pushed_state.pr_metadata is not None
    assert pushed_state.pr_metadata["title"] == "t"
    # And the durable local file was written too.
    assert (in_git_repo / ".vergil" / "pr-workflow.json").is_file()


def test_report_ready_relay_push_failure_surfaces_but_local_persists(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A relay push failure surfaces loudly on stderr but never undoes the durable
    # local write (which happens first and stays): report-ready still succeeds
    # and .vergil/pr-workflow.json still holds the ready state.
    with patch("vergil_tooling.bin.vrg_pr_workflow.GitHubTransport") as mock_transport:
        mock_transport.return_value.ref = "refs/vergil/pr-workflow/feature/42-x"
        mock_transport.return_value.write.side_effect = subprocess.CalledProcessError(
            1, ("git", "push")
        )
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
    captured = capsys.readouterr()
    # Local write is durable: the command still succeeds and stdout is unchanged.
    assert rc == 0
    assert json.loads(captured.out) == {"ok": True, "status": "ready"}
    # The push failure surfaced loudly on stderr.
    assert "warning" in captured.err.lower()
    assert "relay ref" in captured.err.lower()
    # And the local state file persists with the ready metadata.
    state_path = in_git_repo / ".vergil" / "pr-workflow.json"
    assert state_path.is_file()
    persisted = json.loads(state_path.read_text())
    assert persisted["status"] == "ready"
    assert persisted["pr_metadata"]["title"] == "t"


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


def test_report_ready_rejects_cross_repo_issue(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Guard parity with vrg-submit-pr: report-ready refuses a cross-repo --issue.
    # A PR can only close an issue in its own repo, and because issue numbers are
    # not unique across repos, a cross-repo close is a genuine mis-close hazard.
    # The check runs first, so no epic/operational gh call hits the foreign repo.
    with patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "org/.github#127",
                "--title",
                "t",
                "--summary",
                "s",
                "--notes",
                "n",
            ]
        )
    assert rc == 1
    assert "different repo" in capsys.readouterr().err.lower()


def test_report_ready_defers_cross_repo_when_repo_unresolvable(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If the current repo cannot be resolved (offline), the cross-repo guard
    # defers rather than blocking; vrg-submit-pr's check still catches it later.
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
                "org/.github#127",
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


def test_report_ready_defers_cross_repo_when_repo_empty(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty current repo (nothing to compare against) defers rather than
    # blocking; a bare same-repo issue then proceeds normally.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value=""),
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


def test_report_ready_allows_explicit_same_repo_ref(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An explicit owner/repo#N matching the current repo passes the cross-repo
    # guard (same repo) and proceeds.
    with (
        patch("vergil_tooling.bin.vrg_pr_workflow.github.current_repo", return_value="org/repo"),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_epic_linkage", return_value=False),
        patch("vergil_tooling.bin.vrg_pr_workflow.epics.is_operational_task", return_value=False),
    ):
        rc = vrg_pr_workflow.main(
            [
                "--base",
                "develop",
                "report-ready",
                "--issue",
                "org/repo#42",
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


def _report_ready(issue: str = "42", title: str = "t") -> None:
    vrg_pr_workflow.main(
        [
            "--base",
            "develop",
            "report-ready",
            "--issue",
            issue,
            "--title",
            title,
            "--summary",
            "s",
            "--notes",
            "n",
        ]
    )


def test_unfreeze_reopens_after_report_ready(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # After report-ready the worktree is frozen (status ready); unfreeze drops it
    # back to implementing so commits are allowed again, retaining the metadata.
    _report_ready()
    capsys.readouterr()
    rc = vrg_pr_workflow.main(["--base", "develop", "unfreeze"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "implementing"}

    vrg_pr_workflow.main(["--base", "develop", "status"])
    state = json.loads(capsys.readouterr().out)
    assert state["status"] == "implementing"
    assert state["pr_metadata"]["title"] == "t"


def test_unfreeze_with_no_file_errors(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = vrg_pr_workflow.main(["--base", "develop", "unfreeze"])
    assert rc == 1
    assert "no workflow file" in capsys.readouterr().err.lower()


def test_report_ready_after_unfreeze_refreezes(
    in_git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The full deliberate-unfreeze round trip: ready -> unfreeze -> report-ready
    # refreshes the metadata and re-freezes (status ready).
    _report_ready(title="t1")
    vrg_pr_workflow.main(["--base", "develop", "unfreeze"])
    _report_ready(title="t2")
    capsys.readouterr()
    vrg_pr_workflow.main(["--base", "develop", "status"])
    state = json.loads(capsys.readouterr().out)
    assert state["status"] == "ready"
    assert state["pr_metadata"]["title"] == "t2"


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
