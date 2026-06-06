"""Git subprocess wrappers."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import github

_REMOTE_SUBCOMMANDS: set[str] = {"push", "pull", "fetch", "ls-remote"}


def _git_auth_env(token: str) -> dict[str, str]:
    """Return env dict that authenticates HTTPS git to GitHub."""
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }


def _remote_env(args: tuple[str, ...]) -> dict[str, str] | None:
    """Return auth env if *args* begins with a remote-capable subcommand."""
    if args and args[0] in _REMOTE_SUBCOMMANDS:
        token = github.get_installation_token()
        if token is not None:
            return _git_auth_env(token)
    return None


def run(*args: str) -> None:
    """Run a git command and raise on failure."""
    env = _remote_env(args)
    try:
        result = subprocess.run(  # noqa: S603
            ("git", *args),  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def read_output(*args: str) -> str:
    """Run a git command and return stripped stdout."""
    env = _remote_env(args)
    try:
        result = subprocess.run(  # noqa: S603
            ("git", *args),  # noqa: S607
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise
    return result.stdout.strip()


def repo_root() -> Path:
    """Return the repository root directory."""
    return Path(read_output("rev-parse", "--show-toplevel"))


def is_main_worktree() -> bool:
    """Return True when the CWD belongs to the main worktree.

    Secondary worktrees have a ``.git`` file whose git-dir points into
    ``.git/worktrees/<name>/``, while the main worktree's git-dir is
    ``.git`` itself — ``--git-dir`` and ``--git-common-dir`` are equal
    only for the main worktree.
    """
    git_dir = Path(read_output("rev-parse", "--git-dir")).resolve()
    common_dir = Path(read_output("rev-parse", "--git-common-dir")).resolve()
    return git_dir == common_dir


def main_worktree_root() -> Path:
    """Return the root directory of the main worktree."""
    common_dir = Path(read_output("rev-parse", "--git-common-dir")).resolve()
    return common_dir.parent


def current_branch() -> str:
    """Return the current branch name."""
    return read_output("rev-parse", "--abbrev-ref", "HEAD")


def has_staged_changes() -> bool:
    """Return True if there are staged changes."""
    result = subprocess.run(  # noqa: S603
        ("git", "diff", "--cached", "--quiet"),  # noqa: S607
        check=False,
    )
    return result.returncode != 0


def ref_exists(ref: str) -> bool:
    """Return True if a git ref exists."""
    result = subprocess.run(  # noqa: S603
        ("git", "rev-parse", "--verify", "--quiet", ref),  # noqa: S607
        check=False,
    )
    return result.returncode == 0


def commit_sha(ref: str) -> str:
    """Return the commit SHA that *ref* resolves to."""
    return read_output("rev-parse", ref)


def merged_branches(target: str) -> list[str]:
    """Return local branches merged into *target*."""
    output = read_output("branch", "--merged", target, "--format=%(refname:short)")
    if not output:
        return []
    return output.splitlines()


def working_tree_status() -> str:
    """Return ``git status --porcelain`` output (empty string when clean)."""
    return read_output("status", "--porcelain")
