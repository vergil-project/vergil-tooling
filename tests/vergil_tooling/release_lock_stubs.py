"""Shared helpers for the release-lock ref tests: a local bare origin and a flaky push.

The lock, history and arming refs are written to a throwaway bare repository
served as the repo's ``origin`` (``refs.origin_url`` is pointed at it), from a
working directory that is not a clone, so nothing depends on the CWD.
:class:`FlakyPush` stands in for a transport whose first push lands (or not)
and then reports a transient failure, so ``lib.git``'s retry meets a lease
rejection.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import retry
from vergil_tooling.lib.release_lock import refs

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

REPO = "org/repo"

#: The real ``subprocess.run``, kept before any test replaces it.
_REAL_RUN = subprocess.run


class Origin:
    """A bare repository served as ``origin`` for :data:`REPO`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str, stdin: str | None = None, env: dict[str, str] | None = None) -> str:
        return _REAL_RUN(  # noqa: S603
            ("git", "-C", str(self.path), *args),  # noqa: S607
            check=True,
            text=True,
            capture_output=True,
            input=stdin,
            env={**os.environ, **(env or {})},
        ).stdout.strip()

    def sha(self, ref: str) -> str | None:
        """The sha *ref* points at, or ``None`` when it is absent."""
        found = self.git("for-each-ref", "--format=%(objectname)", ref)
        return found or None

    def file(self, ref: str, name: str) -> str:
        return self.git("cat-file", "blob", f"{ref}:{name}")

    def commit(
        self,
        name: str,
        text: str,
        *,
        parent: str | None = None,
        author: str = "Test",
        email: str = "test@example.com",
    ) -> str:
        """Build a one-file commit holding *text* as *name*, authored by *author*."""
        blob = self.git("hash-object", "-w", "--stdin", stdin=text)
        tree = self.git("mktree", stdin=f"100644 blob {blob}\t{name}\n")
        parents = ("-p", parent) if parent else ()
        ident = {
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": email,
        }
        return self.git("commit-tree", tree, *parents, "-m", "test", env=ident)

    def set_ref(self, ref: str, sha: str) -> None:
        self.git("update-ref", ref, sha)

    def reject_pushes(self) -> None:
        """Install a pre-receive hook that refuses every push."""
        hook = self.path / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'refused by the test hook' >&2\nexit 1\n")
        hook.chmod(0o755)


def make_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    """A fresh bare origin for :data:`REPO`, with no global or system git config."""
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    bare = tmp_path / "origin.git"
    _REAL_RUN(("git", "init", "-q", "--bare", str(bare)), check=True)  # noqa: S603, S607
    elsewhere = tmp_path / "not-a-clone"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(refs, "origin_url", lambda repo: str(bare))
    return Origin(bare)


class FlakyPush:
    """Fail the first push with a transient error, after *apply*ing it or not.

    *meanwhile*, when given, runs before the failure is reported: another
    writer's change landing in the gap. ``lib.git`` retries the push, and the
    retry meets whatever the ref then holds.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        apply: bool = True,
        meanwhile: Callable[[], object] | None = None,
    ) -> None:
        self.apply = apply
        self.meanwhile = meanwhile
        self.failed = 0
        self.pushes = 0
        monkeypatch.setattr(subprocess, "run", self._run)
        monkeypatch.setattr(retry, "compute_delay", lambda attempt: 0.0)

    def _run(self, cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)) and "push" in cmd:
            self.pushes += 1
            if not self.failed:
                self.failed += 1
                if self.apply:
                    _REAL_RUN(cmd, *args, **kwargs)
                if self.meanwhile is not None:
                    self.meanwhile()
                raise subprocess.CalledProcessError(
                    128, cmd, output="", stderr="remote: 502 Bad Gateway\n"
                )
        return _REAL_RUN(cmd, *args, **kwargs)
