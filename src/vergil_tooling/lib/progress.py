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
from importlib import metadata
from typing import IO, TYPE_CHECKING, Any, Literal

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Generator, Sequence
    from pathlib import Path

LOG_RETAIN = 20
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Auto-sized rolling window (--output-window omitted): the visible tail is
# computed per render from the live terminal height. The cap bounds redraw
# churn on very tall terminals; the margin leaves headroom for the summary
# line and shell prompt; the floor keeps degenerate terminals usable.
AUTO_WINDOW_CAP = 40
AUTO_WINDOW_MARGIN = 2
AUTO_WINDOW_FLOOR = 3

# Off a TTY, a status block that has not changed is reprinted as one
# heartbeat line at most this often, so a log shows the run is alive
# without a copy of the block on every update (issue #4694).
STATUS_HEARTBEAT_SECS = 300.0

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
    """Outcome of one stage.

    *note* is a short account of what an ``ok`` stage did, shown on its
    status line (e.g. ``4 merged``); a warning or failure uses *error*.
    """

    name: str
    status: StageStatus
    elapsed: float = 0.0
    error: str | None = None
    note: str | None = None


class StageFailedError(Exception):
    """Raise from a stage to fail it with exactly this message as its reason.

    Any other exception fails the stage with ``<Type>: <message>``; this one
    is shown as its message alone, for a stage whose reason already reads
    as a sentence (e.g. ``vrg-submit-pr``'s ``no PR opened: …``). Chain the
    underlying error (``raise StageFailedError(...) from exc``) so the log's
    traceback still names it.
    """


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


def tooling_version_line(command: str) -> str:
    """``<command>: vergil-tooling <version>``, naming the installed release.

    Printed at the top of a run and written as the first line of its run
    log, so a validation can tie a host run to the release it validates
    (issue #4994).
    """
    return f"{command}: vergil-tooling {metadata.version('vergil-tooling')}"


class RunLog:
    """Full verbose log for one run of a progress-aware command.

    Lives at ``.vergil/<command>-YYYYMMDD-HHMMSS.log``. On creation, prunes
    older logs for the same command so at most ``LOG_RETAIN`` remain.
    *header*, when given, is written as the log's first line.
    """

    def __init__(self, command: str, repo_root: Path, *, header: str | None = None) -> None:
        log_dir = repo_root / ".vergil"
        log_dir.mkdir(exist_ok=True)
        _prune_logs(log_dir, command)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005
        self.path = log_dir / f"{command}-{stamp}.log"
        self._fh = self.path.open("a", encoding="utf-8")
        if header is not None:
            self.write(header)

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
    *,
    success_label: str | None = None,
    failure_exit: int | None = 1,
) -> str:
    """Build the plain-text final summary shared by all renderers.

    *success_label*, when given, replaces *label* in the clean-completion
    line only (e.g. ``vrg-release v2.1.44``); the with-errors line always
    uses the plain *label* so a failed run never names what it did not
    finish (issue #3753).

    *failure_exit* is the exit status the failure line names (``· exit 1``).
    ``None`` leaves it off, for a command whose exit status is decided after
    the pipeline (``vrg-submit-pr`` goes on to finalize the PRs it opened).
    """
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
        suffix = f" · exit {failure_exit}" if failure_exit is not None else ""
        lines.append(f"PipelineError: {PipelineError(failures)}{suffix}")
    else:
        done = success_label or label
        lines.append(f"✓  {done} complete  (total: {format_total(total_elapsed)})")
    lines.append(f"   full log → {log_path}")
    return "\n".join(lines)


def _status_line(result: StageResult) -> str:
    symbol = _SYMBOLS[result.status]
    if result.status == "skipped":
        return f"{symbol} {result.name} — {result.error}"
    text = result.error or result.note
    detail = f" — {text}" if text else ""
    return f"{symbol} {result.name}  {format_elapsed(result.elapsed)}{detail}"


