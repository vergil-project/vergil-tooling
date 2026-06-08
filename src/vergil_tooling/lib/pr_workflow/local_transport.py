"""The local, file-based transport.

State lives in ``.vergil/pr-workflow.json`` in the shared worktree. Writes are
atomic (temp + rename, via await_file); waits poll by re-reading the file each
interval. Change detection is SHA-256-of-content via re-read — never mtime,
matching await_file's deliberate decision (mtime semantics vary across the host
mount that the two agents share). Git facts come from lib/git, run in the
process CWD (the worktree).
"""

from __future__ import annotations

import time
from pathlib import Path

from vergil_tooling.lib import git
from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

_DIR = ".vergil"
_FILE = "pr-workflow.json"
_POLL_INTERVAL = 1.0


class LocalFileTransport(Transport):
    def __init__(
        self,
        worktree_root: Path,
        *,
        base: str = "origin/develop",
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self.worktree_root = worktree_root
        self.base = base
        self.poll_interval = poll_interval

    @property
    def path(self) -> Path:
        return self.worktree_root / _DIR / _FILE

    def read(self) -> WorkflowState | None:
        if not self.path.is_file():
            return None
        return WorkflowState.from_json(self.path.read_text())

    def write(self, state: WorkflowState) -> None:
        atomic_write(self.path, state.to_json())

    def wait_until_present(self, *, timeout: float) -> WorkflowState:
        deadline = time.monotonic() + timeout
        while True:
            state = self.read()
            if state is not None:
                return state
            if time.monotonic() >= deadline:
                msg = (
                    f"timed out waiting for the workflow file after {timeout}s — "
                    "is the implement session running in this worktree?"
                )
                raise WorkflowError(msg)
            time.sleep(self.poll_interval)

    def wait_until_owner(self, role: str, *, timeout: float) -> WorkflowState:
        deadline = time.monotonic() + timeout
        while True:
            state = self.read()
            if state is not None:
                if state.error is not None:
                    reason = state.error.get("reason", "unknown")
                    raise WorkflowError(f"counterpart reported an error: {reason}")
                if state.owner == role:
                    return state
            if time.monotonic() >= deadline:
                raise WorkflowError(f"timed out after {timeout}s waiting for owner={role!r}")
            time.sleep(self.poll_interval)

    def head_sha(self) -> str:
        return git.commit_sha("HEAD")

    def merge_base(self) -> str:
        return git.read_output("merge-base", self.base, "HEAD")
