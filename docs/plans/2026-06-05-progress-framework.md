# Progress Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the stage-aware progress framework (`vergil_tooling/lib/progress.py`) from
`docs/specs/2026-06-05-progress-framework-design.md` and migrate `vrg-release` and
`vrg-validate` onto it.

**Architecture:** A single module `src/vergil_tooling/lib/progress.py` provides `Stage`,
`run_pipeline`, three renderers (rich/GHA/plain), an always-on `.vergil/` log, and a streaming
subprocess runner. `run_pipeline` redirects `sys.stdout`/`sys.stderr` during each stage so all
existing `print()` calls deep in stage code automatically route through the active renderer
and log. `vrg-release` replaces its bespoke orchestrator loop; `vrg-validate` replaces its
sequential check runner.

**Tech Stack:** Python 3.12, `rich` (new — first external dependency), pytest, argparse.

**Issue:** vergil-project/vergil-tooling#1419
**Spec:** `docs/specs/2026-06-05-progress-framework-design.md` (status: Reviewed — read it first)

---

## Repo ground rules (read before Task 1)

- Work **only** inside the worktree `.worktrees/issue-1419-progress-framework/` on branch
  `feature/1419-progress-framework`. `cd` there for every Bash command.
- Use `vrg-git` (never `git`) and `vrg-commit` (never `git commit`). `vrg-commit` usage:
  `vrg-commit --type <type> --scope <scope> --message "<desc>" [--body "<body>"]`
  It commits whatever is staged — stage with `vrg-git add <paths>` first.
- Heredocs (`<<EOF`) are blocked in this environment. Pass multi-line content via files
  (Write tool), never heredocs.
- Tests run inside the dev container: `vrg-container-run -- pytest <args>`.
  Final validation: `vrg-container-run -- vrg-validate` (the only full-validation command).
- Style: `from __future__ import annotations`, full type hints, double quotes, ruff
  line-length 100, py312. Subprocess calls carry `# noqa: S603` (and `# noqa: S607` for
  partial paths) — copy the patterns you see in existing code.

## File structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | modify | add `rich` dependency |
| `src/vergil_tooling/lib/progress.py` | create | entire framework (spec mandates a single module) |
| `tests/vergil_tooling/test_progress.py` | create | all framework tests |
| `src/vergil_tooling/lib/output.py` | modify | `is_ci()` re-pointed at `$GITHUB_ACTIONS` |
| `tests/vergil_tooling/test_output.py` | modify | update `is_ci` tests |
| `src/vergil_tooling/lib/git.py` | modify | `run()` streams via `progress.run` |
| `tests/vergil_tooling/test_git.py` | modify | update `run()` tests |
| `src/vergil_tooling/lib/release/subprocess.py` | modify | drop `verbose`, stream pollers |
| `src/vergil_tooling/lib/release/merge.py` | modify | drop `verbose` param |
| `src/vergil_tooling/lib/release/confirm.py` | modify | drop `verbose` + `skip_cd_docs` logic |
| `src/vergil_tooling/lib/release/bump.py` | modify | drop `verbose` arg at call site |
| `src/vergil_tooling/lib/release/preflight.py` | modify | extract audit; drop `verbose`/`skip_audit` |
| `src/vergil_tooling/lib/release/context.py` | modify | drop `verbose` + `skip_cd_docs` fields |
| `src/vergil_tooling/lib/release/orchestrator.py` | modify | `build_stages()` replaces `run_release()` |
| `src/vergil_tooling/bin/vrg_release.py` | modify | `run_pipeline` wiring; flag changes |
| `src/vergil_tooling/bin/vrg_validate.py` | modify | stage-based check runner |
| `tests/vergil_tooling/test_release_*.py`, `test_release.py` | modify | follow source changes |

`github.run()` is deliberately **not** modified: its captured-then-printed output is routed
through the framework automatically by the stdout redirection in `run_pipeline` (gh commands
are short; the long pollers are handled in Task 10).

---

### Task 1: Add the `rich` dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` (generated)

- [ ] **Step 1: Edit `pyproject.toml`**

Change line `dependencies = []` to:

```toml
dependencies = ["rich>=13.0"]
```

- [ ] **Step 2: Regenerate the lockfile**

Run (from the worktree root): `uv lock`
Expected: `uv.lock` modified, exit 0.

- [ ] **Step 3: Verify rich imports inside the container**

Run: `vrg-container-run -- python -c "import rich; print(rich.__version__)"`
Expected: a version string ≥ 13. (If the container resolves deps at validate-install time and
this fails, run `vrg-container-run -- pip install -e .` once, then retry.)

- [ ] **Step 4: Commit**

```bash
vrg-git add pyproject.toml uv.lock
vrg-commit --type build --scope deps --message "add rich as first external dependency" --body "Required by the progress framework TTY renderer (spec: docs/specs/2026-06-05-progress-framework-design.md). Refs #1419"
```

---

### Task 2: Core types, detection, and formatting

**Files:**
- Create: `src/vergil_tooling/lib/progress.py`
- Create: `tests/vergil_tooling/test_progress.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/vergil_tooling/test_progress.py`:

```python
"""Tests for vergil_tooling.lib.progress."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib import progress
from vergil_tooling.lib.progress import PipelineError, Stage, StageResult

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_MOD = "vergil_tooling.lib.progress"


def test_stage_defaults() -> None:
    stage = Stage("audit", lambda ctx: None, mode="fail_fast")
    assert stage.skip_flag is None


def test_pipeline_error_message_single() -> None:
    failures = [StageResult("build-images", "failed", 4.0, "boom")]
    err = PipelineError(failures)
    assert str(err) == "1 stage failed (build-images)"
    assert err.failures == failures


def test_pipeline_error_message_plural() -> None:
    failures = [
        StageResult("a", "failed", 1.0, "x"),
        StageResult("b", "failed", 2.0, "y"),
    ]
    assert str(PipelineError(failures)) == "2 stages failed (a, b)"


def test_format_elapsed_seconds() -> None:
    assert progress.format_elapsed(3.21) == "3.2s"


def test_format_elapsed_minutes() -> None:
    assert progress.format_elapsed(61) == "1m01s"


def test_format_total() -> None:
    assert progress.format_total(61) == "01:01"
    assert progress.format_total(58) == "00:58"


def test_is_github_actions_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert progress.is_github_actions() is True


def test_is_github_actions_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert progress.is_github_actions() is False


def test_detect_format_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True
        assert progress.detect_format() == "rich"


def test_detect_format_gha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        assert progress.detect_format() == "gha"


def test_detect_format_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        assert progress.detect_format() == "plain"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.progress'`

- [ ] **Step 3: Write the implementation**

Create `src/vergil_tooling/lib/progress.py`:

```python
"""Stage-aware progress framework for long-running, human-invoked commands.

Design: docs/specs/2026-06-05-progress-framework-design.md
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/progress.py tests/vergil_tooling/test_progress.py
vrg-commit --type feat --scope progress --message "add core stage types, detection, and formatting" --body "Stage/StageResult/PipelineError dataclasses, renderer auto-detection (TTY->rich, GITHUB_ACTIONS->gha, else plain), and elapsed/total formatters. Refs #1419"
```

