"""Tests for vergil_tooling.lib.release.subprocess."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.subprocess import wait_for_checks, watch_workflow

_MOD = "vergil_tooling.lib.release.subprocess"


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _cpe(stdout: str = "", stderr: str = "") -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(1, ["gh"], output=stdout, stderr=stderr)
    return exc


class TestWaitForChecks:
    def test_verbose_prints_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=True),
            patch(_MOD + "._run_with_retry", return_value=_completed(stdout="all passed\n")),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=True)
        captured = capsys.readouterr()
        assert "all passed" in captured.out

    def test_quiet_suppresses_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=True),
            patch(_MOD + "._run_with_retry", return_value=_completed(stdout="all passed\n")),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=False)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_failure_raises_with_output(self) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=True),
            patch(_MOD + "._run_with_retry", side_effect=_cpe(stdout="fail", stderr="err")),
            pytest.raises(subprocess.CalledProcessError) as exc_info,
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=False)
        assert exc_info.value.stdout == "fail"
        assert exc_info.value.stderr == "err"

    def test_verbose_failure_prints_then_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=True),
            patch(
                _MOD + "._run_with_retry",
                side_effect=_cpe(stdout="check output\n", stderr="error\n"),
            ),
            pytest.raises(subprocess.CalledProcessError),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=True)
        captured = capsys.readouterr()
        assert "check output" in captured.out
        assert "error" in captured.err

    def test_verbose_failure_no_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=True),
            patch(_MOD + "._run_with_retry", side_effect=_cpe(stdout="fail\n", stderr="")),
            pytest.raises(subprocess.CalledProcessError),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=True)
        captured = capsys.readouterr()
        assert "fail" in captured.out
        assert captured.err == ""

    def test_verbose_empty_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=True),
            patch(_MOD + "._run_with_retry", return_value=_completed(stdout="", stderr="warn\n")),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "warn" in captured.err

    def test_verbose_failure_empty_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=True),
            patch(_MOD + "._run_with_retry", side_effect=_cpe(stdout="", stderr="error\n")),
            pytest.raises(subprocess.CalledProcessError),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error" in captured.err

    def test_polls_until_checks_registered(self) -> None:
        registered_calls = iter([False, False, True])
        with (
            patch(_MOD + "._checks_registered", side_effect=registered_calls),
            patch(_MOD + "._run_with_retry", return_value=_completed()),
            patch(_MOD + ".time.sleep"),
            patch(_MOD + ".time.monotonic", side_effect=[0, 1, 2, 3]),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=False)

    def test_polls_timeout_falls_through(self) -> None:
        with (
            patch(_MOD + "._checks_registered", return_value=False),
            patch(_MOD + "._run_with_retry", return_value=_completed()),
            patch(_MOD + ".time.sleep"),
            patch(_MOD + ".time.monotonic", side_effect=[0, 100]),
        ):
            wait_for_checks("https://github.com/o/r/pull/1", verbose=False)


class TestWatchWorkflow:
    def test_verbose_prints_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(_MOD + "._run_with_retry", return_value=_completed(stdout="workflow done\n")):
            watch_workflow("owner/repo", "12345", verbose=True)
        captured = capsys.readouterr()
        assert "workflow done" in captured.out

    def test_quiet_suppresses_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(_MOD + "._run_with_retry", return_value=_completed(stdout="workflow done\n")):
            watch_workflow("owner/repo", "12345", verbose=False)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_failure_raises_with_output(self) -> None:
        with (
            patch(_MOD + "._run_with_retry", side_effect=_cpe(stdout="fail", stderr="err")),
            pytest.raises(subprocess.CalledProcessError) as exc_info,
        ):
            watch_workflow("owner/repo", "12345", verbose=False)
        assert exc_info.value.stdout == "fail"
        assert exc_info.value.stderr == "err"

    def test_verbose_empty_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch(_MOD + "._run_with_retry", return_value=_completed(stdout="", stderr="warn\n")):
            watch_workflow("owner/repo", "12345", verbose=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "warn" in captured.err

    def test_verbose_failure_prints_then_raises(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(
                _MOD + "._run_with_retry",
                side_effect=_cpe(stdout="run log\n", stderr="error\n"),
            ),
            pytest.raises(subprocess.CalledProcessError),
        ):
            watch_workflow("owner/repo", "12345", verbose=True)
        captured = capsys.readouterr()
        assert "run log" in captured.out
        assert "error" in captured.err

    def test_verbose_failure_empty_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch(_MOD + "._run_with_retry", side_effect=_cpe(stdout="", stderr="error\n")),
            pytest.raises(subprocess.CalledProcessError),
        ):
            watch_workflow("owner/repo", "12345", verbose=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error" in captured.err
