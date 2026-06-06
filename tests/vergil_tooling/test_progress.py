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
