"""Tests for the vrg-upstream-refresh CLI (epic vergil-project/.github#472, T6)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_upstream_refresh as cli
from vergil_tooling.lib import github

if TYPE_CHECKING:
    from pathlib import Path

_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""

_HOOK_TOML = _TOML + '\n[upstream-refresh]\nhook = "echo new > data.txt"\n'


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone of a bare origin on ``develop``, with the CLI run from inside it."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(github, "repo_installation_token", lambda _repo: None)
    monkeypatch.setattr(github, "get_installation_token", lambda *_a, **_k: None)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "checkout", "-q", "-b", "develop")
    monkeypatch.chdir(work)
    return work


def _seed(work: Path, toml: str) -> None:
    (work / "vergil.toml").write_text(toml)
    (work / "data.txt").write_text("old\n")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "develop")


def test_cli_refuses_agent_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    assert cli.main(["--dry-run"]) == 1
    assert "vrg-upstream-refresh opens PRs and is not run by agents" in capsys.readouterr().err


def test_cli_refuses_audit_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
    assert cli.main([]) == 1


def test_cli_requires_config(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(repo, _TOML)
    assert cli.main(["--repo", "o/r", "--base", "develop"]) == 1
    assert "vergil.toml has no [upstream-refresh] section" in capsys.readouterr().err


def test_cli_invalid_config(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(repo, _TOML + '\n[upstream-refresh]\nhook = ""\n')
    assert cli.main([]) == 1
    assert "must be a non-empty string" in capsys.readouterr().err


def test_cli_dry_run_no_push(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(repo, _HOOK_TOML)

    def no_sync(*_a: object) -> str:
        pytest.fail("--dry-run must not sync")

    monkeypatch.setattr(cli, "sync", no_sync)
    assert cli.main(["--repo", "o/r", "--base", "develop", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: would open or refresh" in out
    assert "## Changed files\n\n- `data.txt`" in out
    assert _git(repo, "ls-remote", "origin", "refs/heads/upstream-refresh") == ""


def test_cli_dry_run_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(repo, _TOML + '\n[upstream-refresh]\nhook = "true"\n')
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(cli, "default_branch", lambda repo: "develop")
    assert cli.main(["--dry-run"]) == 0
    assert "Dry run: nothing to refresh on develop" in capsys.readouterr().out


def test_cli_runs_sync_with_resolved_repo_and_base(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(repo, _HOOK_TOML)
    monkeypatch.setattr(cli.github, "current_repo", lambda: "o/from-origin")
    monkeypatch.setattr(cli, "default_branch", lambda repo: "develop")
    calls: list[tuple[object, ...]] = []

    def fake_sync(*args: object) -> str:
        calls.append(args)
        return "opened"

    monkeypatch.setattr(cli, "sync", fake_sync)
    assert cli.main([]) == 0
    ((repo_name, base, root, notes, paths),) = calls
    assert (repo_name, base, root, notes, paths) == (
        "o/from-origin",
        "develop",
        repo,
        [],
        ["data.txt"],
    )
    assert "vrg-upstream-refresh: opened" in capsys.readouterr().out


def test_cli_refusal_exits_1(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(repo, _HOOK_TOML)
    (repo / "data.txt").write_text("dirty\n")
    assert cli.main(["--repo", "o/r", "--base", "develop"]) == 1
    assert "working tree is not clean" in capsys.readouterr().err


def test_cli_command_failure_exits_1(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(repo, _HOOK_TOML)

    def boom(*_a: object) -> str:
        raise subprocess.CalledProcessError(1, ["gh", "pr", "list"], stderr="HTTP 502")

    monkeypatch.setattr(cli, "sync", boom)
    assert cli.main(["--repo", "o/r", "--base", "develop"]) == 1
    err = capsys.readouterr().err
    assert "command failed" in err
    assert "HTTP 502" in err
