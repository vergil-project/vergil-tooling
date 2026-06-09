"""End-to-end tests driving the vrg-pr-workflow CLI in-process via cli.main().

In-process (not subprocess) so coverage is measured: each call exercises
parse_args, main, the cmd_* dispatch, identity resolution, and the real-git
head_sha/merge_base. The role is resolved from VRG_IDENTITY_MODE (no --as flag).
"""

from __future__ import annotations

import json
import os
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


def _run(monkeypatch: pytest.MonkeyPatch, repo: Path, *args: str, identity: str = "user") -> int:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("VRG_IDENTITY_MODE", identity)
    return cli.main(["--base", "develop", *args])


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed(
    repo: Path,
    *,
    owner: str,
    status: str,
    last_reviewed: str | None = None,
    pr_metadata: dict[str, str] | None = None,
) -> None:
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
    state.pr_metadata = pr_metadata
    LocalFileTransport(repo, base="develop").write(state)


def test_solo_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    repo = _init_repo(tmp_path)

    assert _run(monkeypatch, repo, "next", "--issue", "1534", "--no-audit") == 0
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

    assert _run(monkeypatch, repo, "next") == 0
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
    _run(monkeypatch, repo, "next", "--issue", "1534", "--no-audit")
    capsys.readouterr()
    assert _run(monkeypatch, repo, "next", "--issue", "1534", identity="audit") == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "solo"


def test_first_user_next_without_issue_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    assert _run(monkeypatch, repo, "next") == 1
    assert "must pass --issue" in capsys.readouterr().err


def test_next_as_human_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    repo = _init_repo(tmp_path)
    assert _run(monkeypatch, repo, "next", "--issue", "1534", identity="human") == 1
    assert "USER or AUDIT agent" in capsys.readouterr().err


def test_submit_check_with_bad_payload_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="audit", status="reviewing")
    assert _run(monkeypatch, repo, "submit-check", "--payload", str(repo / "missing.json")) == 1
    assert "check payload" in capsys.readouterr().err


def test_user_next_rejects_stale_different_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _run(monkeypatch, repo, "next", "--issue", "1534", "--no-audit")
    capsys.readouterr()
    assert _run(monkeypatch, repo, "next", "--issue", "9999") == 1
    assert "stale workflow file" in capsys.readouterr().err


def test_abort_records_terminal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="implementing")
    assert _run(monkeypatch, repo, "abort", "--reason", "giving up") == 0
    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["status"] == "error"
    assert state["error"]["reason"] == "giving up"
    assert state["error"]["by"] == "user"


def test_submit_check_full_round_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="audit", status="reviewing")
    payload = repo / "check.json"
    ids = check_ids()
    out: dict = {}
    for i, cid in enumerate(ids):
        payload.write_text(json.dumps({"id": cid, "status": "pass"}))
        assert _run(monkeypatch, repo, "submit-check", "--payload", str(payload)) == 0
        out = json.loads(capsys.readouterr().out)
        if i < len(ids) - 1:
            assert out["owner"] == "audit"
            assert out["pending"] == ids[i + 1]
    assert out["status"] == "approved"
    assert out["owner"] == "user"
    assert out["pending"] is None


def test_report_fixes_hands_back_to_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="changes-requested", last_reviewed="oldsha")
    assert _run(monkeypatch, repo, "report-fixes", "--note", "addressed") == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "audit"


def test_report_fixes_revises_metadata_without_new_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    # last_reviewed == current HEAD, so there is no new commit: the only thing to
    # re-review is the revised summary. This is the pr-description-fidelity path.
    _seed(
        repo,
        owner="user",
        status="changes-requested",
        last_reviewed=_head_sha(repo),
        pr_metadata={"title": "t", "summary": "old", "notes": "n", "linkage": "Ref"},
    )
    assert _run(monkeypatch, repo, "report-fixes", "--summary", "sharper summary") == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "audit"
    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["pr_metadata"]["summary"] == "sharper summary"
    assert state["round"] == 1


def test_report_fixes_rejects_empty_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="changes-requested", last_reviewed=_head_sha(repo))
    assert _run(monkeypatch, repo, "report-fixes", "--note", "nothing changed") == 1
    assert "no new commits and no metadata revision" in capsys.readouterr().err


def test_escalate_hands_to_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="implementing")
    assert _run(monkeypatch, repo, "escalate", "--reason", "stuck") == 0
    assert json.loads(capsys.readouterr().out)["owner"] == "human"


def test_resolve_hands_back_as_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="human", status="escalated")
    assert (
        _run(monkeypatch, repo, "resolve", "--to", "user", "--note", "go ahead", identity="human")
        == 0
    )
    assert json.loads(capsys.readouterr().out)["owner"] == "user"


def test_resolve_rejected_for_non_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="human", status="escalated")
    assert _run(monkeypatch, repo, "resolve", "--to", "user", identity="user") == 1
    assert "human-only" in capsys.readouterr().err


def test_status_reports_absence_then_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    assert _run(monkeypatch, repo, "status") == 0
    assert json.loads(capsys.readouterr().out) == {"exists": False}
    _seed(repo, owner="user", status="implementing")
    assert _run(monkeypatch, repo, "status") == 0
    assert json.loads(capsys.readouterr().out)["issue"] == "1534"


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
    assert _run(monkeypatch, repo, "next", identity="audit") == 1
    assert "must pass --issue" in capsys.readouterr().err


def test_audit_next_is_done_when_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = _init_repo(tmp_path)
    _seed(repo, owner="user", status="approved")
    assert _run(monkeypatch, repo, "next", identity="audit") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["done"] is True
    assert out["reason"] == "approved"


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
            "--issue",
            "1534",
            "--no-audit",
        ),
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "VRG_IDENTITY_MODE": "user"},
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["then"]["verb"] == "report-ready"