---

### Task 3: `RunLog` — timestamped log with ANSI stripping and prune-on-start

**Files:**
- Modify: `src/vergil_tooling/lib/progress.py`
- Modify: `tests/vergil_tooling/test_progress.py`

- [ ] **Step 1: Write the failing tests** (append to `test_progress.py`)

```python
def test_runlog_creates_timestamped_file(tmp_path: Path) -> None:
    log = progress.RunLog("vrg-release", tmp_path)
    assert log.path.parent == tmp_path / ".vergil"
    assert log.path.name.startswith("vrg-release-")
    assert log.path.name.endswith(".log")
    log.close()


def test_runlog_write_strips_ansi(tmp_path: Path) -> None:
    log = progress.RunLog("vrg-release", tmp_path)
    log.write("\x1b[32mgreen\x1b[0m line")
    log.close()
    assert log.path.read_text() == "green line\n"


def test_runlog_prunes_to_retain_count(tmp_path: Path) -> None:
    log_dir = tmp_path / ".vergil"
    log_dir.mkdir()
    for i in range(25):
        (log_dir / f"vrg-release-20260101-{i:06d}.log").write_text("old")
    (log_dir / "vrg-validate-20260101-000000.log").write_text("other command")
    log = progress.RunLog("vrg-release", tmp_path)
    log.close()
    release_logs = sorted(log_dir.glob("vrg-release-*.log"))
    assert len(release_logs) == progress.LOG_RETAIN
    # newest survives, oldest pruned, other commands untouched
    assert log.path in release_logs
    assert (log_dir / "vrg-validate-20260101-000000.log").exists()
    assert not (log_dir / "vrg-release-20260101-000000.log").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -k runlog -v`
Expected: FAIL — `AttributeError: ... has no attribute 'RunLog'`

- [ ] **Step 3: Implement** (append to `progress.py`; add `from datetime import datetime`
and `from pathlib import Path` to the imports — `Path` moves out of `TYPE_CHECKING` since
it is used at runtime)

```python
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
```

(If ruff complains about `DTZ005` with a different code or not at all, match whatever the
linter actually asks for — the intent is naive local time, which is correct for a local
log filename.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/progress.py tests/vergil_tooling/test_progress.py
vrg-commit --type feat --scope progress --message "add RunLog with ANSI stripping and prune-on-start" --body "Timestamped .vergil/<command>-YYYYMMDD-HHMMSS.log, always flushed; keeps the most recent 20 logs per command. Refs #1419"
```

---

### Task 4: Summary builder + Plain and GHA renderers

**Files:**
- Modify: `src/vergil_tooling/lib/progress.py`
- Modify: `tests/vergil_tooling/test_progress.py`

- [ ] **Step 1: Write the failing tests** (append; add `import io` and
`from pathlib import Path` to the test file's runtime imports)

```python
def _results_mixed() -> list[StageResult]:
    return [
        StageResult("audit", "skipped", 0.0, "skipped via --skip-audit"),
        StageResult("changelog", "ok", 3.2),
        StageResult("build-images", "failed", 65.0, "docker build exited 1"),
    ]


def test_build_summary_with_failures(tmp_path: Path) -> None:
    text = progress.build_summary("release 2.1.2", _results_mixed(), 61.0, tmp_path / "x.log")
    assert "⚠  warnings (non-fatal):" in text
    assert "audit — skipped via --skip-audit" in text
    assert "✗  failures:" in text
    assert "build-images — docker build exited 1" in text
    assert "release 2.1.2 completed with errors  (total: 01:01)" in text
    assert "PipelineError: 1 stage failed (build-images) · exit 1" in text
    assert str(tmp_path / "x.log") in text


def test_build_summary_success(tmp_path: Path) -> None:
    text = progress.build_summary(
        "release 2.1.2", [StageResult("changelog", "ok", 3.2)], 58.0, tmp_path / "x.log"
    )
    assert "✓  release 2.1.2 complete  (total: 00:58)" in text
    assert "PipelineError" not in text


def test_plain_renderer_lifecycle() -> None:
    out = io.StringIO()
    r = progress.PlainRenderer(out)
    r.start_stage("audit")
    r.stage_line("checking things")
    r.end_stage(StageResult("audit", "ok", 2.1))
    r.end_stage(StageResult("docs", "skipped", 0.0, "skipped via --skip-docs"))
    r.summary("SUMMARY")
    r.close()
    text = out.getvalue()
    assert "→ audit  starting..." in text
    assert "checking things" in text
    assert "✓ audit  2.1s" in text
    assert "⚠ docs — skipped via --skip-docs" in text
    assert "SUMMARY" in text


def test_gha_renderer_groups_and_error() -> None:
    out = io.StringIO()
    r = progress.GhaRenderer(out)
    r.start_stage("audit")
    r.stage_line("checking")
    r.end_stage(StageResult("audit", "failed", 2.0, "boom"))
    r.summary("SUMMARY")
    r.close()
    text = out.getvalue()
    assert "::group::audit\n" in text
    assert "checking\n" in text
    assert "::endgroup::\n" in text
    assert "::error title=audit failed::boom" in text
    assert "SUMMARY" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -k "summary or renderer" -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement** (append to `progress.py`; add `IO` to the `typing` import and
`from collections.abc import Sequence` under `TYPE_CHECKING`)

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/progress.py tests/vergil_tooling/test_progress.py
vrg-commit --type feat --scope progress --message "add summary builder and plain/GHA renderers" --body "Shared plain-text final summary; PlainRenderer flat output; GhaRenderer ::group:: sections with ::error annotations on failed stages. Refs #1419"
```

---

### Task 5: `RichRenderer`

**Files:**
- Modify: `src/vergil_tooling/lib/progress.py`
- Modify: `tests/vergil_tooling/test_progress.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
def _rich_renderer() -> tuple[progress.RichRenderer, io.StringIO]:
    out = io.StringIO()
    return progress.RichRenderer(out, window=2, force_terminal=True), out


def test_rich_renderer_lifecycle_and_window() -> None:
    r, out = _rich_renderer()
    r.start_stage("build")
    for i in range(5):
        r.stage_line(f"line {i}")
    assert list(r._tail) == ["line 3", "line 4"]  # window of 2
    r.end_stage(StageResult("build", "ok", 2.1))
    assert r._active is None
    assert not r._tail
    r.summary("SUMMARY TEXT")
    r.close()
    assert "SUMMARY TEXT" in out.getvalue()
    assert "build" in out.getvalue()


def test_rich_renderer_window_zero_streams() -> None:
    out = io.StringIO()
    r = progress.RichRenderer(out, window=0, force_terminal=True)
    r.start_stage("build")
    r.stage_line("streamed line")
    r.end_stage(StageResult("build", "ok", 1.0))
    r.summary("S")
    r.close()
    assert "streamed line" in out.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -k rich -v`
Expected: FAIL — `AttributeError: ... no attribute 'RichRenderer'`

- [ ] **Step 3: Implement** (append; add `from collections import deque` and the rich imports
at module top: `from rich.console import Console, Group`, `from rich.live import Live`,
`from rich.spinner import Spinner`, `from rich.text import Text`)

```python
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
                parts.extend(
                    Text.from_ansi(f"   {line}", style="dim") for line in self._tail
                )
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
        self._completed.append(
            Text(_status_line(result), style=_STATUS_STYLES[result.status])
        )
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -v`
Expected: all PASS. (If `Live.is_started` does not exist in the installed rich version,
track started state with a `self._started` bool set in `__init__`/`summary` instead.)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/progress.py tests/vergil_tooling/test_progress.py
vrg-commit --type feat --scope progress --message "add RichRenderer rolling-window live display" --body "rich Live display: completed stages collapse to colored status lines, active stage shows spinner plus last-N window; window=0 streams then collapses. Refs #1419"
```

---

### Task 6: Session plumbing, `emit()`, print capture, and the streaming subprocess runner

**Files:**
- Modify: `src/vergil_tooling/lib/progress.py`
- Modify: `tests/vergil_tooling/test_progress.py`

This is the subprocess execution contract from the spec. Key design: a module-global
`_session` holds the active renderer + log. `emit(line)` routes a line to both (or plain
`print`s when no pipeline is active, so library code behaves sanely outside pipelines).
`_EmitWriter` is a file-like object used by `run_pipeline` (Task 7) to redirect
`sys.stdout`/`sys.stderr` during stage execution, so every bare `print()` in stage code
routes through `emit`. Renderers write to the **real** stdout captured at session start,
so there is no recursion.

- [ ] **Step 1: Write the failing tests** (append; add `import subprocess` and `import sys`
to the test file)

```python
class _FakeRenderer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def start_stage(self, name: str) -> None:
        pass

    def stage_line(self, line: str) -> None:
        self.lines.append(line)

    def end_stage(self, result: StageResult) -> None:
        pass

    def summary(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


def _fake_session(tmp_path: Path) -> tuple[_FakeRenderer, progress.RunLog]:
    renderer = _FakeRenderer()
    log = progress.RunLog("test-cmd", tmp_path)
    progress._session = progress._Session(renderer=renderer, log=log)
    return renderer, log


def test_emit_without_session_prints(capsys: pytest.CaptureFixture[str]) -> None:
    progress._session = None
    progress.emit("hello")
    assert capsys.readouterr().out == "hello\n"


def test_emit_with_session_routes_to_renderer_and_log(tmp_path: Path) -> None:
    renderer, log = _fake_session(tmp_path)
    try:
        progress.emit("\x1b[31mred\x1b[0m line")
    finally:
        progress._session = None
        log.close()
    assert renderer.lines == ["\x1b[31mred\x1b[0m line"]  # raw to renderer
    assert log.path.read_text() == "red line\n"  # stripped in log


def test_emit_writer_buffers_partial_lines(tmp_path: Path) -> None:
    renderer, log = _fake_session(tmp_path)
    try:
        w = progress._EmitWriter()
        w.write("par")
        w.write("tial\nsecond\nthi")
        w.flush()  # flushes the partial third line
    finally:
        progress._session = None
        log.close()
    assert renderer.lines == ["partial", "second", "thi"]


def test_run_streams_lines_to_session(tmp_path: Path) -> None:
    renderer, log = _fake_session(tmp_path)
    try:
        rc = progress.run(
            (sys.executable, "-c", "import sys; print('out1'); print('err1', file=sys.stderr)")
        )
    finally:
        progress._session = None
        log.close()
    assert rc == 0
    assert "out1" in renderer.lines
    assert "err1" in renderer.lines


def test_run_raises_with_captured_output(tmp_path: Path) -> None:
    progress._session = None
    code = "import sys; print('so long'); print('bad', file=sys.stderr); sys.exit(3)"
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        progress.run((sys.executable, "-c", code))
    assert excinfo.value.returncode == 3
    assert "so long" in excinfo.value.output
    assert "bad" in excinfo.value.stderr


def test_run_check_false_returns_code() -> None:
    progress._session = None
    rc = progress.run((sys.executable, "-c", "import sys; sys.exit(2)"), check=False)
    assert rc == 2
```

Also add `import pytest` to the test file's runtime imports (it is currently only under
`TYPE_CHECKING` — move it out, `pytest.raises` is a runtime use).

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -k "emit or run_" -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement** (append; add `import io`, `import subprocess`, `import threading`
to module imports)

