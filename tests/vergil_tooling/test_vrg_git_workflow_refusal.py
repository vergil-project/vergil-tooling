"""Tests for vrg-git's diagnosis of a workflow-permission push refusal (#3814).

GitHub refuses an agent push for ``.github/workflows/**`` both when the
branch's own commits change a workflow file (expected: the human's
vrg-submit-pr pushes it) and when the branch is only stale, carrying an old
copy of a workflow file the default branch has since changed (the remedy is a
rebase). These tests build real repositories so the merge-base and diff
lookups really run; only the network push and the token lookup are stubbed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_git
from vergil_tooling.bin.vrg_git import _diagnose_workflow_refusal, main

_REAL_RUN = subprocess.run
_REFUSAL = (
    " ! [remote rejected] feature/1-x -> feature/1-x (refusing to allow a GitHub App to "
    "create or update workflow `.github/workflows/ci.yml` without `workflows` permission)\n"
)


def _git(repo: Path, *args: str) -> str:
    result = _REAL_RUN(("git", *args), cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout


def _commit(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"change {rel}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on ``feature/1-x``, branched from ``origin/develop`` (``origin/HEAD``)."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    _commit(path, ".github/workflows/ci.yml", "on: push\n")
    _commit(path, "README.md", "base\n")
    _git(path, "remote", "add", "origin", "https://github.com/orga/repo.git")
    _git(path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
    _git(path, "checkout", "-q", "-b", "feature/1-x")
    return path.resolve()


def _advance_base(repo: Path, rel: str, text: str) -> None:
    """Move ``origin/develop`` ahead of the branch's base with one commit on *rel*."""
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, rel, text)
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(repo, "checkout", "-q", "feature/1-x")


def _push(argv: list[str]) -> int:
    refused = subprocess.CompletedProcess(args=["git", *argv], returncode=1, stdout="", stderr=_REFUSAL)
    with (
        patch.object(vrg_git, "_run_network_with_retry", return_value=refused),
        patch.object(vrg_git.github, "get_installation_token", return_value=None),
        patch.object(vrg_git, "_check_frozen_push", return_value=None),
    ):
        return main(argv)


# -- the diagnosis ------------------------------------------------------------


def test_own_workflow_change_is_own(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _commit(repo, ".github/workflows/ci.yml", "on: pull_request\n")
    monkeypatch.chdir(repo)
    diagnosis = _diagnose_workflow_refusal(["origin", "feature/1-x"])
    assert diagnosis.kind == "own"
    assert diagnosis.base == "origin/develop"
    assert diagnosis.files == (".github/workflows/ci.yml",)


def test_upstream_only_workflow_change_is_stale(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "src.txt", "work\n")
    _advance_base(repo, ".github/workflows/ci.yml", "on: [push, pull_request]\n")
    monkeypatch.chdir(repo)
    diagnosis = _diagnose_workflow_refusal(["-u", "origin", "HEAD"])
    assert diagnosis.kind == "stale"
    assert diagnosis.base == "origin/develop"
    assert diagnosis.files == (".github/workflows/ci.yml",)


def test_own_change_on_a_stale_branch_stays_own(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The branch really changes a workflow: never reported as merely stale.
    _commit(repo, ".github/workflows/release.yml", "on: release\n")
    _advance_base(repo, ".github/workflows/ci.yml", "on: [push, pull_request]\n")
    monkeypatch.chdir(repo)
    diagnosis = _diagnose_workflow_refusal([])
    assert diagnosis.kind == "own"
    assert diagnosis.files == (".github/workflows/release.yml",)


def test_no_workflow_difference_is_unknown(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A branch that has merged the base has no own workflow change and no
    # workflow difference from it: vrg-git cannot say why GitHub refused, so
    # it keeps the usual guidance and says so, rather than claim staleness.
    _commit(repo, "src.txt", "work\n")
    _advance_base(repo, ".github/workflows/ci.yml", "on: [push, pull_request]\n")
    _git(repo, "merge", "-q", "--no-edit", "origin/develop")
    monkeypatch.chdir(repo)
    diagnosis = _diagnose_workflow_refusal(["origin", "feature/1-x"])
    assert diagnosis.kind == "unknown"
    assert "may be out of date locally" in diagnosis.reason


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--all", "origin"], "the push uses --all"),
        (["--tags", "origin"], "the push uses --tags"),
        (["origin", "develop"], "'develop', not the current branch"),
        (["origin", "other:feature/1-x"], "'other:feature/1-x', not the current branch"),
        (["origin", ":feature/1-x"], "not the current branch"),
    ],
)
def test_push_not_of_head_is_unknown(
    repo: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], reason: str
) -> None:
    _commit(repo, "src.txt", "work\n")
    _advance_base(repo, ".github/workflows/ci.yml", "on: [push, pull_request]\n")
    monkeypatch.chdir(repo)
    diagnosis = _diagnose_workflow_refusal(args)
    assert diagnosis.kind == "unknown"
    assert reason in diagnosis.reason


