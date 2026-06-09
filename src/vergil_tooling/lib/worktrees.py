"""Discover and select canonical ``.worktrees/`` worktrees.

Single home for worktree-convention logic: enumeration of worktrees
under the canonical ``.worktrees/`` container, branch lookup, and
interactive selection. Worktrees elsewhere (developer-managed,
outside the convention) are deliberately ignored — auto-acting on
them would surprise the user. Issue #315.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from vergil_tooling.lib import git, github
from vergil_tooling.lib.repo_init import prompt_choice


@dataclass(frozen=True)
class Worktree:
    """A canonical worktree and the branch it has checked out."""

    path: Path
    branch: str


class WorktreeState(str, Enum):
    """Lifecycle state of a canonical worktree, derived from PR + local signals."""

    OPEN_PR = "open-pr"
    NO_PR = "no-pr"
    DRAFT = "draft"
    MERGED = "merged"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorktreeStatus:
    """A worktree's derived lifecycle state and the signals behind it."""

    worktree: Worktree
    state: WorktreeState
    pr_number: int | None
    ahead: int
    dirty: bool
    detail: str | None = None

    @property
    def removable(self) -> bool:
        """True when the worktree is finished cruft safe to delete.

        Merged or closed PRs are removable — unless the tree is dirty,
        in which case there is uncommitted work to rescue first.
        """
        return self.state in (WorktreeState.MERGED, WorktreeState.CLOSED) and not self.dirty


def classify_worktree(
    worktree: Worktree,
    *,
    pr_number: int | None,
    pr_state: str | None,
    pr_lookup_failed: bool,
    ahead: int,
    dirty: bool,
    detail: str | None = None,
) -> WorktreeStatus:
    """Map already-gathered signals to a WorktreeStatus. Pure: no I/O.

    A failed PR lookup yields UNKNOWN with *detail* — never a silent
    downgrade to NO_PR, which would mislabel real work as stalled.
    """
    if pr_lookup_failed:
        state = WorktreeState.UNKNOWN
    elif pr_state == "OPEN":
        state = WorktreeState.OPEN_PR
    elif pr_state == "MERGED":
        state = WorktreeState.MERGED
    elif pr_state == "CLOSED":
        state = WorktreeState.CLOSED
    elif ahead > 0:
        state = WorktreeState.NO_PR
    else:
        state = WorktreeState.DRAFT
    return WorktreeStatus(
        worktree=worktree,
        state=state,
        pr_number=pr_number,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
    )


def _resolve_pr_state(branch: str) -> tuple[int | None, str | None]:
    """Resolve ``(pr_number, pr_state)`` for *branch*.

    An open PR wins; otherwise the most recent closed/merged PR (whose
    ``MERGED`` vs ``CLOSED`` state is read explicitly); otherwise
    ``(None, None)`` for a branch with no PR.
    """
    open_pr = github.pr_for_branch(branch)
    if open_pr is not None:
        return int(open_pr["number"]), "OPEN"
    closed = github.closed_pr_for_branch(branch)
    if closed is not None:
        return int(closed["number"]), github.pr_state(closed["number"])
    return None, None


def gather_worktree_status(worktree: Worktree, *, target: str) -> WorktreeStatus:
    """Gather local + remote signals for *worktree* and classify it.

    The single source of truth shared by ``vrg-worktree-status`` and the
    ``vrg-finalize-pr`` straggler sweep. A failed ``gh`` PR lookup is
    surfaced as ``UNKNOWN`` with the captured reason — never a silent
    failure that would misclassify the worktree.
    """
    ahead = git.commits_ahead(target, worktree.branch)
    dirty = bool(git.read_output("-C", str(worktree.path), "status", "--porcelain"))
    try:
        pr_number, pr_state = _resolve_pr_state(worktree.branch)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or str(exc)).strip()
        return classify_worktree(
            worktree,
            pr_number=None,
            pr_state=None,
            pr_lookup_failed=True,
            ahead=ahead,
            dirty=dirty,
            detail=detail,
        )
    return classify_worktree(
        worktree,
        pr_number=pr_number,
        pr_state=pr_state,
        pr_lookup_failed=False,
        ahead=ahead,
        dirty=dirty,
    )


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
