"""The local, file-based transport.

State lives in ``.vergil/pr-workflow.json`` in the worktree. Writes are atomic
(temp + rename, via atomic_write). Git facts come from lib/git, run in the
process CWD (the worktree). (The dual-agent polling/heartbeat waits were removed
with the loop in #1872.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

if TYPE_CHECKING:
    from pathlib import Path

_DIR = ".vergil"
_FILE = "pr-workflow.json"


class LocalFileTransport(Transport):
    def __init__(self, worktree_root: Path, *, base: str = "origin/develop") -> None:
        self.worktree_root = worktree_root
        self.base = base

    @property
    def path(self) -> Path:
        return self.worktree_root / _DIR / _FILE

    def read(self) -> WorkflowState | None:
        if not self.path.is_file():
            return None
        return WorkflowState.from_json(self.path.read_text())

    def write(self, state: WorkflowState) -> None:
        atomic_write(self.path, state.to_json())

    def head_sha(self) -> str:
        return git.commit_sha("HEAD")

    def merge_base(self) -> str:
        return git.read_output("merge-base", self.base, "HEAD")