class PlainRenderer:
    """Sequential flat output for piped / non-GHA-CI contexts.

    A status block (:func:`status_block`) is append-only here: it is printed
    whole when it changed, and otherwise as its one heartbeat line once
    ``STATUS_HEARTBEAT_SECS`` have passed since anything of it was printed.
    """

    def __init__(self, out: IO[str], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._out = out
        self._clock = clock
        self._status_printed: float | None = None

    def start_stage(self, name: str) -> None:
        self._status_printed = None
        self._print(f"→ {name}  starting...")

    def stage_line(self, line: str) -> None:
        self._print(line)

    def stage_status(self, lines: Sequence[str], *, changed: bool, heartbeat: str) -> None:
        now = self._clock()
        if changed or self._status_printed is None:
            self._print("")
            for line in lines:
                self._print(line)
        elif now - self._status_printed >= STATUS_HEARTBEAT_SECS:
            self._print(heartbeat)
        else:
            return
        self._status_printed = now

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


def _tail_line(line: str) -> Text:
    """One rolling-window line, forced to a single physical row.

    The auto-window budgets *logical* lines (``_visible_window``), but Rich's
    LiveRender measures *physical* rows. A streamed line wider than the console
    (e.g. a CI-check row ending in a ~95-char job URL) would wrap to two rows,
    pushing the live block past the viewport; Rich then ellipsis-crops it to the
    full viewport height, and a full-viewport block scrolls on every repaint,
    leaving duplicated top rows behind. ``no_wrap`` + ellipsis keeps one logical
    line to one physical row so the budget holds and the block never overflows.
    """
    return Text.from_ansi(f"   {line}", style="dim", no_wrap=True, overflow="ellipsis")


class RichRenderer:
    """Live TTY display: completed stages collapse to one line, the active stage
    shows a spinner plus a rolling window of its last N output lines.

    An active stage's status block (:func:`status_block`) sits between its
    spinner and its rolling window and is redrawn in place on every update;
    it goes when the stage collapses.

    ``window=None`` (the default) auto-sizes the window to the terminal:
    each render shows as many tail lines as fit the current console height
    after the completed-stage lines and spinner, clamped to
    [AUTO_WINDOW_FLOOR, AUTO_WINDOW_CAP]. Querying ``console.size`` per
    render makes the window resize-aware."""

    def __init__(
        self,
        out: IO[str],
        window: int | None,
        *,
        force_terminal: bool | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        self._console = Console(
            file=out, highlight=False, force_terminal=force_terminal, width=width, height=height
        )
        self._window = window
        self._completed: list[Text] = []
        # In auto mode the visible slice never exceeds the cap, so buffering
        # more than AUTO_WINDOW_CAP lines is pointless.
        maxlen = AUTO_WINDOW_CAP if window is None else window if window > 0 else 1
        self._tail: deque[str] = deque(maxlen=maxlen)
        self._active: str | None = None
        self._status: list[str] = []
        self._live = Live(console=self._console, refresh_per_second=8)
        self._live.start()

    def _visible_window(self) -> int:
        """Tail size for auto mode, computed from the live terminal height."""
        available = (
            self._console.size.height
            - len(self._completed)
            - len(self._status)
            - 1  # the spinner line
            - AUTO_WINDOW_MARGIN
        )
        return max(AUTO_WINDOW_FLOOR, min(AUTO_WINDOW_CAP, available))

    def _renderable(self) -> Group:
        parts: list[Text | Spinner] = list(self._completed)
        if self._active is not None:
            parts.append(
                Spinner("dots", text=Text(f" {self._active}", no_wrap=True, overflow="ellipsis"))
            )
            parts.extend(
                Text(f"   {line}", no_wrap=True, overflow="ellipsis") for line in self._status
            )
            if self._window is None:
                visible = list(self._tail)[-self._visible_window() :]
                parts.extend(_tail_line(line) for line in visible)
            elif self._window > 0:
                parts.extend(_tail_line(line) for line in self._tail)
        return Group(*parts)

    def start_stage(self, name: str) -> None:
        self._active = name
        self._tail.clear()
        self._status = []
        self._live.update(self._renderable())

    def stage_status(self, lines: Sequence[str], *, changed: bool, heartbeat: str) -> None:  # noqa: ARG002 — a live redraw shows every update
        self._status = list(lines)
        self._live.update(self._renderable())

    def stage_line(self, line: str) -> None:
        if self._window == 0:
            self._console.print(Text.from_ansi(line))
        else:
            self._tail.append(line)
        self._live.update(self._renderable())

    def end_stage(self, result: StageResult) -> None:
        self._completed.append(
            Text(
                _status_line(result),
                style=_STATUS_STYLES[result.status],
                no_wrap=True,
                overflow="ellipsis",
            )
        )
        self._active = None
        self._tail.clear()
        self._status = []
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
    # Reason set by ``mark_warn`` during the current stage; ``run_pipeline``
    # reads and clears it per stage to downgrade a normally-returning stage's
    # status from ``ok`` to ``warn``.
    pending_warn: str | None = None
    # Text set by ``note`` during the current stage, shown on its ``ok``
    # status line; read and cleared per stage like ``pending_warn``.
    pending_note: str | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def handle_line(self, line: str) -> None:
        with self._lock:
            self.log.write(line)
            self.renderer.stage_line(line)

    def handle_log_line(self, line: str) -> None:
        with self._lock:
            self.log.write(line)

    def handle_status(self, lines: Sequence[str], *, changed: bool, heartbeat: str) -> None:
        with self._lock:
            if changed:
                stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                self.log.write(f"[{stamp}] status changed:")
                for line in lines:
                    self.log.write(line)
            self.renderer.stage_status(lines, changed=changed, heartbeat=heartbeat)


_session: _Session | None = None


def emit(line: str) -> None:
    """Route one line of output through the active pipeline, or print it."""
    session = _session
    if session is not None:
        session.handle_line(line)
    else:
        print(line)


def log_only(line: str) -> None:
    """Write one line to the active pipeline's run log, and not to the terminal.

    For output a stage keeps off the terminal but must still leave in the
    run log, which :func:`emit` would also render: vrg-release's
    install stage, whose commands' output is one check line on the
    terminal on success (issue #4812). The line goes to the same
    :class:`RunLog`, under the same lock, as every emitted line. Outside a
    running pipeline it writes to the log :func:`attach_log` attached, and
    with none attached it is a harmless no-op.
    """
    session = _session
    if session is not None:
        session.handle_log_line(line)
    elif _attached_log is not None:
        _attached_log.write(line)


# The run log a caller attaches while it runs work outside a pipeline, after
# the pipeline has finished (attach_log): log_only and stderr_line write there.
_attached_log: RunLog | None = None


@contextlib.contextmanager
def attach_log(log: RunLog) -> Generator[None]:
    """Make *log* the run log for :func:`log_only` and :func:`stderr_line` outside a pipeline.

    For work a command runs on real stdio once its pipeline has finished
    (``vrg-finalize-pr``'s ``--clean-dirty`` clear, issue #2348), whose lines
    must still reach the same run log. Inside a running pipeline the
    pipeline's own log is used as before.
    """
    global _attached_log  # noqa: PLW0603
    previous = _attached_log
    _attached_log = log
    try:
        yield
    finally:
        _attached_log = previous


def stderr_line(line: str) -> None:
    """Print *line* on stderr and leave it in the running command's run log, once.

    Inside a running stage, stderr is already routed through :func:`emit`,
    which renders the line and writes it to the run log. Outside one the line
    is printed on stderr and written to the log :func:`attach_log` attached,
    if any.
    """
    print(line, file=sys.stderr)
    if _session is None:
        log_only(line)


def status_block(lines: Sequence[str], *, changed: bool, heartbeat: str) -> None:
    """Show the current stage's status block: a few lines that replace the last ones.

    For a stage that polls (``vrg-finalize-pr``'s watch, issue #4694). The
    caller passes the whole block on every update, *changed* when what it
    reports has changed since the last update (not merely a clock in it),
    and *heartbeat*, one line that stands for an unchanged block.

    - On a TTY the block is redrawn in place on every update and goes when
      the stage collapses to its status line.
    - Off a TTY (plain and GHA) it is append-only: printed whole when
      *changed* (and on the stage's first update), and otherwise as
      *heartbeat* at most every ``STATUS_HEARTBEAT_SECS``.
    - The run log gets the block, with a timestamp, each time it changed.

    Outside a running stage it prints the block when *changed*, and nothing
    otherwise.
    """
    session = _session
    if session is not None:
        session.handle_status(lines, changed=changed, heartbeat=heartbeat)
    elif changed:
        print()
        for line in lines:
            print(line)


def mark_warn(reason: str) -> None:
    """Signal that the current stage completed but with a non-fatal warning.

    A stage whose ``fn`` returns normally is recorded ``ok`` (✓). Calling this
    while a stage runs downgrades that stage's recorded status to ``warn`` (⚠),
    with *reason* shown on its status row and in the summary — **without
    raising**, so control flow (including ``fail_fast``) is unaffected.

    Used by vrg-release's confirm stages to surface a deferred publish failure
    at the row where it was observed, not only in the terminal summary (a green
    ✓ over a deferral reads as full success at a glance). Outside a running
    pipeline (``_session is None``) it is a harmless no-op; the last call in a
    stage wins.
    """
    session = _session
    if session is not None:
        session.pending_warn = reason


def note(text: str) -> None:
    """Set the short account shown on the current stage's ``ok`` status line.

    A pipeline stage that returns normally is shown as ``✓ <name>  <elapsed>``;
    calling this while it runs adds ``— <text>`` (e.g. ``PR #12 auto-merge
    on``), as :func:`run_stage`'s *describe* does for a stage of its own. A
    stage that fails or is marked with :func:`mark_warn` shows its reason
    instead. Outside a running pipeline it is a harmless no-op; the last call
    in a stage wins.
    """
    session = _session
    if session is not None:
        session.pending_note = text


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
    stdin: int | None = None,
    cwd: Path | None = None,
    transcript: IO[str] | None = None,
) -> int:
    """Run *cmd*, streaming each output line through the active pipeline.

    ``stdin`` is forwarded to Popen — pass ``subprocess.DEVNULL`` for
    children that must never block on a terminal read. ``cwd`` is the
    child's working directory (default: this process's). Each line of
    stdout and stderr is also written to *transcript*, when given, in the
    order the lines arrive; a write that fails stops the writing, and its
    ``OSError`` is raised once the command has exited.

    Raises CalledProcessError (carrying captured stdout/stderr) on nonzero
    exit when ``check`` is true; otherwise returns the exit code.
    """
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
    )
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
    transcript_lock = threading.Lock()
    transcript_errors: list[OSError] = []

    def _pump(stream: IO[str], name: str) -> None:
        for raw in stream:
            line = raw.rstrip("\n")
            captured[name].append(line)
            if transcript is not None:
                with transcript_lock:
                    if not transcript_errors:
                        try:
                            transcript.write(line + "\n")
                        except OSError as exc:
                            transcript_errors.append(exc)
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
    if transcript_errors:
        raise transcript_errors[0]
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
        default=None,
        metavar="N",
        help=(
            "Rolling window size for the TTY renderer. Default: auto-size to the "
            f"terminal height (max {AUTO_WINDOW_CAP} lines, resize-aware). "
            "N = fixed window. 0 = full stream then collapse."
        ),
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


