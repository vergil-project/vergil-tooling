"""End-to-end tests driving the vrg-pr-workflow CLI in-process via cli.main().

In-process (not subprocess) so coverage is measured: each call exercises
parse_args, main, the cmd_* dispatch, and the real-git head_sha/merge_base.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.bin import vrg_pr_workflow as cli
from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.registry import check_ids

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_NOW = "2026-06-08T00:00:00Z"


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


def _run(monkeypatch: pytest.MonkeyPatch, repo: Path, *args: str) -> int:
    monkeypatch.chdir(repo)
    return cli.main(["--base", "develop", *args])


def _seed(repo: Path, *, owner: str, status: str, last_reviewed: str | None = None) -> None:
    """Write a workflow state directly, for verbs that need a specific turn."""
    state = engine.init_state(
        issue="1534",
        branch="feature/1534-x",
        base="develop",
        mode="paired",
        head_sha="seed",
        base_sha="seed",
        user_token="u-1",
        now=_NOW,
    )
    state.owner = owner
    state.status = status
    state.git["last_reviewed_sha"] = last_reviewed
    LocalFileTransport(repo, base="develop").write(state)


def test_solo_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    repo = _init_repo(tmp_path)

    assert _run(monkeypatch, repo, "next", "--as", "user", "--issue", "1534", "--no-audit") == 0
    assert json.loads(capsys.readouterr().out)["then"]["verb"] == "report-ready"

    assert (
        _run(
            monkeypatch,
            repo,
            "report-ready",
            "--title",
            "feat: x",
            "--summary",
            "did x",
            "--notes",
            "n",
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "approved"

    assert _run(monkeypatch, repo, "next", "--as", "user") == 0
    assert json.loads(capsys.readouterr().out) == {
        "done": True,
        "reason": "approved",
        "next_human_action": "run vrg-submit-pr",
    }

    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["mode"] == "solo"
    assert state["history"][0]["action"] == "init"


def test_audit_on_solo_file_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _run(monkeypatch, repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    capsys.readouterr()
    assert _run(monkeypatch, repo, "next", "--as", "audit", "--issue", "1534") == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "solo"


def test_first_user_next_without_issue_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    assert _run(monkeypatch, repo, "next", "--as", "user") == 1
    assert "must pass --issue" in capsys.readouterr().err


def test_submit_review_with_bad_payload_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="audit", status="reviewing")
    assert _run(monkeypatch, repo, "submit-review", "--payload", str(repo / "missing.json")) == 1
    assert "review payload" in capsys.readouterr().err


def test_user_next_rejects_stale_different_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _run(monkeypatch, repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    capsys.readouterr()
    assert _run(monkeypatch, repo, "next", "--as", "user", "--issue", "9999") == 1
    assert "stale workflow file" in capsys.readouterr().err


def test_abort_records_terminal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="implementing")
    assert _run(monkeypatch, repo, "abort", "--as", "user", "--reason", "giving up") == 0
    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["status"] == "error"
    assert state["error"]["reason"] == "giving up"


def test_submit_review_success_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="audit", status="reviewing")
    payload = repo / "review.json"
    payload.write_text(
        json.dumps({"checks": [{"id": cid, "status": "pass"} for cid in check_ids()]})
    )
    assert _run(monkeypatch, repo, "submit-review", "--payload", str(payload)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "approved"
    assert out["owner"] == "user"


def test_report_fixes_hands_back_to_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="changes-requested", last_reviewed="oldsha")
    assert _run(monkeypatch, repo, "report-fixes", "--note", "addressed") == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "audit"


def test_escalate_hands_to_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="implementing")
    assert _run(monkeypatch, repo, "escalate", "--as", "user", "--reason", "stuck") == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "human"


def test_resolve_hands_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="human", status="escalated")
    assert _run(monkeypatch, repo, "resolve", "--to", "user", "--note", "go ahead") == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "user"


def test_status_reports_absence_then_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    assert _run(monkeypatch, repo, "status") == 0
    assert json.loads(capsys.readouterr().out) == {"exists": False}
    _seed(repo, owner="user", status="implementing")
    assert _run(monkeypatch, repo, "status") == 0
    assert json.loads(capsys.readouterr().out)["issue"] == "1534"


def test_console_entry_point_runs_as_subprocess(tmp_path: Path) -> None:
    # Smoke test that the installed module entry point works end-to-end (not
    # counted toward coverage, which is measured in-process above).
    repo = _init_repo(tmp_path)
    import sys

    out = subprocess.run(
        (
            sys.executable,
            "-m",
            "vergil_tooling.bin.vrg_pr_workflow",
            "--base",
            "develop",
            "next",
            "--as",
            "user",
            "--issue",
            "1534",
            "--no-audit",
        ),
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["then"]["verb"] == "report-ready"


def test_verb_without_workflow_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    rc = _run(monkeypatch, repo, "report-ready", "--title", "t", "--summary", "s", "--notes", "n")
    assert rc == 1
    assert "no workflow file" in capsys.readouterr().err


def test_audit_next_without_issue_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="audit", status="reviewing")
    assert _run(monkeypatch, repo, "next", "--as", "audit") == 1
    assert "must pass --issue" in capsys.readouterr().err