@pytest.mark.parametrize(
    "args",
    [
        ["origin", "feature/1-x"],
        ["origin", "refs/heads/feature/1-x"],
        ["origin", "+HEAD:refs/heads/feature/1-x"],
        ["--force-with-lease", "origin", "feature/1-x"],
        ["--force-with-lease=feature/1-x:abc", "origin", "feature/1-x"],
        ["--set-upstream", "origin"],
    ],
)
def test_push_of_head_is_diagnosed(
    repo: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    _commit(repo, "src.txt", "work\n")
    _advance_base(repo, ".github/workflows/ci.yml", "on: [push, pull_request]\n")
    monkeypatch.chdir(repo)
    assert _diagnose_workflow_refusal(args).kind == "stale"


def test_unknown_default_branch_is_unknown(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    monkeypatch.chdir(repo)
    diagnosis = _diagnose_workflow_refusal(["origin", "feature/1-x"])
    assert diagnosis.kind == "unknown"
    assert "refs/remotes/origin/HEAD is not set" in diagnosis.reason


def test_no_merge_base_is_unknown(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(repo, "checkout", "-q", "--orphan", "lonely")
    _commit(repo, "x.txt", "x\n")
    monkeypatch.chdir(repo)
    diagnosis = _diagnose_workflow_refusal(["origin", "lonely"])
    assert diagnosis.kind == "unknown"
    assert "no merge-base with origin/develop" in diagnosis.reason


def test_outside_a_repo_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _diagnose_workflow_refusal(["origin", "feature/1-x"]).kind == "unknown"


# -- what vrg-git push reports --------------------------------------------------


def test_own_change_keeps_the_hand_off(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, ".github/workflows/ci.yml", "on: pull_request\n")
    monkeypatch.chdir(repo)
    assert _push(["push", "-u", "origin", "feature/1-x"]) == 0
    err = capsys.readouterr().err
    assert "refusing to allow" in err
    assert "own commits change .github/workflows/**" in err
    assert ".github/workflows/ci.yml" in err
    assert "report-ready" in err
    assert "could not check" not in err
    assert "STALE" not in err


def test_stale_branch_fails_with_rebase_guidance(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "src.txt", "work\n")
    _advance_base(repo, ".github/workflows/ci.yml", "on: [push, pull_request]\n")
    monkeypatch.chdir(repo)
    assert _push(["push", "-u", "origin", "feature/1-x"]) == 1
    err = capsys.readouterr().err
    assert "refusing to allow" in err
    assert "STALE" in err
    assert "vrg-git fetch origin" in err
    assert "vrg-git rebase origin/develop" in err
    assert ".github/workflows/ci.yml" in err
    assert "Do NOT hand this off" in err
    assert "PROCEED AS USUAL" not in err
    assert "This branch's own commits change" not in err


def test_undiagnosed_refusal_keeps_the_hand_off_with_a_caveat(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    monkeypatch.chdir(repo)
    assert _push(["push", "-u", "origin", "feature/1-x"]) == 0
    err = capsys.readouterr().err
    assert "PROCEED AS USUAL" in err
    assert "could not check the branch's own diff" in err
    assert "vrg-git diff <default-branch>...HEAD -- .github/workflows/" in err
    assert "own commits change" not in err


def test_dash_c_diagnoses_the_named_repo(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(repo, "src.txt", "work\n")
    _advance_base(repo, ".github/workflows/ci.yml", "on: [push, pull_request]\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _push(["-C", str(repo), "push", "-u", "origin", "feature/1-x"]) == 1
    assert "STALE" in capsys.readouterr().err