def _make_renderer(fmt: str, out: IO[str], window: int | None) -> Any:
    if fmt == "rich":
        return RichRenderer(out, window)
    if fmt == "gha":
        return GhaRenderer(out)
    return PlainRenderer(out)


def run_stage[T](
    name: str,
    fn: Callable[[], T],
    *,
    args: argparse.Namespace,
    log: RunLog,
    describe: Callable[[T], tuple[StageStatus, str]],
) -> T:
    """Run *fn* as one stage of its own, in a display of its own, and return its value.

    For a stage that must finish before the caller knows which pipeline, if
    any, runs next (``vrg-finalize-pr``'s watch, issue #4694). The stage is
    rendered as a pipeline's are: on a TTY it collapses to one status line
    when it ends, and the display closes with no summary, leaving that line
    above whatever the caller prints next. Its output goes to *log*, which
    the caller owns and may hand on to :func:`run_pipeline`.

    *describe* turns *fn*'s value into the stage's status (``ok`` or
    ``warn``) and the text on its status line. An exception from *fn*,
    ``KeyboardInterrupt`` included, ends the stage ``failed`` (or
    ``interrupted``) and is raised again for the caller to handle.
    """
    global _session  # noqa: PLW0603
    renderer = _make_renderer(args.output_format or detect_format(), sys.stdout, args.output_window)
    session = _Session(renderer=renderer, log=log)
    _session = session
    renderer.start_stage(name)
    log.write(f"=== {name}: started ===")
    start = time.monotonic()
    try:
        try:
            with (
                contextlib.redirect_stdout(_EmitWriter()),
                contextlib.redirect_stderr(_EmitWriter()),
            ):
                value = fn()
        except KeyboardInterrupt:
            log.write(f"=== {name}: interrupted ===")
            renderer.end_stage(
                StageResult(name, "interrupted", time.monotonic() - start, "interrupted")
            )
            raise
        except Exception as exc:
            cause = f"{type(exc).__name__}: {exc}"
            log.write(f"=== {name}: failed ({cause}) ===")
            log.write(traceback.format_exc())
            renderer.end_stage(StageResult(name, "failed", time.monotonic() - start, cause))
            raise
        status, text = describe(value)
        elapsed = time.monotonic() - start
        log.write(f"=== {name}: {status} ({text}, {format_elapsed(elapsed)}) ===")
        if status == "ok":
            result = StageResult(name, status, elapsed, note=text)
        else:
            result = StageResult(name, status, elapsed, text)
        renderer.end_stage(result)
        return value
    finally:
        _session = None
        renderer.close()


