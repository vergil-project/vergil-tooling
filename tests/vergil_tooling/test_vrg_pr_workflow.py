"""Tests for vrg-pr-workflow's batch subcommands (epic vergil-project/.github#1019).

``batch-start``, ``batch-extract`` and ``batch-show`` run in a temporary git
clone whose ``origin`` names a github.com repo; the hard rules are faked at
``batch_rules.check_hard_rules``. The other subcommands are covered in
``tests/vergil_tooling/pr_workflow/test_cli_e2e.py``.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_pr_workflow
from vergil_tooling.lib.pr_workflow.batch_marker import Marker, read_marker, write_markers
from vergil_tooling.lib.pr_workflow.batch_rules import BatchRuleError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from unittest.mock import MagicMock

RULES = "vergil_tooling.bin.vrg_pr_workflow.batch_rules.check_hard_rules"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture()
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone of acme/tools on develop, with the working directory in it."""
    repo = tmp_path / "tools"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base commit")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/tools.git")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture()
def rules() -> Iterator[MagicMock]:
    with patch(RULES) as fake:
        yield fake


def _run(*args: str) -> int:
    return vrg_pr_workflow.main(["--base", "develop", *args])


def _state_dir(repo: Path, number: int) -> Path:
    return repo / "build" / ".vergil-state" / "issue-implement" / str(number)


def test_batch_start_writes_marker_for_every_member(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(clone, "rev-parse", "develop")
    assert _run("batch-start", "--issue", "10", "--issue", "#11", "--issue", "acme/Tools#12") == 0
    rules.assert_called_once_with(["#10", "#11", "#12"], repo="acme/tools")
    for issue in ("#10", "#11", "#12"):
        assert read_marker(clone, issue) == Marker(["#10", "#11", "#12"], base)
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "resumed": False, "issues": ["#10", "#11", "#12"], "base_sha": base}


def test_batch_start_writes_to_the_main_clone_from_a_worktree(
    clone: Path, rules: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = clone.parent / "wt"
    _git(clone, "worktree", "add", "-b", "feature/10-batch-x", str(worktree))
    monkeypatch.chdir(worktree)
    assert _run("batch-start", "--issue", "10", "--issue", "11") == 0
    assert read_marker(clone, "#10") is not None
    assert not (worktree / "build").exists()


def test_batch_start_refuses_and_writes_nothing_on_rule_break(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    rules.side_effect = BatchRuleError("no duplicate", "#10 is listed more than once")
    assert _run("batch-start", "--issue", "10", "--issue", "10") == 1
    assert "no duplicate" in capsys.readouterr().err
    assert not (clone / "build").exists()


def test_batch_start_accepts_identical_existing_batch(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    old = "c" * 40
    write_markers(clone, ["#10", "#11"], base_sha=old)
    before = (_state_dir(clone, 10) / "batch").stat().st_mtime_ns
    assert _run("batch-start", "--issue", "10", "--issue", "11") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "resumed": True, "issues": ["#10", "#11"], "base_sha": old}
    assert read_marker(clone, "#11") == Marker(["#10", "#11"], old)
    assert (_state_dir(clone, 10) / "batch").stat().st_mtime_ns == before


def test_batch_start_refuses_different_list_over_existing_markers(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-start", "--issue", "11", "--issue", "12") == 1
    assert "#11 is already in the batch #10, #11" in capsys.readouterr().err
    assert read_marker(clone, "#12") is None
    assert read_marker(clone, "#11") == Marker(["#10", "#11"], "c" * 40)


def test_batch_start_exits_2_in_another_repos_clone(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("batch-start", "--issue", "acme/other#10", "--issue", "11") == 2
    assert "this is a clone of acme/tools" in capsys.readouterr().err
    rules.assert_not_called()
    assert not (clone / "build").exists()


def test_batch_start_exits_2_when_the_clone_repo_cannot_be_told(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(clone, "remote", "set-url", "origin", "https://example.com/acme/tools.git")
    assert _run("batch-start", "--issue", "10", "--issue", "11") == 2
    assert "cannot tell which repo" in capsys.readouterr().err
    rules.assert_not_called()


def test_batch_start_refuses_an_unresolvable_base(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        vrg_pr_workflow.main(
            ["--base", "origin/develop", "batch-start", "--issue", "10", "--issue", "11"]
        )
        == 1
    )
    assert "cannot resolve the base origin/develop" in capsys.readouterr().err
    assert read_marker(clone, "#10") is None


def test_batch_start_refuses_a_malformed_issue(
    clone: Path, rules: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("batch-start", "--issue", "ten", "--issue", "11") == 1
    assert "--issue must be N, #N or owner/repo#N" in capsys.readouterr().err
    rules.assert_not_called()


def test_batch_extract_rewrites_the_batch(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = "c" * 40
    write_markers(clone, ["#10", "#11", "#12"], base_sha=sha)
    assert _run("batch-extract", "--issue", "acme/tools#11") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "extracted": "#11", "remaining": ["#10", "#12"], "batch": True}
    assert not _state_dir(clone, 11).exists()
    assert read_marker(clone, "#12") == Marker(["#10", "#12"], sha)


def test_batch_extract_to_one_member(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-extract", "--issue", "11") == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "extracted": "#11", "remaining": ["#10"], "batch": False}
    assert read_marker(clone, "#10") is None


def test_batch_extract_refuses_while_security_details_exist(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    review = _state_dir(clone, 11) / "merge-review"
    review.mkdir(mode=0o700)
    (review / "security-R1.md").write_text("details\n")
    assert _run("batch-extract", "--issue", "11") == 1
    assert "record step" in capsys.readouterr().err
    assert (review / "security-R1.md").exists()


def test_batch_extract_exits_2_in_another_repos_clone(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-extract", "--issue", "acme/other#11") == 2
    assert read_marker(clone, "#11") is not None
    assert "acme/other" in capsys.readouterr().err


def test_batch_show_prints_the_marker(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = "c" * 40
    write_markers(clone, ["#10", "#11"], base_sha=sha)
    assert _run("batch-show", "--issue", "acme/tools#11") == 0
    assert json.loads(capsys.readouterr().out) == {"issues": ["#10", "#11"], "base_sha": sha}


def test_batch_show_exits_1_without_marker(clone: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("batch-show", "--issue", "10") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "#10 has no batch marker" in captured.err


def test_batch_show_exits_2_in_another_repos_clone(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Another repo's #10 is in a batch here, so a wrong-clone lookup must never
    # read this clone's marker, nor answer "no marker".
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    assert _run("batch-show", "--issue", "acme/other#10") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "this is a clone of acme/tools" in captured.err


def test_batch_show_exits_2_on_an_unreadable_marker(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_markers(clone, ["#10", "#11"], base_sha="c" * 40)
    (_state_dir(clone, 10) / "batch").write_text("{")
    assert _run("batch-show", "--issue", "10") == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_batch_show_exits_2_on_a_malformed_issue(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("batch-show", "--issue", "ten") == 2
    assert "--issue must be" in capsys.readouterr().err


def test_batch_show_exits_2_outside_a_git_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert _run("batch-show", "--issue", "10") == 2
    assert "error: " in capsys.readouterr().err


def test_other_commands_keep_raising_outside_a_git_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        _run("status")


def test_batch_show_exits_2_without_an_origin_remote(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(clone, "remote", "remove", "origin")
    assert _run("batch-show", "--issue", "acme/tools#10") == 2
    assert "its origin remote cannot be read" in capsys.readouterr().err
