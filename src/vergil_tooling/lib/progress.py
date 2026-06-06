"""Stage-aware progress framework for long-running, human-invoked commands.

Design: docs/specs/2026-06-05-progress-framework-design.md
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

LOG_RETAIN = 20
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

StageMode = Literal["warn", "fail_defer", "fail_fast"]
StageStatus = Literal["ok", "warn", "failed", "skipped", "interrupted"]

_SYMBOLS: dict[str, str] = {
    "ok": "✓",
    "warn": "⚠",
    "failed": "✗",
    "skipped": "⚠",
    "interrupted": "✗",
}


@dataclass
class Stage:
    """One step of a procedural pipeline. Failure is signaled by ``fn`` raising."""

    name: str
    fn: Callable[[Any], None]
    mode: StageMode
    skip_flag: str | None = None


@dataclass
class StageResult:
    """Outcome of one stage."""

    name: str
    status: StageStatus
    elapsed: float = 0.0
    error: str | None = None


class PipelineError(Exception):
    """One or more pipeline stages failed."""

    def __init__(self, failures: list[StageResult]) -> None:
        self.failures = failures
        names = ", ".join(f.name for f in failures)
        plural = "" if len(failures) == 1 else "s"
        super().__init__(f"{len(failures)} stage{plural} failed ({names})")


def format_elapsed(seconds: float) -> str:
    """Format a per-stage duration: ``3.2s`` or ``1m01s``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"


def format_total(seconds: float) -> str:
    """Format a pipeline total as ``MM:SS``."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def is_github_actions() -> bool:
    """True when actually running under GitHub Actions."""
    return os.environ.get("GITHUB_ACTIONS") == "true"


def detect_format() -> str:
    """Auto-detect the renderer: TTY -> rich, GHA -> gha, else plain."""
    if sys.stdout.isatty():
        return "rich"
    if is_github_actions():
        return "gha"
    return "plain"


class RunLog:
    """Full verbose log for one run of a progress-aware command.

    Lives at ``.vergil/<command>-YYYYMMDD-HHMMSS.log``. On creation, prunes
    older logs for the same command so at most ``LOG_RETAIN`` remain.
    """

    def __init__(self, command: str, repo_root: Path) -> None:
        log_dir = repo_root / ".vergil"
        log_dir.mkdir(exist_ok=True)
        _prune_logs(log_dir, command)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005
        self.path = log_dir / f"{command}-{stamp}.log"
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, line: str) -> None:
        """Append one line, with ANSI escapes stripped, and flush."""
        self._fh.write(_ANSI_RE.sub("", line) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _prune_logs(log_dir: Path, command: str) -> None:
    logs = sorted(log_dir.glob(f"{command}-*.log"))
    excess = len(logs) - (LOG_RETAIN - 1)
    for stale in logs[: max(0, excess)]:
        stale.unlink(missing_ok=True)
