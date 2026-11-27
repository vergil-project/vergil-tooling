"""A real-git fixture for the batch tests: a bare ``origin`` and a clone of it.

Git runs for real, so the fold, autosquash and rebase behaviour under test is
git's own. Nothing here reads GitHub. Git's global and system configuration is
cut off and the commit identity is fixed, so the commits are the same on every
host.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

BASE_FILES: dict[str, str] = {"a.txt": "one\ntwo\nthree\n", "b.txt": "uno\ndos\n"}


class RebaseConflictError(Exception):
    """``autosquash`` stopped on a conflict. The rebase was aborted, so the branch is as it was."""


def _git(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    """Run git in *cwd* and return its stripped stdout; a failing command raises."""
    full_env = None if env is None else {**os.environ, **env}
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=full_env,
    )
    return result.stdout.strip()


def _commit(clone: Path, files: Mapping[str, str], subject: str) -> str:
    """Write *files* into *clone*, commit them all with *subject*, and return the new sha."""
    for name, text in files.items():
        path = clone / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", subject)
    return _git(clone, "rev-parse", "HEAD")


@dataclass(frozen=True)
class BatchRepo:
    """A clone of a bare ``origin``, on the feature branch ``feature`` forked from ``develop``.

    *path* is the clone, *origin* the bare repository it was cloned from, and
    *root* the directory holding both and any other clone a test makes.
    """

    path: Path
    origin: Path
    root: Path

    def git(self, *args: str) -> str:
        """Run git in the clone and return its stripped stdout."""
        return _git(self.path, *args)

    def head(self) -> str:
        """The sha the clone's HEAD points at."""
        return self.git("rev-parse", "HEAD")

    def commit_issue(self, number: int, files: Mapping[str, str]) -> str:
        """Commit *files* as issue *number*'s commit, its subject ending ``(#<number>)``."""
        return _commit(self.path, files, f"change {number} (#{number})")

    def fixup(self, sha: str, files: Mapping[str, str]) -> str:
        """Commit *files* as a ``fixup!`` of commit *sha*, on top of the branch."""
        subject = self.git("log", "-1", "--format=%s", sha)
        return _commit(self.path, files, f"fixup! {subject}")

    def autosquash(self) -> None:
        """Fold the ``fixup!`` commits into their targets, as ``vrg-git rebase -i --autosquash``.

        A conflict aborts the rebase, leaves the branch where it was, and raises
        :class:`RebaseConflictError`. Any other failure is raised unchanged.
        """
        try:
            _git(
                self.path,
                "rebase",
                "-i",
                "--autosquash",
                "origin/develop",
                env={"GIT_SEQUENCE_EDITOR": "true"},
            )
        except subprocess.CalledProcessError as exc:
            if not self.git("diff", "--name-only", "--diff-filter=U"):
                raise
            self.git("rebase", "--abort")
            msg = f"autosquash onto origin/develop conflicted: {exc.stdout}{exc.stderr}"
            raise RebaseConflictError(msg) from exc

    def rebase_in_progress(self) -> bool:
        """Whether a rebase is stopped part-way in the clone."""
        return any(
            (self.path / self.git("rev-parse", "--git-path", name)).exists()
            for name in ("rebase-merge", "rebase-apply")
        )

    def rebase_onto_develop(self) -> None:
        """Rebase the branch onto ``origin/develop``.

        A conflict is not handled: it raises :class:`subprocess.CalledProcessError`.
        """
        self.git("rebase", "origin/develop")

    def advance_develop(self, files: Mapping[str, str]) -> str:
        """Commit *files* on ``origin``'s ``develop`` from a second clone, and fetch it into ours.

        Returns the new ``develop`` sha. The clone's own branch is left alone.
        """
        pusher = self.root / "pusher"
        if not pusher.exists():
            _git(self.root, "clone", "-q", str(self.origin), str(pusher))
        _git(pusher, "fetch", "-q", "origin")
        _git(pusher, "checkout", "-q", "-B", "develop", "origin/develop")
        sha = _commit(pusher, files, "develop moves on")
        _git(pusher, "push", "-q", "origin", "develop")
        self.git("fetch", "-q", "origin")
        return sha


@pytest.fixture
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BatchRepo:
    """A bare ``origin`` with a ``develop`` of :data:`BASE_FILES`, and a clone on ``feature``."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "checkout", "-q", "-B", "develop")
    _commit(clone, BASE_FILES, "base")
    _git(clone, "push", "-q", "origin", "develop")
    _git(clone, "checkout", "-q", "-b", "feature")
    return BatchRepo(path=clone, origin=origin, root=tmp_path)
