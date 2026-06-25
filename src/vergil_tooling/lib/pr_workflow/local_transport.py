"""The local, file-based transport.

State lives in ``.vergil/pr-workflow.json`` in the shared worktree. Writes are
atomic (temp + rename, via await_file); waits poll by re-reading the file each
interval. Change detection is SHA-256-of-content via re-read — never mtime,
matching await_file's deliberate decision (mtime semantics vary across the host
mount that the two agents share). Git facts come from lib/git, run in the
process CWD (the worktree).
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

if TYPE_CHECKING:
    from pathlib import Path

_DIR = ".vergil"
_FILE = "pr-workflow.json"
_POLL_INTERVAL = 1.0
# Emit a heartbeat to stderr while blocking so a long wait is visibly alive in
# the watching human's session, not a silent hang (the two agents run in the
# foreground; transparency is the oversight model).
_HEARTBEAT_INTERVAL = 15.0


def _heartbeat(waiting_for: str, elapsed: float) -> None:
    print(f"  … still waiting for {waiting_for} ({int(elapsed)}s)", file=sys.stderr, flush=True)


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

    def wait_until_present(
        self, *, timeout: float, waiting_for: str | None = None
    ) -> WorkflowState:
        start = time.monotonic()
        deadline = start + timeout
        last_beat = start
        while True:
            state = self.read()
            if state is not None:
                return state
            now = time.monotonic()
            if now >= deadline:
                msg = (
                    f"timed out waiting for the workflow file after {timeout}s — "
                    "is the implement session running in this worktree?"
                )
                raise WorkflowError(msg)
            if waiting_for and now - last_beat >= _HEARTBEAT_INTERVAL:
                _heartbeat(waiting_for, now - start)
                last_beat = now
            time.sleep(self.poll_interval)

    def wait_until_owner(
        self, status: str, *, timeout: float, waiting_for: str | None = None
    ) -> WorkflowState:
        """Wait until the state file reaches a given status value.

        Named ``wait_until_owner`` for historical compatibility; the ``status``
        parameter replaced the ``role`` parameter when the dual-agent ownership
        model was removed (#1872). Scheduled for rename in a later cleanup task.
        """
        start = time.monotonic()
        deadline = start + timeout
        last_beat = start
        while True:
            state = self.read()
            if state is not None and state.status == status:
                return state
            now = time.monotonic()
            if now >= deadline:
                raise WorkflowError(f"timed out after {timeout}s waiting for status={status!r}")
            if waiting_for and now - last_beat >= _HEARTBEAT_INTERVAL:
                _heartbeat(waiting_for, now - start)
                last_beat = now
            time.sleep(self.poll_interval)

    def head_sha(self) -> str:
        return git.commit_sha("HEAD")

    def merge_base(self) -> str:
        return git.read_output("merge-base", self.base, "HEAD")
