"""A fresh shallow clone of a repo's ``develop`` branch, in a temporary directory.

``vrg-github-repo-config`` runs its managed-file checks against what is
committed on ``develop``, never against whatever checkout the caller happens to
be standing in: a dirty working tree, a feature branch or an unrelated repo
cannot change the result, and ``--repo other/repo`` audits that repo's files
from anywhere (epic vergil-project/.github#1064, Task 2).

The clone is plain ``git clone`` over HTTPS, which authenticates through git's
credential helper: ``gh auth setup-git`` in the ops workflow, and the human's
own ``gh`` login locally. A caller holding its own token (``token=``, epic
vergil-project/.github#1168) passes it as an HTTP header in the environment
instead. No token is put in argv, the URL or the logs.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Generator

DEVELOP_BRANCH = "develop"


class DevelopCloneError(RuntimeError):
    """The ``develop`` branch of a repo could not be cloned."""


def clone_url(repo: str) -> str:
    """The HTTPS clone URL of the GitHub repo ``OWNER/REPO``."""
    return f"https://github.com/{repo}.git"


def _clone_with_token(token: str, args: tuple[str, ...]) -> None:
    """Run ``git clone`` authenticated by *token*, retrying transient failures.

    The token reaches git only through :func:`git._git_auth_env` (an HTTP
    header in ``GIT_CONFIG_VALUE_0``), never argv, the URL or a config file.
    """

    def _once() -> None:
        try:
            subprocess.run(  # noqa: S603
                ("git", *args),  # noqa: S607
                check=True,
                text=True,
                capture_output=True,
                env=git._git_auth_env(token),  # noqa: SLF001 - the shared auth-header form
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    git._retry_network("clone", _once)  # noqa: SLF001 - the shared retry for network calls


@contextlib.contextmanager
def clone_develop(repo: str, *, token: str | None = None) -> Generator[Path]:
    """Yield the path of a shallow clone of *repo*'s ``develop`` branch.

    The clone (``--depth 1 --branch develop``) lives in a
    ``tempfile.TemporaryDirectory`` and is removed when the context exits,
    whether it exits normally or by an exception. Transient GitHub failures are
    retried; any other failure raises :class:`DevelopCloneError` naming the
    repo (git's own stderr has already been printed).

    With *token*, the clone authenticates with it as an HTTP header (the
    ``git._git_auth_env`` form) instead of git's credential helper.
    """
    with tempfile.TemporaryDirectory(prefix="vrg-develop-clone-") as tmp:
        target = Path(tmp) / "repo"
        args = (
            "clone",
            "--quiet",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            DEVELOP_BRANCH,
            clone_url(repo),
            str(target),
        )
        try:
            if token is None:
                git.read_output(*args)
            else:
                _clone_with_token(token, args)
        except (subprocess.CalledProcessError, OSError) as exc:
            msg = (
                f"Could not clone the {DEVELOP_BRANCH!r} branch of {repo}: "
                f"{error_text(exc, output=False)}"
            )
            raise DevelopCloneError(msg) from exc
        yield target
