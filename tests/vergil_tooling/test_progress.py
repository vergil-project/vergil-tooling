"""Tests for vergil_tooling.lib.progress."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

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


def test_pipeline_fail_defer_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
        Stage(
            "audit", lambda ctx: calls.append("audit"), mode="fail_fast", skip_flag="skip_audit"
        ),
        Stage("after", lambda ctx: calls.append("after"), mode="fail_defer"),
    ]
    assert _pipeline(tmp_path, stages, _args(skip_audit=True)) == 0
    assert calls == ["after"]  # audit never ran
    assert "audit — skipped via --skip-audit" in capsys.readouterr().out


def _interrupt(ctx: object) -> None:
    raise KeyboardInterrupt


def test_pipeline_interrupt_exits_130(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
