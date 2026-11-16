"""Tests for the vrg-upstream-refresh CLI (epic vergil-project/.github#472, T6; #1168, Task 3a).

Task 3a leaves the CLI with its prepare-backed ``--dry-run`` only; publish and
the CLI modes arrive with Task 3b.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_upstream_refresh as cli
from vergil_tooling.lib import develop_clone, github, upstream_refresh

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
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare origin on ``develop`` that ``clone_develop`` clones; the CLI runs from a checkout."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv(upstream_refresh.KEY_ENV, raising=False)
    monkeypatch.setattr(github, "repo_installation_token", lambda _repo: None)
    monkeypatch.setattr(github, "get_installation_token", lambda *_a, **_k: None)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(bare))
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: bare.as_uri())
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(bare), str(work))
    _git(work, "checkout", "-q", "-b", "develop")
    monkeypatch.chdir(work)
    return bare


def _seed(origin: Path, toml: str) -> Path:
    work = origin.parent / "work"
    (work / "vergil.toml").write_text(toml)
    (work / "data.txt").write_text("old\n")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "develop")
    return work


def test_cli_refuses_agent_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    assert cli.main(["--dry-run"]) == 1
    assert "vrg-upstream-refresh opens PRs and is not run by agents" in capsys.readouterr().err


def test_cli_refuses_audit_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
    assert cli.main([]) == 1


def test_cli_without_dry_run_refused_until_publish(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)

    def no_prepare(*_a: object, **_k: object) -> object:
        pytest.fail("a refused run must not prepare")

    monkeypatch.setattr(cli, "prepare", no_prepare)
    assert cli.main(["--repo", "o/r"]) == 1
    assert "publishing is not available in this build" in capsys.readouterr().err


def test_cli_base_flag_removed() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--base", "develop"])


def test_cli_requires_config(origin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(origin, _TOML)
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "has no [upstream-refresh] section" in capsys.readouterr().err


def test_cli_invalid_config(origin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(origin, _TOML + '\n[upstream-refresh]\nhook = ""\n')
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "must be a non-empty string" in capsys.readouterr().err


def test_cli_dry_run_prints_body_and_leaves_checkout(
    origin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = _seed(origin, _HOOK_TOML)
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: would open the refresh PR on o/r (develop)" in out
    assert "## Changed files\n\n- `data.txt`" in out
    # Nothing is pushed, and the caller's checkout is untouched.
    assert _git(work, "ls-remote", "origin").count("refs/heads/") == 1
    assert _git(work, "status", "--porcelain", "--untracked-files=all") == ""
    assert (work / "data.txt").read_text() == "old\n"


def test_cli_dry_run_nothing(
    origin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(origin, _TOML + '\n[upstream-refresh]\nhook = "true"\n')
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert cli.main(["--dry-run"]) == 0
    assert "Dry run: nothing to refresh on develop" in capsys.readouterr().out


def test_cli_dry_run_resolves_repo_from_origin(
    origin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(origin, _HOOK_TOML)
    monkeypatch.setattr(cli.github, "current_repo", lambda: "o/from-origin")
    seen: list[str] = []
    real = upstream_refresh.prepare

    def spy(repo: str, out_dir: Path, **kwargs: object) -> upstream_refresh.Prepared:
        seen.append(repo)
        return real(repo, out_dir)

    monkeypatch.setattr(cli, "prepare", spy)
    assert cli.main(["--dry-run"]) == 0
    assert seen == ["o/from-origin"]


def test_cli_hook_failure_exits_1(origin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(origin, _TOML + '\n[upstream-refresh]\nhook = "exit 7"\n')
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "[upstream-refresh].hook failed (exit 7)" in capsys.readouterr().err


def test_cli_clone_failure_exits_1(
    origin: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = origin.parent / "missing.git"
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: missing.as_uri())
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    assert "Could not clone the 'develop' branch of o/r" in capsys.readouterr().err


def test_cli_command_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)

    def boom(*_a: object, **_k: object) -> object:
        raise subprocess.CalledProcessError(1, ["gh", "repo", "view"], stderr="HTTP 502")

    monkeypatch.setattr(cli, "prepare", boom)
    assert cli.main(["--repo", "o/r", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "command failed" in err
    assert "HTTP 502" in err
