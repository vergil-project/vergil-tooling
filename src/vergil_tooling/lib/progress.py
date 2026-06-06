"""Stage-aware progress framework for long-running, human-invoked commands.

Design: docs/specs/2026-06-05-progress-framework-design.md
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import IO, TYPE_CHECKING, Any, Literal

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Sequence
    from pathlib import Path

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


_STATUS_STYLES: dict[str, str] = {
    "ok": "green",
    "warn": "yellow",
    "failed": "red",
    "skipped": "yellow",
    "interrupted": "red",
}


class RichRenderer:
    """Live TTY display: completed stages collapse to one line, the active stage
    shows a spinner plus a rolling window of its last N output lines."""

    def __init__(self, out: IO[str], window: int, *, force_terminal: bool | None = None) -> None:
        self._console = Console(file=out, highlight=False, force_terminal=force_terminal)
        self._window = window
        self._completed: list[Text] = []
        self._tail: deque[str] = deque(maxlen=window if window > 0 else 1)
        self._active: str | None = None
        self._live = Live(console=self._console, refresh_per_second=8)
        self._live.start()

    def _renderable(self) -> Group:
        parts: list[Text | Spinner] = list(self._completed)
        if self._active is not None:
            parts.append(Spinner("dots", text=Text(f" {self._active}")))
            if self._window > 0:
                parts.extend(Text.from_ansi(f"   {line}", style="dim") for line in self._tail)
        return Group(*parts)

    def start_stage(self, name: str) -> None:
        self._active = name
        self._tail.clear()
        self._live.update(self._renderable())

    def stage_line(self, line: str) -> None:
        if self._window == 0:
            self._console.print(Text.from_ansi(line))
        else:
            self._tail.append(line)
        self._live.update(self._renderable())

    def end_stage(self, result: StageResult) -> None:
        self._completed.append(Text(_status_line(result), style=_STATUS_STYLES[result.status]))
        self._active = None
        self._tail.clear()
        self._live.update(self._renderable())

    def summary(self, text: str) -> None:
        self._live.update(self._renderable())
        self._live.stop()
        self._console.print(Text(text))

    def close(self) -> None:
        if self._live.is_started:
            self._live.stop()


@dataclass
class _Session:
    """Active pipeline state: routes output lines to the renderer and the log."""

    renderer: Any
    log: RunLog

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def handle_line(self, line: str) -> None:
        with self._lock:
            self.log.write(line)
            self.renderer.stage_line(line)


_session: _Session | None = None


def emit(line: str) -> None:
    """Route one line of output through the active pipeline, or print it."""
    session = _session
    if session is not None:
        session.handle_line(line)
    else:
        print(line)


class _EmitWriter(io.TextIOBase):
    """File-like sink that forwards complete lines to ``emit``.

    Used to redirect sys.stdout/sys.stderr during stage execution so bare
    ``print()`` calls deep in stage code join the renderer and log.
    """

    def __init__(self) -> None:
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            emit(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            emit(self._buf)
            self._buf = ""


def run(
    cmd: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> int:
    """Run *cmd*, streaming each output line through the active pipeline.

    Raises CalledProcessError (carrying captured stdout/stderr) on nonzero
    exit when ``check`` is true; otherwise returns the exit code.
    """
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def _pump(stream: IO[str], name: str) -> None:
        for raw in stream:
            line = raw.rstrip("\n")
            captured[name].append(line)
            emit(line)

    threads = [
        threading.Thread(target=_pump, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        raise
    for thread in threads:
        thread.join()
    if check and returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            tuple(cmd),
            output="\n".join(captured["stdout"]),
            stderr="\n".join(captured["stderr"]),
        )
    return returncode


def add_progress_args(parser: argparse.ArgumentParser, stages: Sequence[Stage]) -> None:
    """Add the framework's CLI flags, including per-stage --skip-* escape hatches."""
    parser.add_argument(
        "--output-window",
        type=int,
        default=5,
        metavar="N",
        help="Rolling window size for the TTY renderer. 0 = full stream then collapse.",
    )
    parser.add_argument(
        "--output-format",
        choices=("rich", "gha", "plain"),
        default=None,
        help="Override renderer auto-detection.",
    )
    for stage in stages:
        if stage.skip_flag is not None:
            if stage.mode != "fail_fast":
                msg = f"skip_flag is only supported on fail_fast stages: {stage.name}"
                raise ValueError(msg)
            parser.add_argument(
                "--" + stage.skip_flag.replace("_", "-"),
                action="store_true",
                default=False,
                help=f"Skip the {stage.name} stage entirely.",
            )


def _make_renderer(fmt: str, out: IO[str], window: int) -> Any:
    if fmt == "rich":
        return RichRenderer(out, window)
    if fmt == "gha":
        return GhaRenderer(out)
    return PlainRenderer(out)


def run_pipeline(
    ctx: Any,
    stages: Sequence[Stage],
    *,
    command: str,
    label: str,
    args: argparse.Namespace,
    repo_root: Path,
) -> int:
    """Run *stages* in order with progress rendering and full logging.

    Returns 0 on success, 1 if any fail_defer/fail_fast stage failed,
    130 on KeyboardInterrupt.
    """
    global _session  # noqa: PLW0603
    out = sys.stdout
    log = RunLog(command, repo_root)
    fmt = args.output_format or detect_format()
    renderer = _make_renderer(fmt, out, args.output_window)
    _session = _Session(renderer=renderer, log=log)
    results: list[StageResult] = []
    interrupted = False
    start_total = time.monotonic()
    try:
        for stage in stages:
            if stage.skip_flag is not None and getattr(args, stage.skip_flag, False):
                flag = "--" + stage.skip_flag.replace("_", "-")
                result = StageResult(stage.name, "skipped", 0.0, f"skipped via {flag}")
                log.write(f"=== {stage.name}: skipped via {flag} ===")
                renderer.end_stage(result)
                results.append(result)
                continue
            renderer.start_stage(stage.name)
            log.write(f"=== {stage.name}: started ===")
            start = time.monotonic()
            try:
                with (
                    contextlib.redirect_stdout(_EmitWriter()),
                    contextlib.redirect_stderr(_EmitWriter()),
                ):
                    stage.fn(ctx)
            except KeyboardInterrupt:
                result = StageResult(
                    stage.name, "interrupted", time.monotonic() - start, "interrupted"
                )
                log.write(f"=== {stage.name}: interrupted ===")
                renderer.end_stage(result)
                results.append(result)
                interrupted = True
                break
            except Exception as exc:  # noqa: BLE001 — the pipeline is the failure boundary
                cause = f"{type(exc).__name__}: {exc}"
                status: StageStatus = "warn" if stage.mode == "warn" else "failed"
                result = StageResult(stage.name, status, time.monotonic() - start, cause)
                log.write(f"=== {stage.name}: {status} ({cause}) ===")
                log.write(traceback.format_exc())
                renderer.end_stage(result)
                results.append(result)
                if stage.mode == "fail_fast":
                    break
            else:
                elapsed = time.monotonic() - start
                result = StageResult(stage.name, "ok", elapsed)
                log.write(f"=== {stage.name}: ok ({format_elapsed(elapsed)}) ===")
                renderer.end_stage(result)
                results.append(result)
    finally:
        _session = None
        text = build_summary(label, results, time.monotonic() - start_total, log.path)
        renderer.summary(text)
        renderer.close()
        log.write(text)
        log.close()
    if interrupted:
        return 130
    if any(r.status == "failed" for r in results):
        return 1
    return 0
