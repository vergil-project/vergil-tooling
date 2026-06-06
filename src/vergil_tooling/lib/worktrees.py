"""Discover and select canonical ``.worktrees/`` worktrees.

Single home for worktree-convention logic: enumeration of worktrees
under the canonical ``.worktrees/`` container, branch lookup, and
interactive selection. Worktrees elsewhere (developer-managed,
outside the convention) are deliberately ignored — auto-acting on
them would surprise the user. Issue #315.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib import git
from vergil_tooling.lib.repo_init import prompt_choice


@dataclass(frozen=True)
class Worktree:
    """A canonical worktree and the branch it has checked out."""

    path: Path
    branch: str


def list_worktrees(repo_root: Path) -> list[Worktree]:
    """Return worktrees under ``repo_root/.worktrees/`` with their branches.

    Detached worktrees (no ``branch`` line in the porcelain output) and
    worktrees outside the canonical container are excluded.
    """
    output = git.read_output("worktree", "list", "--porcelain")
    canonical_root = (repo_root / ".worktrees").resolve()

    worktrees: list[Worktree] = []
    current_path: Path | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ").strip())
        elif line.startswith("branch ") and current_path is not None:
            ref = line.removeprefix("branch ").strip()
            resolved = current_path.resolve()
            current_path = None
            try:
                resolved.relative_to(canonical_root)
            except ValueError:
                continue
            worktrees.append(Worktree(path=resolved, branch=ref.removeprefix("refs/heads/")))
    return worktrees


def worktree_for_branch(branch: str, repo_root: Path) -> Path | None:
    """Return the canonical worktree path that has *branch* checked out, or None."""
    for wt in list_worktrees(repo_root):
        if wt.branch == branch:
            return wt.path
    return None


def require_tty(context: str) -> None:
    """Fail fast when an interactive prompt cannot reach the human.

    These tools are human touch points by design: a human is assumed to
    be present, and EOF-as-default would be a silent failure. Scripted
    use is served by explicit arguments, not by piping into prompts.

    Both stdin and stdout must be terminals: a non-TTY stdin means the
    answer cannot be typed; a non-TTY stdout means the prompt text is
    written into a pipe the human never sees — the prompt blocks
    invisibly instead of failing fast (issue #1448).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        msg = (
            f"{context} requires an interactive terminal.\n"
            "  Pass the target explicitly to run non-interactively."
        )
        raise SystemExit(msg)


def select_worktree(
    candidates: list[Worktree],
    *,
    purpose: str,
    labels: list[str],
) -> Worktree:
    """Choose among candidate worktrees; prompt only when there are several.

    ``labels`` must parallel ``candidates`` one-to-one and is what the
    menu displays.
    """
    if not candidates:
        msg = "select_worktree requires at least one candidate"
        raise ValueError(msg)
    if len(candidates) == 1:
        return candidates[0]
    require_tty(purpose)
    chosen = prompt_choice(purpose, labels)
    return candidates[labels.index(chosen)]
