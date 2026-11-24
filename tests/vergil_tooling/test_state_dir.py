"""Tests for ``vrg-state-dir``, the task state directory helper."""

from __future__ import annotations

import stat
import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_state_dir

if TYPE_CHECKING:
    from pathlib import Path

Cap = pytest.CaptureFixture[str]

ISSUE = "acme/widget#42"
ISSUE_REPO_URL = "https://github.com/acme/widget.git"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A main clone of the issue's repository, with one commit, as the cwd."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    _git(path, "remote", "add", "origin", ISSUE_REPO_URL)
    (path / ".gitignore").write_text("build/\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(path)
    return path


def _run(argv: list[str], capsys: Cap) -> tuple[int, str, str]:
    code = vrg_state_dir.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _argv(kind: str = "operational", issue: str = ISSUE) -> list[str]:
    return ["--kind", kind, "--issue", issue, "--ensure"]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.mark.parametrize("kind", ["operational", "issue-implement", "upstream-report"])
def test_ensure_creates_each_level_700_and_prints_the_path(
    repo: Path, capsys: Cap, kind: str
) -> None:
    code, out, err = _run(_argv(kind), capsys)
    assert (code, err) == (0, "")
    state = repo / "build" / ".vergil-state"
    directory = state / kind / "42"
    assert out == f"{directory}\n"
    assert [_mode(level) for level in (state, state / kind, directory)] == [0o700] * 3
    assert list(directory.iterdir()) == []


def test_ensure_accepts_an_existing_private_directory(repo: Path, capsys: Cap) -> None:
    _run(_argv(), capsys)
    directory = repo / "build" / ".vergil-state" / "operational" / "42"
    (directory / "security-1.md").write_text("kept")
    code, out, _err = _run(_argv(), capsys)
    assert code == 0
    assert out == f"{directory}\n"
    assert (directory / "security-1.md").read_text() == "kept"


def test_ensure_from_a_linked_worktree_uses_the_main_clone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Cap
) -> None:
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "other", str(linked), "develop")
    monkeypatch.chdir(linked)
    code, out, _err = _run(_argv(), capsys)
    assert code == 0
    assert out == f"{repo / 'build' / '.vergil-state' / 'operational' / '42'}\n"
    assert not (linked / "build").exists()


def test_ensure_refuses_a_symlinked_level(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    state = repo / "build" / ".vergil-state"
    state.mkdir(parents=True, mode=0o700)
    (state / "operational").symlink_to(elsewhere)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "operational is a symlink, not a directory" in err
    assert list(elsewhere.iterdir()) == []


def test_ensure_refuses_a_non_directory_level(repo: Path, capsys: Cap) -> None:
    _run(_argv(issue="acme/widget#41"), capsys)
    (repo / "build" / ".vergil-state" / "operational" / "42").write_text("a file")
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "42 is not a directory" in err


def test_ensure_refuses_a_wrong_mode(repo: Path, capsys: Cap) -> None:
    _run(_argv(), capsys)
    directory = repo / "build" / ".vergil-state" / "operational" / "42"
    directory.chmod(0o755)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "42 has mode 755, not 700" in err


def test_ensure_refuses_a_symlinked_build(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / "build").symlink_to(elsewhere)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "build is a symlink, not a directory" in err
    assert list(elsewhere.iterdir()) == []


def test_ensure_refuses_a_clone_of_another_repo(repo: Path, capsys: Cap) -> None:
    _git(repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "pushes to acme/other, not the issue's repo acme/widget" in err
    assert "run vrg-state-dir in a clone of acme/widget" in err
    assert not (repo / "build").exists()


def test_ensure_refuses_a_clone_without_origin(repo: Path, capsys: Cap) -> None:
    _git(repo, "remote", "remove", "origin")
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "cannot read the push URL of the 'origin' remote" in err
    assert not (repo / "build").exists()


def test_ensure_reports_a_git_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: Cap
) -> None:
    def fail() -> Path:
        raise subprocess.CalledProcessError(128, ["git", "rev-parse", "--git-common-dir"])

    monkeypatch.setattr(vrg_state_dir.git, "main_worktree_root", fail)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "ERROR: git failed (exit 128): git rev-parse --git-common-dir" in err
    assert not (repo / "build").exists()


@pytest.mark.parametrize("issue", ["widget#42", "acme/widget", "#42", "acme/widget#0"])
def test_ensure_refuses_a_bad_issue_ref(repo: Path, capsys: Cap, issue: str) -> None:
    code, out, err = _run(_argv(issue=issue), capsys)
    assert code == 1
    assert out == ""
    assert "--issue must" in err
    assert not (repo / "build").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["--kind", "operational", "--issue", ISSUE],
        ["--kind", "design-review", "--issue", ISSUE, "--ensure"],
        ["--issue", ISSUE, "--ensure"],
        ["--kind", "operational", "--ensure"],
    ],
)
def test_usage_errors_exit_2(repo: Path, capsys: Cap, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_state_dir.main(argv)
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""
    assert not (repo / "build").exists()