def run_pipeline(
    ctx: Any,
    stages: Sequence[Stage],
    *,
    command: str,
    label: str,
    args: argparse.Namespace,
    repo_root: Path,
    success_label: Callable[[], str | None] | None = None,
    results_sink: list[StageResult] | None = None,
    log: RunLog | None = None,
    failure_exit: int | None = 1,
) -> int:
    """Run *stages* in order with progress rendering and full logging.

    *success_label* is called once, after the stages finish, to supply the
    clean-completion label from state a stage produced (e.g. the version
    vrg-release cut). Returning ``None`` keeps the plain *label*.

    *results_sink*, when given, is extended with every stage's
    :class:`StageResult`, in order, once the stages finish, however they
    finish (vrg-validate records them for merge-review).

    *log*, when given, is the run log to write to instead of a new one (a
    :func:`run_stage` before the pipeline wrote to it too); the caller owns
    it and closes it.

    *failure_exit* is passed to :func:`build_summary`: the exit status its
    failure line names, or ``None`` to name none.

    Returns 0 on success, 1 if any fail_defer/fail_fast stage failed,
    130 on KeyboardInterrupt.
    """
    global _session  # noqa: PLW0603
    out = sys.stdout
    owns_log = log is None
    if log is None:
        log = RunLog(command, repo_root)
    fmt = args.output_format or detect_format()
    renderer = _make_renderer(fmt, out, args.output_window)
    session = _Session(renderer=renderer, log=log)
    _session = session
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
            session.pending_warn = None
            session.pending_note = None
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
            # SystemExit included: a stage calling sys.exit() is a stage
            # failure to record and summarize, not a silent process exit.
            except (Exception, SystemExit) as exc:  # noqa: BLE001 — the pipeline is the failure boundary
                cause = (
                    str(exc)
                    if isinstance(exc, StageFailedError)
                    else f"{type(exc).__name__}: {exc}"
                )
                status: StageStatus = "warn" if stage.mode == "warn" else "failed"
                result = StageResult(stage.name, status, time.monotonic() - start, cause)
                log.write(f"=== {stage.name}: {status} ({cause}) ===")
                log.write(traceback.format_exc())
                # Verbose context (e.g. a captured subprocess' stdout/stderr)
                # rides on the exception's ``detail`` attribute but is absent
                # from both ``str(exc)`` and the traceback. Record it in the
                # full log so the summary's "full log →" pointer actually leads
                # to the real reason instead of just the headline (#1691).
                detail = getattr(exc, "detail", None)
                if isinstance(detail, str) and detail.strip():
                    log.write(detail)
                renderer.end_stage(result)
                results.append(result)
                if stage.mode == "fail_fast":
                    break
            else:
                elapsed = time.monotonic() - start
                warn_reason = session.pending_warn
                if warn_reason is not None:
                    result = StageResult(stage.name, "warn", elapsed, warn_reason)
                    log.write(f"=== {stage.name}: warn ({warn_reason}) ===")
                else:
                    result = StageResult(stage.name, "ok", elapsed, note=session.pending_note)
                    done = format_elapsed(elapsed)
                    if session.pending_note is not None:
                        done = f"{session.pending_note}, {done}"
                    log.write(f"=== {stage.name}: ok ({done}) ===")
                renderer.end_stage(result)
                results.append(result)
    finally:
        _session = None
        if results_sink is not None:
            results_sink.extend(results)
        text = build_summary(
            label,
            results,
            time.monotonic() - start_total,
            log.path,
            success_label=success_label() if success_label is not None else None,
            failure_exit=failure_exit,
        )
        renderer.summary(text)
        renderer.close()
        log.write(text)
        if owns_log:
            log.close()
    if interrupted:
        return 130
    if any(r.status == "failed" for r in results):
        return 1
    return 0