```python
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
```

Note: `Sequence` is needed at runtime now — move it out of `TYPE_CHECKING` or import it
at runtime (`from collections.abc import Callable, Sequence` stays under `TYPE_CHECKING`
only if annotations remain strings via `from __future__ import annotations`; it does, so
the TYPE_CHECKING import is fine — verify mypy/ty accepts it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/progress.py tests/vergil_tooling/test_progress.py
vrg-commit --type feat --scope progress --message "add session routing, print capture, and streaming subprocess runner" --body "emit() routes lines to the active renderer and log (plain print outside pipelines); _EmitWriter captures redirected stdout/stderr; progress.run() streams child output line-by-line and raises CalledProcessError carrying captured output. Refs #1419"
```

---

### Task 7: `run_pipeline()` and `add_progress_args()`

**Files:**
- Modify: `src/vergil_tooling/lib/progress.py`
- Modify: `tests/vergil_tooling/test_progress.py`

- [ ] **Step 1: Write the failing tests** (append; add `import argparse` to the test file)

```python
def _args(**skips: bool) -> argparse.Namespace:
    return argparse.Namespace(output_window=5, output_format="plain", **skips)


def _pipeline(tmp_path: Path, stages: list[Stage], args: argparse.Namespace) -> int:
    return progress.run_pipeline(
        None, stages, command="test-cmd", label="test", args=args, repo_root=tmp_path
    )


def test_pipeline_success_exit_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    stages = [
        Stage("one", lambda ctx: calls.append("one"), mode="fail_fast"),
        Stage("two", lambda ctx: calls.append("two"), mode="fail_defer"),
    ]
    assert _pipeline(tmp_path, stages, _args()) == 0
    assert calls == ["one", "two"]
    out = capsys.readouterr().out
    assert "✓  test complete" in out


def test_pipeline_stage_print_is_captured(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stages = [Stage("noisy", lambda ctx: print("deep print"), mode="fail_defer")]
    _pipeline(tmp_path, stages, _args())
    assert "deep print" in capsys.readouterr().out
    logs = list((tmp_path / ".vergil").glob("test-cmd-*.log"))
    assert len(logs) == 1
    assert "deep print" in logs[0].read_text()


def _boom(ctx: object) -> None:
    msg = "boom"
    raise RuntimeError(msg)


def test_pipeline_fail_defer_continues(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    stages = [
        Stage("bad", _boom, mode="fail_defer"),
        Stage("after", lambda ctx: calls.append("after"), mode="fail_defer"),
    ]
    assert _pipeline(tmp_path, stages, _args()) == 1
    assert calls == ["after"]
    out = capsys.readouterr().out
    assert "✗  failures:" in out
    assert "bad — RuntimeError: boom" in out


def test_pipeline_fail_fast_stops(tmp_path: Path) -> None:
    calls: list[str] = []
    stages = [
        Stage("bad", _boom, mode="fail_fast"),
        Stage("after", lambda ctx: calls.append("after"), mode="fail_fast"),
    ]
    assert _pipeline(tmp_path, stages, _args()) == 1
    assert calls == []


def test_pipeline_warn_does_not_affect_exit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stages = [Stage("meh", _boom, mode="warn")]
    assert _pipeline(tmp_path, stages, _args()) == 0
    assert "⚠  warnings (non-fatal):" in capsys.readouterr().out


def test_pipeline_skip_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    stages = [
        Stage("audit", lambda ctx: calls.append("audit"), mode="fail_fast", skip_flag="skip_audit"),
        Stage("after", lambda ctx: calls.append("after"), mode="fail_defer"),
    ]
    assert _pipeline(tmp_path, stages, _args(skip_audit=True)) == 0
    assert calls == ["after"]  # audit never ran
    assert "audit — skipped via --skip-audit" in capsys.readouterr().out


def _interrupt(ctx: object) -> None:
    raise KeyboardInterrupt


def test_pipeline_interrupt_exits_130(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    stages = [
        Stage("slow", _interrupt, mode="fail_defer"),
        Stage("after", lambda ctx: calls.append("after"), mode="fail_defer"),
    ]
    assert _pipeline(tmp_path, stages, _args()) == 130
    assert calls == []
    out = capsys.readouterr().out
    assert "slow — interrupted" in out
    assert "full log →" in out


def test_pipeline_traceback_in_log(tmp_path: Path) -> None:
    stages = [Stage("bad", _boom, mode="fail_defer")]
    _pipeline(tmp_path, stages, _args())
    log = next((tmp_path / ".vergil").glob("test-cmd-*.log"))
    assert "Traceback" in log.read_text()


def test_add_progress_args_generates_flags() -> None:
    parser = argparse.ArgumentParser()
    stages = [Stage("audit", lambda ctx: None, mode="fail_fast", skip_flag="skip_audit")]
    progress.add_progress_args(parser, stages)
    args = parser.parse_args(["--skip-audit", "--output-window", "3"])
    assert args.skip_audit is True
    assert args.output_window == 3
    assert args.output_format is None
    args = parser.parse_args([])
    assert args.skip_audit is False
    assert args.output_window == 5


def test_add_progress_args_rejects_skip_on_non_fail_fast() -> None:
    parser = argparse.ArgumentParser()
    stages = [Stage("docs", lambda ctx: None, mode="fail_defer", skip_flag="skip_docs")]
    with pytest.raises(ValueError, match="skip_flag is only supported on fail_fast stages"):
        progress.add_progress_args(parser, stages)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -k "pipeline or progress_args" -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Implement** (append; add `import argparse`, `import contextlib`,
`import time`, `import traceback` to module imports)

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_progress.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/progress.py tests/vergil_tooling/test_progress.py
vrg-commit --type feat --scope progress --message "add run_pipeline and add_progress_args" --body "Pipeline runner with warn/fail_defer/fail_fast modes, skip-flag escape hatches, stdout/stderr capture during stages, KeyboardInterrupt summary with exit 130, and auto-generated CLI flags. Refs #1419"
```

---

### Task 8: Re-point `output.is_ci()` at `$GITHUB_ACTIONS`

**Files:**
- Modify: `src/vergil_tooling/lib/output.py`
- Modify: `tests/vergil_tooling/test_output.py`

- [ ] **Step 1: Update the tests first.** In `tests/vergil_tooling/test_output.py`, replace
the two `is_ci` tests (`test_is_ci_returns_true_when_not_a_tty`,
`test_is_ci_returns_false_when_tty`) with:

```python
def test_is_ci_true_under_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert is_ci() is True


def test_is_ci_false_when_merely_piped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        assert is_ci() is False
```

(`pytest` is currently imported under `TYPE_CHECKING` in this file — that is fine, the
`MonkeyPatch` annotation is a string.)

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_output.py -k is_ci -v`
Expected: `test_is_ci_true_under_github_actions` FAILS (current impl checks isatty).

- [ ] **Step 3: Implement.** In `src/vergil_tooling/lib/output.py`, replace:

```python
def is_ci() -> bool:
    return not sys.stdout.isatty()
```

with:

```python
def is_ci() -> bool:
    """True when actually running under GitHub Actions.

    Detection is owned by lib/progress.py; this fixes the old behavior where
    merely piped output (local pipes, agent runs) got ::error:: annotations.
    """
    return is_github_actions()
```

and add the import: `from vergil_tooling.lib.progress import is_github_actions`.
Update the module docstring's "Detection:" line to say detection is
`$GITHUB_ACTIONS == "true"` (owned by `lib/progress.py`), not `isatty`.

- [ ] **Step 4: Run the full test suite for fallout**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_output.py -v`
Expected: all PASS. Then `vrg-container-run -- pytest tests/ -x -q` — if anything else
patched `output.is_ci` indirectly via isatty, fix it to patch `output.is_ci` directly
(most existing tests already do).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/output.py tests/vergil_tooling/test_output.py
vrg-commit --type fix --scope output --message "re-point is_ci at GITHUB_ACTIONS env" --body "Detection ownership moves to lib/progress.py. Fixes piped-local and agent-run output receiving GHA ::error:: annotations meant for the Actions log parser. Refs #1419"
```

---

### Task 9: `git.run()` streams via `progress.run`

**Files:**
- Modify: `src/vergil_tooling/lib/git.py`
- Modify: `tests/vergil_tooling/test_git.py`

- [ ] **Step 1: Replace the implementation.** In `src/vergil_tooling/lib/git.py`, replace the
body of `run()` (currently `subprocess.run(..., capture_output=True)` + post-success prints,
lines ~36–56) with:

```python
def run(*args: str) -> None:
    """Run a git command, streaming output, and raise on failure."""
    env = _remote_env(args)
    progress.run(("git", *args), env=env)
```

Add the import `from vergil_tooling.lib import progress` (no circularity:
`progress.py` imports nothing from `lib`). Leave `read_output()` and everything else alone.

- [ ] **Step 2: Update the `run()` tests.** In `tests/vergil_tooling/test_git.py` the six
`test_run_*` tests assert against `subprocess.run`. Rewrite them to patch
`vergil_tooling.lib.git.progress.run` instead. The behavioral contract is unchanged
(output visible, errors carry output) but the mechanism is now delegation:

```python
def test_run_delegates_to_progress_run() -> None:
    with patch("vergil_tooling.lib.git.progress") as m_progress:
        git.run("status")
    m_progress.run.assert_called_once_with(("git", "status"), env=None)


def test_run_raises_on_failure() -> None:
    err = subprocess.CalledProcessError(1, ("git", "status"), output="so", stderr="se")
    with (
        patch("vergil_tooling.lib.git.progress") as m_progress,
        pytest.raises(subprocess.CalledProcessError) as excinfo,
    ):
        m_progress.run.side_effect = err
        git.run("status")
    assert excinfo.value.output == "so"
    assert excinfo.value.stderr == "se"
```

Keep `test_run_commit_no_env_var_gate` (it asserts env handling — adapt its assertion to
inspect the `env=` kwarg passed to `progress.run`). Delete
`test_run_prints_captured_output`, `test_run_prints_stderr_on_error`, and
`test_run_error_no_output` — printing is now the streaming runner's job, covered by
`test_progress.py`. Adapt `test_run_error_carries_output` to the pattern above.

- [ ] **Step 3: Run tests**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_git.py -v`
Expected: all PASS.

- [ ] **Step 4: Run the full suite for fallout** (callers that patched `git.run` internals)

Run: `vrg-container-run -- pytest tests/ -q`
Expected: PASS. Fix any test that patched `subprocess.run` inside `git.run` by patching
`progress.run` instead.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/git.py tests/vergil_tooling/test_git.py
vrg-commit --type refactor --scope git --message "stream git subprocess output via progress.run" --body "git.run() delegates to the progress framework's streaming runner: lines reach the live renderer and run log as they arrive, and CalledProcessError still carries captured output. Refs #1419"
```

---

### Task 10: Stream the long pollers; remove the `--verbose` plumbing

**Files:**
- Modify: `src/vergil_tooling/lib/release/subprocess.py`
- Modify: `src/vergil_tooling/lib/release/merge.py`
- Modify: `src/vergil_tooling/lib/release/confirm.py:80`
- Modify: `src/vergil_tooling/lib/release/bump.py:36`
- Modify: `src/vergil_tooling/lib/release/preflight.py` (drop `verbose` param only)
- Modify: `src/vergil_tooling/lib/release/context.py` (drop `verbose` field)
- Modify: `tests/vergil_tooling/test_release_subprocess.py`, `test_release_merge.py`,
  `test_release_confirm.py`, `test_release_preflight.py`, `test_release_context.py`
  (whichever assert on `verbose`)

Per the spec, `--verbose` is subsumed by the rolling window + always-on log. The noisy
pollers (`gh pr checks --watch`, `gh run watch`) become streaming calls that **retain
transient retry** — GitHub API flakiness makes retry essential. `progress.run` raises
`CalledProcessError` carrying captured output, which is exactly what `retry.is_retryable`
inspects, so streaming and retry compose via a small wrapper. The wrapper also preserves
`_gh_env()` credential injection (which `_run_with_retry` previously provided).

- [ ] **Step 1: Rewrite `lib/release/subprocess.py`.** Replace `_run_verbose` and the
`verbose` parameters:

```python
"""Subprocess wrappers for noisy release commands (streamed via progress)."""

from __future__ import annotations

import subprocess
import time

from vergil_tooling.lib import progress, retry
from vergil_tooling.lib.github import (
    GitHubAPIError,
    _checks_registered,
    _gh_env,
    current_repo,
    head_sha,
)

_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 180


def _stream_with_retry(cmd: tuple[str, ...]) -> None:
    """Stream *cmd* via progress.run, retrying transient GitHub failures.

    Streaming-compatible analogue of github._run_with_retry: progress.run
    raises CalledProcessError carrying captured output, which is what
    retry.is_retryable inspects. Preserves _gh_env credential injection.
    """
    env = _gh_env()
    for attempt in range(retry.MAX_RETRIES + 1):
        try:
            progress.run(cmd, env=env)
        except subprocess.CalledProcessError as exc:
            if attempt == retry.MAX_RETRIES or not retry.is_retryable(exc):
                raise
            delay = retry.compute_delay(attempt)
            progress.emit(
                f"transient GitHub failure, retrying in {delay:.1f}s"
                f" (attempt {attempt + 1}/{retry.MAX_RETRIES + 1})"
            )
            time.sleep(delay)
        else:
            return
    raise AssertionError("unreachable")  # pragma: no cover


def wait_for_checks(pr: str) -> None:
    """Block until CI checks on *pr* pass, streaming watch output."""
    repo = current_repo()
    sha = head_sha(pr)

    deadline = time.monotonic() + _POLL_TIMEOUT_SECS
    while not _checks_registered(repo, sha):
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_SECS)

    if not _checks_registered(repo, sha):
        raise GitHubAPIError(
            1,
            ("gh", "pr", "checks", pr, "--watch"),
            stderr=(
                f"no checks reported for {sha[:8]} after {_POLL_TIMEOUT_SECS}s"
                " — GitHub may be experiencing delays"
            ),
        )

    _stream_with_retry(("gh", "pr", "checks", pr, "--watch"))  # noqa: S607


def watch_workflow(repo: str, run_id: str, *, check_status: bool = True) -> None:
    """Block until a workflow run completes, streaming watch output."""
    cmd: tuple[str, ...] = ("gh", "run", "watch", "--repo", repo)
    if check_status:
        cmd = (*cmd, "--exit-status")
    cmd = (*cmd, run_id)
    _stream_with_retry(cmd)  # noqa: S607
```

Add tests to `test_release_subprocess.py` for the retry wrapper (adapt `_MOD` to the
file's convention; patch `time.sleep` to keep the test instant):

```python
def test_stream_with_retry_passes_gh_env() -> None:
    with (
        patch(_MOD + ".progress") as m_progress,
        patch(_MOD + "._gh_env", return_value={"GH_TOKEN": "x"}),
    ):
        release_subprocess._stream_with_retry(("gh", "run", "watch"))
    m_progress.run.assert_called_once_with(("gh", "run", "watch"), env={"GH_TOKEN": "x"})


def test_stream_with_retry_retries_transient_then_succeeds() -> None:
    transient = subprocess.CalledProcessError(1, ("gh",), output="", stderr="HTTP 502")
    with (
        patch(_MOD + ".progress") as m_progress,
        patch(_MOD + "._gh_env", return_value=None),
        patch(_MOD + ".time"),
    ):
        m_progress.run.side_effect = [transient, None]
        release_subprocess._stream_with_retry(("gh", "run", "watch"))
    assert m_progress.run.call_count == 2


def test_stream_with_retry_propagates_non_transient() -> None:
    fatal = subprocess.CalledProcessError(1, ("gh",), output="", stderr="not found")
    with (
        patch(_MOD + ".progress") as m_progress,
        patch(_MOD + "._gh_env", return_value=None),
        pytest.raises(subprocess.CalledProcessError),
    ):
        m_progress.run.side_effect = fatal
        release_subprocess._stream_with_retry(("gh", "run", "watch"))
    assert m_progress.run.call_count == 1


def test_stream_with_retry_gives_up_after_max() -> None:
    transient = subprocess.CalledProcessError(1, ("gh",), output="", stderr="HTTP 502")
    with (
        patch(_MOD + ".progress") as m_progress,
        patch(_MOD + "._gh_env", return_value=None),
        patch(_MOD + ".time"),
        pytest.raises(subprocess.CalledProcessError),
    ):
        m_progress.run.side_effect = transient
        release_subprocess._stream_with_retry(("gh", "run", "watch"))
    assert m_progress.run.call_count == retry.MAX_RETRIES + 1
```

- [ ] **Step 2: Update the callers.**

`lib/release/merge.py`: `wait_and_merge(pr_url: str, *, phase: str) -> None` — drop the
`verbose` param and pass-through (`wait_for_checks(pr_url)`).
`lib/release/confirm.py:80`: `watch_workflow(ctx.repo, run_id, check_status=check_status)`.
`lib/release/bump.py:36`: `wait_and_merge(pr_url, phase="back-merge-bump")`.
`lib/release/orchestrator.py:43-47`: `wait_and_merge(ctx.release_pr_url, phase="merge-release")`.
`lib/release/preflight.py`: remove the `verbose` parameter and the `verbose=verbose`
argument to `ReleaseContext` (lines 23, 57).
`lib/release/context.py`: delete the `verbose: bool = False` field (line 20).

- [ ] **Step 3: Run the suite and fix fallout**

Run: `vrg-container-run -- pytest tests/ -q`
Expected failures in `test_release_subprocess.py` (patched `_run_verbose` /
`_run_with_retry`) and any test constructing `ReleaseContext(verbose=...)` or calling the
changed signatures. Fix pattern: patch
`vergil_tooling.lib.release.subprocess._stream_with_retry` and assert it was called with
the expected command tuple; delete `verbose=` kwargs from `ReleaseContext` constructions.
Re-run until green.

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/ tests/vergil_tooling/
vrg-commit --type refactor --scope release --message "stream CI pollers with retry and drop --verbose plumbing" --body "wait_for_checks/watch_workflow stream through the progress framework via _stream_with_retry, retaining transient-failure retry and _gh_env credential injection; the verbose flag chain (context field, preflight/merge/confirm params) is removed — subsumed by the rolling window and always-on run log. Refs #1419"
```

---

### Task 11: Extract the audit stage from preflight

**Files:**
- Modify: `src/vergil_tooling/lib/release/preflight.py`
- Modify: `tests/vergil_tooling/test_release_preflight.py`

- [ ] **Step 1: Make the audit standalone.** In `lib/release/preflight.py`:

1. Rename `_check_gh_auth` → `check_gh_auth` and `_audit_repo_config` → `audit_repo_config`
   (public; keep behavior identical). Update internal references.
2. Remove the `skip_audit` parameter from `preflight()` and delete lines 31–32
   (`if not skip_audit: _audit_repo_config(repo)`). Preflight keeps its own
   `check_gh_auth()` call for `repo`.
3. Add a module-level convenience used by the orchestrator's audit stage:

```python
def run_audit() -> None:
    """Standalone audit stage: resolve the repo and audit its GitHub config."""
    audit_repo_config(check_gh_auth())
```

- [ ] **Step 2: Update tests.** In `test_release_preflight.py`: remove `skip_audit=` kwargs;
any test asserting "audit is skipped when skip_audit=True" becomes a test that
`preflight()` never calls `audit_repo_config`; add:

```python
def test_run_audit_resolves_repo_and_audits() -> None:
    with (
        patch(_MOD + ".check_gh_auth", return_value="owner/repo") as m_auth,
        patch(_MOD + ".audit_repo_config") as m_audit,
    ):
        preflight_module.run_audit()
    m_auth.assert_called_once_with()
    m_audit.assert_called_once_with("owner/repo")
```

(Adapt `_MOD` / import names to the file's existing conventions.)

- [ ] **Step 3: Run tests**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_release_preflight.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/preflight.py tests/vergil_tooling/test_release_preflight.py
vrg-commit --type refactor --scope release --message "extract repo-config audit into a standalone stage function" --body "The audit becomes its own fail_fast pipeline stage with skip_flag=skip_audit; preflight no longer takes skip_audit. Refs #1419"
```

---

### Task 12: Rewrite the orchestrator as a stage list

**Files:**
- Modify: `src/vergil_tooling/lib/release/orchestrator.py`
- Modify: `tests/vergil_tooling/test_release_orchestrator.py`

Stage modes (rationale): everything through `back-merge-bump` is a hard sequential
dependency → `fail_fast`. `confirm-develop`, `promote`, `close-finalize`,
`consumer-refresh` are verification/cleanup whose failure should not block the remaining
steps → `fail_defer` (the framework reports them at the end — this replaces the old
`--skip-cd-docs` workaround).

- [ ] **Step 1: Rewrite `orchestrator.py`.** Keep `_phase_details` and `merge_release` /
`_promote_phase` exactly as they are (minus the `verbose` arg already removed in Task 10).
Delete `run_release` and `_format_elapsed`. Add:

```python
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.release.preflight import preflight, run_audit


@dataclass
class ReleaseState:
    """Pipeline context for vrg-release; ctx is populated by the preflight stage."""

    version_override: str | None
    repo_root: Path
    promote: bool
    ctx: ReleaseContext | None = None


def _audit_stage(state: ReleaseState) -> None:
    run_audit()


def _preflight_stage(state: ReleaseState) -> None:
    ctx = preflight(
        version_override=state.version_override,
        repo_root=state.repo_root,
    )
    ctx.promote = state.promote
    state.ctx = ctx


def _tracked(name: str, fn: Callable[[ReleaseContext], None]) -> Callable[[ReleaseState], None]:
    """Wrap a phase fn with tracking-issue comments (command-local, per spec)."""

    def stage(state: ReleaseState) -> None:
        ctx = state.ctx
        if ctx is None:
            raise ReleaseError(
                phase=name,
                command=name,
                message="release context missing — preflight did not run",
            )
        try:
            fn(ctx)
        except ReleaseError as exc:
            comment_phase_failed(ctx, name, exc)
            raise
        except Exception as exc:
            wrapped = ReleaseError(
                phase=name,
                command=str(getattr(exc, "cmd", type(exc).__name__)),
                message=str(exc),
                detail=(getattr(exc, "stderr", None) or getattr(exc, "stdout", None)),
            )
            comment_phase_failed(ctx, name, wrapped)
            raise wrapped from exc
        comment_phase_complete(ctx, name, _phase_details(ctx, name))

    return stage


def build_stages() -> list[Stage]:
    """The vrg-release pipeline, in execution order."""
    return [
        Stage("audit", _audit_stage, mode="fail_fast", skip_flag="skip_audit"),
        Stage("preflight", _preflight_stage, mode="fail_fast"),
        Stage("prepare", _tracked("prepare", prepare), mode="fail_fast"),
        Stage("merge-release", _tracked("merge-release", merge_release), mode="fail_fast"),
        Stage("confirm-main", _tracked("confirm-main", confirm_main), mode="fail_fast"),
        Stage("back-merge-bump", _tracked("back-merge-bump", back_merge_and_bump), mode="fail_fast"),
        Stage("confirm-develop", _tracked("confirm-develop", confirm_develop), mode="fail_defer"),
        Stage("promote", _tracked("promote", _promote_phase), mode="fail_defer"),
        Stage("close-finalize", _tracked("close-finalize", close_and_finalize), mode="fail_defer"),
        Stage("consumer-refresh", _tracked("consumer-refresh", consumer_refresh), mode="fail_defer"),
    ]
```

(`ReleaseContext` import moves out of `TYPE_CHECKING` only if needed at runtime — it is
not; annotations stay strings. `Callable` stays under `TYPE_CHECKING`. Remove the now-unused
`time` import.)

- [ ] **Step 2: Rewrite the orchestrator tests.** Replace `test_release_orchestrator.py`'s
`run_release`-based tests with stage-list tests (keep the `_ctx()` helper):

```python
def test_build_stages_order_and_modes() -> None:
    stages = build_stages()
    assert [s.name for s in stages] == [
        "audit", "preflight", "prepare", "merge-release", "confirm-main",
        "back-merge-bump", "confirm-develop", "promote", "close-finalize",
        "consumer-refresh",
    ]
    assert stages[0].skip_flag == "skip_audit"
    fail_fast = {s.name for s in stages if s.mode == "fail_fast"}
    assert fail_fast == {"audit", "preflight", "prepare", "merge-release",
                         "confirm-main", "back-merge-bump"}


def test_preflight_stage_populates_ctx() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=False)  # noqa: S108
    ctx = _ctx()
    with patch(_MOD + ".preflight", return_value=ctx) as m_preflight:
        _preflight_stage(state)
    m_preflight.assert_called_once_with(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    assert state.ctx is ctx
    assert ctx.promote is False


def test_tracked_stage_comments_on_success() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    state.ctx = _ctx()
    fn = MagicMock()
    with patch(_MOD + ".comment_phase_complete") as m_comment:
        _tracked("prepare", fn)(state)
    fn.assert_called_once_with(state.ctx)
    m_comment.assert_called_once()


def test_tracked_stage_wraps_and_comments_on_failure() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    state.ctx = _ctx()
    fn = MagicMock(side_effect=RuntimeError("boom"))
    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError, match="boom"),
    ):
        _tracked("prepare", fn)(state)
    m_failed.assert_called_once()


def test_tracked_stage_requires_ctx() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    with pytest.raises(ReleaseError, match="preflight did not run"):
        _tracked("prepare", MagicMock())(state)
```

(Add `MagicMock` to the mock imports; import `ReleaseState`, `_preflight_stage`,
`_tracked`, `build_stages` from the orchestrator; keep existing comment-phase tests that
still apply, delete the `run_release` ones.)

- [ ] **Step 3: Run tests**

Run: `vrg-container-run -- pytest tests/vergil_tooling/test_release_orchestrator.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/orchestrator.py tests/vergil_tooling/test_release_orchestrator.py
vrg-commit --type refactor --scope release --message "replace bespoke phase runner with declarative stage list" --body "build_stages() expresses the release pipeline as progress.Stage entries; tracking-issue comments stay command-local via the _tracked wrapper; audit becomes a skippable fail_fast stage and the back half of the pipeline becomes fail_defer. Refs #1419"
```

---

### Task 13: Wire `vrg-release` to `run_pipeline`; remove `--skip-cd-docs`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_release.py`
- Modify: `src/vergil_tooling/lib/release/confirm.py`
- Modify: `src/vergil_tooling/lib/release/context.py`
- Modify: `tests/vergil_tooling/test_release.py`, `test_release_confirm.py`,
  `test_release_context.py`

- [ ] **Step 1: Rewrite `bin/vrg_release.py`:**

```python
"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, progress
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the full release workflow from develop to main.",
    )
    parser.add_argument(
        "version_override",
        nargs="?",
        choices=("minor", "major"),
        default=None,
        help="Bump to next minor or major before releasing (default: release current version).",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        default=False,
        help="Skip rolling-tag promotion after release.",
    )
    progress.add_progress_args(parser, build_stages())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = git.repo_root()
    state = ReleaseState(
        version_override=args.version_override,
        repo_root=repo_root,
        promote=not args.no_promote,
    )
    return progress.run_pipeline(
        state,
        build_stages(),
        command="vrg-release",
        label="vrg-release",
        args=args,
        repo_root=repo_root,
    )


if __name__ == "__main__":
    sys.exit(main())
```

Removed: `--verbose`, `--skip-cd-docs`, the manual preflight block, the `ReleaseError`
handler (failures are rendered by the pipeline summary and logged with tracebacks).
`--skip-audit` is now auto-generated from the stage list.

- [ ] **Step 2: Remove `skip_cd_docs`.**

`lib/release/context.py`: delete the `skip_cd_docs: bool = False` field.
`lib/release/confirm.py`: in `confirm_main` and `confirm_develop`, delete the
`skip_docs = ctx.skip_cd_docs` lines and the two `if skip_docs:` blocks; call
`_watch_cd(ctx, branch=..., check_status=True)` unconditionally (drop the now-constant
`check_status` plumbing only if it is otherwise unused — `_watch_cd`'s `check_status`
param stays, `confirm_*` just stops passing `False`). The `expected` job tuples become
unconditional.

- [ ] **Step 3: Update tests.**

`test_release.py`: parse_args tests — delete `--verbose`/`--skip-cd-docs` cases; add:

```python
def test_parse_args_has_progress_flags() -> None:
    args = parse_args(["--skip-audit", "--output-format", "plain"])
    assert args.skip_audit is True
    assert args.output_format == "plain"


def test_main_runs_pipeline() -> None:
    with (
        patch("vergil_tooling.bin.vrg_release.git") as m_git,
        patch("vergil_tooling.bin.vrg_release.progress") as m_progress,
    ):
        m_progress.run_pipeline.return_value = 0
        assert main(["--no-promote", "--output-format", "plain"]) == 0
    state = m_progress.run_pipeline.call_args.args[0]
    assert state.promote is False
    assert state.repo_root is m_git.repo_root.return_value
```

(`parse_args` inside `main` is not patched, so `m_progress.add_progress_args` must still
add real flags — patch `progress` only in the `main` test by calling
`main(["--no-promote"])`… if the mocked `add_progress_args` breaks parsing, patch
`vergil_tooling.bin.vrg_release.progress.run_pipeline` alone instead — adjust to what
actually works, the assertion targets are the state object and the return code.)
`test_release_confirm.py` / `test_release_context.py`: delete `skip_cd_docs` cases.

- [ ] **Step 4: Run the full suite**

Run: `vrg-container-run -- pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_release.py src/vergil_tooling/lib/release/ tests/vergil_tooling/
vrg-commit --type feat --scope release --message "run vrg-release through the progress pipeline" --body "vrg-release adopts run_pipeline as the reference implementation: stage-aware rendering, always-on .vergil run log, auto-generated --skip-audit. Removes --verbose and --skip-cd-docs (fail_defer reporting supersedes both). Refs #1419"
```

---

### Task 14: Migrate `vrg-validate`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_validate.py`
- Modify: its test file (locate with `vrg-git grep -l "vrg_validate" tests/`)

Behavior changes (intentional, per spec): language checks become `fail_defer` — a lint
failure no longer prevents typecheck/test/audit from running; all failures are reported in
the final summary. `install` stays fail-fast. The check order and `--check` single-mode
semantics are preserved.

- [ ] **Step 1: Rewrite the runner parts of `bin/vrg_validate.py`.** Keep
`_in_dev_container`, `_find_custom_validator`, `_run_common_checks`, `_CHECK_KINDS`,
`_LANGUAGE_CHECK_ORDER` unchanged. Replace `_run_commands`, `_run_custom_validator`,
`_run_single_check`, `_run_all_checks`, and `main` with:

```python
class ValidationFailure(Exception):
    """One or more validation commands failed."""


def _command_stage(label: str, cmds: list[list[str]], *, mode: str) -> Stage:
    def fn(_ctx: object) -> None:
        failed = 0
        for cmd in cmds:
            print(f"Running ({label}): {' '.join(cmd)}")
            rc = progress.run(cmd, check=False)
            if rc != 0:
                failed += 1
                if mode == "fail_fast":
                    break
        if failed:
            msg = f"{failed} of {len(cmds)} {label} command(s) failed"
            raise ValidationFailure(msg)

    return Stage(label, fn, mode=mode)  # type: ignore[arg-type]


def _build_stages(check: str | None, language: str | None, repo_root: Path) -> list[Stage]:
    stages: list[Stage] = []

    if check in (None, "common"):

        def common_fn(_ctx: object) -> None:
            rc = _run_common_checks(repo_root)
            if rc != 0:
                msg = f"common checks exited {rc}"
                raise ValidationFailure(msg)

        stages.append(Stage("common", common_fn, mode="fail_defer"))

    if language is not None and check != "common":
        kinds = [
            kind
            for kind in _LANGUAGE_CHECK_ORDER
            if check is None or check == kind.value
        ]
        kind_cmds = [(kind, language_commands(language, kind)) for kind in kinds]
        kind_cmds = [(kind, cmds) for kind, cmds in kind_cmds if cmds]
        if kind_cmds:
            install_cmds = language_commands(language, CheckKind.INSTALL)
            if install_cmds:
                stages.append(_command_stage("install", install_cmds, mode="fail_fast"))
            stages.extend(
                _command_stage(kind.value, cmds, mode="fail_defer")
                for kind, cmds in kind_cmds
            )

    if check is None:
        custom = _find_custom_validator(repo_root)
        if custom is not None:

            def custom_fn(_ctx: object) -> None:
                print(f"Running: {custom}")
                rc = progress.run((custom,), check=False)
                if rc != 0:
                    msg = f"custom validator exited {rc}"
                    raise ValidationFailure(msg)

            stages.append(Stage("custom", custom_fn, mode="fail_defer"))

    return stages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-validate",
        description="Run validation checks from the command registry.",
    )
    parser.add_argument(
        "--check",
        choices=list(_CHECK_KINDS.keys()),
        default=None,
        help="Run only this check type. Omit to run all.",
    )
    progress.add_progress_args(parser, ())
    args = parser.parse_args(argv)

    if not _in_dev_container():
        print(
            "ERROR: vrg-validate must run inside a dev container.\n"
            "       Run: vrg-container-run -- vrg-validate",
            file=sys.stderr,
        )
        return 1

    venv_bin = Path.cwd() / ".venv" / "bin"
    if venv_bin.is_dir() and str(venv_bin) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"

    repo_root = git.repo_root()

    try:
        vergil_config = config.read_config(repo_root)
        language = vergil_config.project.primary_language
    except FileNotFoundError:
        language = None
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    stages = _build_stages(args.check, language, repo_root)
    if not stages:
        print(f"No {args.check} commands for language '{language or '<not set>'}'")
        return 0

    return progress.run_pipeline(
        None,
        stages,
        command="vrg-validate",
        label="vrg-validate",
        args=args,
        repo_root=repo_root,
    )
```

Imports: add `from vergil_tooling.lib import config, git, progress` (config/git already
there) and `from vergil_tooling.lib.progress import Stage`; drop the now-unused
`subprocess` import if nothing else uses it. If the `# type: ignore[arg-type]` on the
`mode` literal annoys the type checker differently, type the `mode` param as
`Literal["warn", "fail_defer", "fail_fast"]` (import from `progress` as `StageMode`)
instead of `str` and delete the ignore.

Note the single-check semantics change: `--check lint` previously also ran install;
that is preserved (install stage is added whenever language kinds will run). `--check
common` runs only the common stage, as before.

- [ ] **Step 2: Update the vrg-validate tests.** Locate the test file
(`vrg-git grep -l vrg_validate tests/`). Tests that called `_run_commands`/
`_run_all_checks` directly must move to `_build_stages` + `run_pipeline` patterns:

```python
def test_build_stages_full_run_order() -> None:
    with patch(_MOD + ".language_commands") as m_cmds, patch(_MOD + "._find_custom_validator", return_value=None):
        m_cmds.side_effect = lambda lang, kind: [["echo", kind.value]]
        stages = _build_stages(None, "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == [
        "common", "install", "lint", "typecheck", "test", "audit",
    ]
    assert stages[1].mode == "fail_fast"
    assert all(s.mode == "fail_defer" for s in stages if s.name != "install")
```

Adapt the remaining existing tests to the new structure; the `main()` wiring test should
patch `_in_dev_container` to `True`, `progress.run_pipeline` to return 0, and assert it
was called with `command="vrg-validate"`.

- [ ] **Step 3: Run the suite**

Run: `vrg-container-run -- pytest tests/ -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_validate.py tests/vergil_tooling/
vrg-commit --type feat --scope validate --message "run vrg-validate through the progress pipeline" --body "Checks become declarative stages: install is fail_fast, language checks and custom validator are fail_defer so all failures are reported in one run instead of aborting at the first failing check kind. Refs #1419"
```

---

### Task 15: Full validation sweep and live smoke test

**Files:** none new — fixes only.

- [ ] **Step 1: Full validation**

Run: `vrg-container-run -- vrg-validate`
Expected: all checks pass — and as a bonus, you are watching the migrated `vrg-validate`
render its own pipeline (plain renderer inside the container, per the spec's Audience
section). Fix any lint/typecheck/test failures and commit fixes with appropriate scopes.

- [ ] **Step 2: Smoke-test the renderers from the worktree root**

```bash
vrg-container-run -- vrg-validate --check common --output-format plain
vrg-container-run -- vrg-validate --check common --output-format gha
```

Expected: plain shows `→ common  starting...` / `✓ common  …` and the summary block with a
`.vergil/vrg-validate-*.log` pointer; gha shows `::group::common` / `::endgroup::`.
Verify the log file exists and contains the run output:
`ls .vergil/vrg-validate-*.log` then inspect one.

- [ ] **Step 3: Confirm log pruning does not touch foreign files**

Run: `ls .vergil/` — only `vrg-validate-*.log` files were added; nothing else in
`.vergil/` was deleted.

- [ ] **Step 4: Commit any remaining fixes**

```bash
vrg-git add -A
vrg-commit --type chore --scope progress --message "validation fixes for progress framework rollout" --body "Refs #1419"
```

(Skip if the tree is clean.)

---

## Deviations from the spec (intentional, reviewed)

- The summary header is `✗  failures:` rather than `✗  deferred failures:` — the same block
  also lists fail_fast and interrupted stages, so the narrower label would be wrong.
- The summary label is the command name (`vrg-release complete`) rather than
  `release 2.1.2 complete` — the version is not known until the preflight stage has run.
  Enhancement if wanted later: let `run_pipeline` accept a label callback.

## Out of scope (tracked by the spec, not this plan)

- `vrg-finalize-pr` / `vrg-submit-pr` adoption (rollout step 4 — separate issue/PR).
- `vrg-container-run` renderer transparency (explicit non-goal / optional follow-up).
