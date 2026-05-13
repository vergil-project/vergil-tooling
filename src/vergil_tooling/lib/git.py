"""Git subprocess wrappers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Env-var contract with `.githooks/pre-commit`. Any internal caller
# that runs `git commit` via this helper is by definition an
# st-* tool invocation — admit it via the gate. See
# docs/specs/host-level-tool.md "Git hooks".
_GATE_ENV_VAR = "ST_COMMIT_CONTEXT"
_GATE_ENABLED_VALUE = "1"


def run(*args: str) -> None:
    """Run a git command and raise on failure.

    When the first positional arg is ``"commit"``, automatically sets
    ``ST_COMMIT_CONTEXT=1`` in the subprocess environment so the
    repository's pre-commit gate admits the commit. This makes the
    env-var contract a property of the helper rather than something
    every internal caller has to remember (issue #295).
    """
    env = None
    if args and args[0] == "commit":
        env = {**os.environ, _GATE_ENV_VAR: _GATE_ENABLED_VALUE}
    subprocess.run(("git", *args), check=True, env=env)  # noqa: S603, S607


def read_output(*args: str) -> str:
    """Run a git command and return stripped stdout."""
    result = subprocess.run(  # noqa: S603
        ("git", *args),  # noqa: S607
        check=True,
        text=True,
        capture_output=True,
    )
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


def merged_branches(target: str) -> list[str]:
    """Return local branches merged into *target*."""
    output = read_output("branch", "--merged", target, "--format=%(refname:short)")
    if not output:
        return []
    return output.splitlines()


def working_tree_status() -> str:
    """Return ``git status --porcelain`` output (empty string when clean)."""
    return read_output("status", "--porcelain")
