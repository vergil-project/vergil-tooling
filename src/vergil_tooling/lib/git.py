"""Git subprocess wrappers."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import github, progress

# Subcommands that may contact the remote and therefore need the installation
# token. "remote" is included because `git remote prune`/`update` ls-remote the
# origin to compute stale refs — a network op — even though most `git remote`
# subcommands are local (#1830).
_REMOTE_SUBCOMMANDS: set[str] = {"push", "pull", "fetch", "ls-remote", "remote"}


def _git_auth_env(token: str) -> dict[str, str]:
    """Return env dict that authenticates HTTPS git to GitHub."""
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        # Fail fast on a credential miss instead of blocking on an interactive
        # prompt no automated caller can answer (#1830).
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }


def _git_env(args: tuple[str, ...]) -> dict[str, str]:
    """Build the env for a git invocation.

    GIT_TERMINAL_PROMPT=0 is set unconditionally (#1830): a network op missing
    credentials we did not supply must fail fast, never hang forever on an
    unanswerable interactive prompt (the bug that wedged `git remote prune` in
    vrg-finalize-pr). For remote-capable subcommands the GitHub installation
    token is layered on when available.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if args and args[0] in _REMOTE_SUBCOMMANDS:
        token = github.get_installation_token()
        if token is not None:
            env = _git_auth_env(token)
    return env


def run(*args: str) -> None:
    """Run a git command, streaming output, and raise on failure."""
    progress.run(("git", *args), env=_git_env(args))


def read_output(*args: str) -> str:
    """Run a git command and return stripped stdout."""
    env = _git_env(args)
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
    args = ("diff", "--cached", "--quiet")
    result = subprocess.run(  # noqa: S603
        ("git", *args),  # noqa: S607
        check=False,
        env=_git_env(args),
    )
    return result.returncode != 0


def ref_exists(ref: str) -> bool:
    """Return True if a git ref exists."""
    args = ("rev-parse", "--verify", "--quiet", ref)
    result = subprocess.run(  # noqa: S603
        ("git", *args),  # noqa: S607
        check=False,
        env=_git_env(args),
    )
    return result.returncode == 0


def commit_sha(ref: str) -> str:
    """Return the commit SHA that *ref* resolves to."""
    return read_output("rev-parse", ref)


def committer_timestamp(path: str | Path) -> int:
    """Return the committer date (epoch seconds) of *path*'s checked-out HEAD.

    Run with ``-C`` so the caller need not change CWD. A canonical worktree
    always has its branch checked out, so ``HEAD`` is the branch tip.
    """
    return int(read_output("-C", str(path), "log", "-1", "--format=%ct", "HEAD"))


def merged_branches(target: str) -> list[str]:
    """Return local branches merged into *target*."""
    output = read_output("branch", "--merged", target, "--format=%(refname:short)")
    if not output:
        return []
    return output.splitlines()


def commits_ahead(base: str, branch: str) -> int:
    """Return the number of commits on *branch* not reachable from *base*."""
    return int(read_output("rev-list", "--count", f"{base}..{branch}"))


def working_tree_status() -> str:
    """Return ``git status --porcelain`` output (empty string when clean)."""
    return read_output("status", "--porcelain")
