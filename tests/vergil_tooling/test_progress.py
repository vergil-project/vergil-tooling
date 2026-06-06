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
