"""End-to-end tests driving the vrg-pr-workflow CLI as a subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

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
    _git(repo, "checkout", "-b", "feature/1534-x")
    (repo / "feature.py").write_text("x = 1\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "feature work")
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "vergil_tooling.bin.vrg_pr_workflow", "--base", "develop", *args),
        cwd=repo, capture_output=True, text=True,
    )


def test_solo_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    out = _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    assert out.returncode == 0, out.stderr
    directive = json.loads(out.stdout)
    assert directive["then"]["verb"] == "report-ready"

    out = _run(repo, "report-ready", "--title", "feat: x", "--summary", "did x", "--notes", "n")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["status"] == "approved"

    out = _run(repo, "next", "--as", "user")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "done": True, "reason": "approved", "next_human_action": "run vrg-submit-pr",
    }

    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["mode"] == "solo"
    assert state["history"][0]["action"] == "init"


def test_audit_on_solo_file_exits_clean(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "next", "--as", "audit", "--issue", "1534")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["reason"] == "solo"


def test_first_user_next_without_issue_errors(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    out = _run(repo, "next", "--as", "user")
    assert out.returncode == 1
    assert "must pass --issue" in out.stderr


def test_submit_review_with_bad_payload_errors(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "submit-review", "--payload", str(repo / "missing.json"))
    assert out.returncode == 1
    assert "review payload" in out.stderr


def test_user_next_rejects_stale_different_issue(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "next", "--as", "user", "--issue", "9999")
    assert out.returncode == 1
    assert "stale workflow file" in out.stderr


def test_abort_records_terminal_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "abort", "--as", "user", "--reason", "giving up")
    assert out.returncode == 0
    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["status"] == "error"
    assert state["error"]["reason"] == "giving up"
