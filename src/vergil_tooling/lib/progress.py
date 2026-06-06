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
from typing import IO, TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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


_RULE = "─" * 45


def build_summary(
    label: str,
    results: Sequence[StageResult],
    total_elapsed: float,
    log_path: Path,
) -> str:
    """Build the plain-text final summary shared by all renderers."""
    warnings = [r for r in results if r.status in ("warn", "skipped")]
    failures = [r for r in results if r.status in ("failed", "interrupted")]
    lines = [_RULE]
    if warnings:
        lines.append("⚠  warnings (non-fatal):")
        lines.extend(f"   {r.name} — {r.error}" for r in warnings)
        lines.append("")
    if failures:
        lines.append("✗  failures:")
        lines.extend(f"   {r.name} — {r.error}" for r in failures)
        lines.append("")
        lines.append(f"{label} completed with errors  (total: {format_total(total_elapsed)})")
        lines.append(f"PipelineError: {PipelineError(failures)} · exit 1")
    else:
        lines.append(f"✓  {label} complete  (total: {format_total(total_elapsed)})")
    lines.append(f"   full log → {log_path}")
    return "\n".join(lines)


def _status_line(result: StageResult) -> str:
    symbol = _SYMBOLS[result.status]
    if result.status == "skipped":
        return f"{symbol} {result.name} — {result.error}"
    detail = f" — {result.error}" if result.error else ""
    return f"{symbol} {result.name}  {format_elapsed(result.elapsed)}{detail}"


class PlainRenderer:
    """Sequential flat output for piped / non-GHA-CI contexts."""

    def __init__(self, out: IO[str]) -> None:
        self._out = out

    def start_stage(self, name: str) -> None:
        self._print(f"→ {name}  starting...")

    def stage_line(self, line: str) -> None:
        self._print(line)

    def end_stage(self, result: StageResult) -> None:
        self._print(_status_line(result))
        self._print("")

    def summary(self, text: str) -> None:
        self._print(text)

    def close(self) -> None:
        pass

    def _print(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()


class GhaRenderer(PlainRenderer):
    """GitHub Actions ::group:: rendering; one collapsible section per stage."""

    def start_stage(self, name: str) -> None:
        self._print(f"::group::{name}")

    def end_stage(self, result: StageResult) -> None:
        self._print("::endgroup::")
        self._print(_status_line(result))
        if result.status in ("failed", "interrupted"):
            self._print(f"::error title={result.name} failed::{result.error}")
        self._print("")
