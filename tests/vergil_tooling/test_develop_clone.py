"""Tests for vergil_tooling.lib.develop_clone, against a local origin repo."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib import develop_clone
from vergil_tooling.lib.develop_clone import DevelopCloneError, clone_develop, clone_url


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _hermetic_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Commit as a fixed identity and ignore the host's git config."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare origin whose ``develop`` and ``feature`` branches differ.

    ``clone_url`` is pointed at it, so ``clone_develop`` clones it for any repo.
    """
    _hermetic_git(tmp_path, monkeypatch)
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    _git(seed, "checkout", "-q", "-b", "develop")
    (seed / "vergil.toml").write_text("develop-1\n")
    _git(seed, "add", "vergil.toml")
    _git(seed, "commit", "-qm", "first")
    (seed / "vergil.toml").write_text("develop-2\n")
    _git(seed, "commit", "-qam", "second")
    _git(seed, "push", "-q", "origin", "develop")
    _git(seed, "checkout", "-q", "-b", "feature")
    (seed / "vergil.toml").write_text("feature\n")
    _git(seed, "commit", "-qam", "feature")
    _git(seed, "push", "-q", "origin", "feature")
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: bare.as_uri())
    return bare


def test_clone_url_is_https_without_credentials() -> None:
    assert clone_url("o/r") == "https://github.com/o/r.git"


@pytest.mark.usefixtures("origin")
def test_clone_yields_develop_tip() -> None:
    with clone_develop("o/r") as clone:
        assert (clone / "vergil.toml").read_text() == "develop-2\n"
        assert _git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "develop"


@pytest.mark.usefixtures("origin")
def test_clone_is_shallow_single_branch() -> None:
    with clone_develop("o/r") as clone:
        assert _git(clone, "rev-list", "--count", "HEAD") == "1"
        remotes = _git(clone, "branch", "-r")
        assert "origin/develop" in remotes
        assert "feature" not in remotes


@pytest.mark.usefixtures("origin")
def test_clone_removed_on_exit() -> None:
    with clone_develop("o/r") as clone:
        assert clone.is_dir()
    assert not clone.exists()
    assert not clone.parent.exists()


@pytest.mark.usefixtures("origin")
def test_clone_removed_on_exception() -> None:
    seen: list[Path] = []
    with pytest.raises(ValueError, match="boom"), clone_develop("o/r") as clone:
        seen.append(clone)
        raise ValueError("boom")
    assert not seen[0].parent.exists()


def test_clone_failure_raises_naming_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _hermetic_git(tmp_path, monkeypatch)
    missing = tmp_path / "missing.git"
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: missing.as_uri())
    with (
        pytest.raises(DevelopCloneError, match=r"Could not clone the 'develop' branch of o/r"),
        clone_develop("o/r"),
    ):
        pytest.fail("the body must not run when the clone fails")


def test_clone_failure_on_missing_develop(origin: Path) -> None:
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/feature")
    _git(origin, "branch", "-D", "develop")
    with (
        pytest.raises(DevelopCloneError, match="o/r"),
        clone_develop("o/r"),
    ):
        pytest.fail("the body must not run when the clone fails")


def test_clone_failure_leaves_no_temp_dir() -> None:
    targets: list[Path] = []

    def _fail(*args: str) -> str:
        targets.append(Path(args[-1]))
        raise subprocess.CalledProcessError(128, ("git", "clone"), stderr="fatal: nope")

    with (
        patch.object(develop_clone.git, "read_output", side_effect=_fail),
        pytest.raises(DevelopCloneError, match="exit status 128"),
        clone_develop("o/r"),
    ):
        pytest.fail("the body must not run when the clone fails")
    assert len(targets) == 1
    assert not targets[0].parent.exists()


def test_clone_argv_carries_no_token() -> None:
    with (
        patch.object(develop_clone.git, "read_output") as mock_read,
        clone_develop("o/r"),
    ):
        pass
    assert mock_read.call_args.args[:-1] == (
        "clone",
        "--quiet",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        "develop",
        "https://github.com/o/r.git",
    )
