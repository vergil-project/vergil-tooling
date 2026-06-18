"""Tests for vergil_tooling.lib.progress."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from rich.spinner import Spinner
from rich.text import Text

from vergil_tooling.lib import progress
from vergil_tooling.lib.progress import PipelineError, Stage, StageResult

if TYPE_CHECKING:
    from pathlib import Path

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


def _auto_renderer(height: int) -> progress.RichRenderer:
    out = io.StringIO()
    return progress.RichRenderer(out, window=None, force_terminal=True, width=80, height=height)


def _rendered_tail_lines(r: progress.RichRenderer) -> list[str]:
    """Extract the dim tail lines from the current renderable (after the spinner)."""
    parts = list(r._renderable().renderables)
    spinner_idx = next(i for i, p in enumerate(parts) if isinstance(p, Spinner))
    return [part.plain for part in parts[spinner_idx + 1 :] if isinstance(part, Text)]


def test_rich_renderer_auto_window_caps_on_tall_terminal() -> None:
    r = _auto_renderer(height=100)
    r.start_stage("build")
    for i in range(progress.AUTO_WINDOW_CAP + 20):
        r.stage_line(f"line {i}")
    tail = _rendered_tail_lines(r)
    assert len(tail) == progress.AUTO_WINDOW_CAP
    # most recent lines, not the oldest
    assert tail[-1].strip() == f"line {progress.AUTO_WINDOW_CAP + 19}"
    r.close()


def test_rich_renderer_auto_window_fits_short_terminal() -> None:
    r = _auto_renderer(height=24)
    for name in ("one", "two", "three"):
        r.end_stage(StageResult(name, "ok", 1.0))
    r.start_stage("build")
    for i in range(50):
        r.stage_line(f"line {i}")
    # height − completed(3) − spinner(1) − margin = visible
    expected = 24 - 3 - 1 - progress.AUTO_WINDOW_MARGIN
    assert len(_rendered_tail_lines(r)) == expected
    r.close()


def test_rich_renderer_auto_window_shrinks_as_stages_complete() -> None:
    r = _auto_renderer(height=24)
    r.start_stage("first")
    for i in range(50):
        r.stage_line(f"line {i}")
    before = len(_rendered_tail_lines(r))
    r.end_stage(StageResult("first", "ok", 1.0))
    r.start_stage("second")
    for i in range(50):
        r.stage_line(f"line {i}")
    after = len(_rendered_tail_lines(r))
    assert after == before - 1
    r.close()


def test_rich_renderer_auto_window_floor_on_degenerate_terminal() -> None:
    r = _auto_renderer(height=4)
    r.start_stage("build")
    for i in range(50):
        r.stage_line(f"line {i}")
    assert len(_rendered_tail_lines(r)) == progress.AUTO_WINDOW_FLOOR
    r.close()


def test_rich_renderer_auto_buffers_only_up_to_cap() -> None:
    r = _auto_renderer(height=100)
    assert r._tail.maxlen == progress.AUTO_WINDOW_CAP
    r.close()


def _physical_height(r: progress.RichRenderer) -> int:
    """Rows the current renderable actually occupies, after wrapping."""
    lines = r._console.render_lines(r._renderable(), r._console.options, pad=False)
    return len(lines)


def test_rich_renderer_long_lines_do_not_overflow_viewport() -> None:
    """Regression (#1517): streamed lines longer than the console width — e.g.
    CI-check rows ending in ~95-char job URLs — must not wrap to multiple
    physical rows.

    The auto-window budgets *logical* lines, but Rich's LiveRender measures
    *physical* rows. Wrapped lines push the live block past the viewport, Rich
    ellipsis-crops it to the full viewport height, and a full-viewport block
    scrolls the terminal on every repaint — leaking duplicated top rows (the
    repeated ``✓ audit`` lines seen in vrg-release)."""
    height = 30
    out = io.StringIO()
    r = progress.RichRenderer(out, window=None, force_terminal=True, width=100, height=height)
    for name in ("audit", "preflight", "prepare", "merge-release", "confirm-main"):
        r.end_stage(StageResult(name, "ok", 3.3))
    r.start_stage("back-merge-bump")
    long = (
        "quality / typecheck / 3.14\tpass\t25s\t"
        "https://github.com/vergil-project/vergil-tooling/actions/runs/27133407446/job/80079745066"
    )
    for _ in range(200):  # saturate the tail with would-be-wrapping lines
        r.stage_line(long)
    # Each line occupies exactly one row, so the block stays within the viewport
    # and Rich never has to crop/scroll it.
    expected_rows = len(r._completed) + 1 + min(r._visible_window(), len(r._tail))
    assert _physical_height(r) == expected_rows
    assert _physical_height(r) <= height
    r.close()


def test_rich_renderer_completed_status_line_does_not_wrap() -> None:
    """A completed-stage line with a long error must occupy one physical row,
    not wrap and inflate the block height (#1517)."""
    out = io.StringIO()
    r = progress.RichRenderer(out, window=None, force_terminal=True, width=60, height=24)
    r.end_stage(StageResult("build-images", "failed", 4.0, "x" * 200))
    r.start_stage("next")
    assert _physical_height(r) == len(r._completed) + 1  # completed row + spinner
    r.close()


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


def test_run_stdin_devnull_gives_child_eof() -> None:
    """Issue #1470: ``stdin=subprocess.DEVNULL`` reaches the child, which
    sees immediate EOF instead of inheriting (and potentially blocking on)
    the parent's stdin."""
    progress._session = None
    code = "import sys; sys.exit(0 if sys.stdin.read() == '' else 1)"
    rc = progress.run((sys.executable, "-c", code), check=False, stdin=subprocess.DEVNULL)
    assert rc == 0


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


def test_pipeline_stage_print_is_captured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_pipeline_warn_does_not_affect_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def _exit_one(ctx: object) -> None:
    raise SystemExit(1)


def test_pipeline_systemexit_is_stage_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stage calling sys.exit() is a recorded failure, not a silent process exit."""
    calls: list[str] = []
    stages = [
        Stage("bad", _exit_one, mode="fail_defer"),
        Stage("after", lambda ctx: calls.append("after"), mode="fail_defer"),
    ]
    assert _pipeline(tmp_path, stages, _args()) == 1
    assert calls == ["after"]
    out = capsys.readouterr().out
    assert "bad — SystemExit: 1" in out


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


class _DetailedError(Exception):
    """Exception carrying a verbose ``detail`` attribute, like ReleaseError."""

    def __init__(self) -> None:
        self.detail = "actions.permissions: expected='selected', actual='all'"
        super().__init__("Repository configuration is non-compliant.")


def _boom_with_detail(ctx: object) -> None:
    raise _DetailedError


def test_pipeline_writes_exception_detail_to_log(tmp_path: Path) -> None:
    """A failing stage's exception ``detail`` is recorded in the full log so
    the real reason survives where the summary's 'full log →' pointer sends
    the user (issue #1691)."""
    stages = [Stage("audit", _boom_with_detail, mode="fail_fast")]
    _pipeline(tmp_path, stages, _args())
    log_text = next((tmp_path / ".vergil").glob("test-cmd-*.log")).read_text()
    assert "actions.permissions: expected='selected', actual='all'" in log_text


def test_pipeline_detail_kept_out_of_one_line_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The verbose detail goes to the log, not the one-line failure summary."""
    stages = [Stage("audit", _boom_with_detail, mode="fail_fast")]
    _pipeline(tmp_path, stages, _args())
    out = capsys.readouterr().out
    assert "audit — _DetailedError: Repository configuration is non-compliant." in out
    assert "expected='selected'" not in out


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
    assert args.output_window is None  # auto-size to terminal height


def test_add_progress_args_rejects_skip_on_non_fail_fast() -> None:
    parser = argparse.ArgumentParser()
    stages = [Stage("docs", lambda ctx: None, mode="fail_defer", skip_flag="skip_docs")]
    with pytest.raises(ValueError, match="skip_flag is only supported on fail_fast stages"):
        progress.add_progress_args(parser, stages)


def test_gha_renderer_ok_stage_no_error_annotation() -> None:
    out = io.StringIO()
    r = progress.GhaRenderer(out)
    r.start_stage("lint")
    r.end_stage(StageResult("lint", "ok", 1.0))
    r.close()
    text = out.getvalue()
    assert "::endgroup::" in text
    assert "::error" not in text


def test_rich_renderer_close_without_summary_stops_live() -> None:
    out = io.StringIO()
    r = progress.RichRenderer(out, window=2, force_terminal=True)
    r.start_stage("build")
    r.close()  # live still started — close() must stop it
    assert r._live.is_started is False


def test_make_renderer_selects_by_format() -> None:
    out = io.StringIO()
    rich_renderer = progress._make_renderer("rich", out, 5)
    assert isinstance(rich_renderer, progress.RichRenderer)
    rich_renderer.close()
    auto_renderer = progress._make_renderer("rich", out, None)
    assert isinstance(auto_renderer, progress.RichRenderer)
    auto_renderer.close()
    assert isinstance(progress._make_renderer("gha", out, 5), progress.GhaRenderer)
    plain = progress._make_renderer("plain", out, 5)
    assert isinstance(plain, progress.PlainRenderer)
    assert not isinstance(plain, progress.GhaRenderer)


def test_run_keyboard_interrupt_terminates_child() -> None:
    progress._session = None
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO("")
    fake_proc.stderr = io.StringIO("")
    fake_proc.wait.side_effect = [KeyboardInterrupt, 130]
    with (
        patch(_MOD + ".subprocess.Popen", return_value=fake_proc),
        pytest.raises(KeyboardInterrupt),
    ):
        progress.run(("sleep", "60"))
    fake_proc.terminate.assert_called_once_with()
    assert fake_proc.wait.call_count == 2
