"""Tests for vergil_tooling.lib.promote."""

from __future__ import annotations

import subprocess as _sp
from unittest.mock import patch

import pytest

from vergil_tooling.lib.promote import promote


def test_promote_runs_tag_and_push() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        promote("2.0.34")
        assert mock_run.call_count == 2
        tag_call = mock_run.call_args_list[0]
        assert tag_call[0][0] == ["git", "tag", "-f", "v2.0", "v2.0.34"]
        push_call = mock_run.call_args_list[1]
        assert push_call[0][0] == ["git", "push", "origin", "v2.0", "--force"]


def test_promote_dry_run_does_not_execute() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        promote("2.0.34", dry_run=True)
        mock_run.assert_not_called()


def test_promote_strips_v_prefix() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        promote("v2.0.34")
        tag_call = mock_run.call_args_list[0]
        assert tag_call[0][0] == ["git", "tag", "-f", "v2.0", "v2.0.34"]


def test_promote_raises_on_tag_failure() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        err = _sp.CalledProcessError(1, "git tag")
        err.stderr = ""
        err.stdout = ""
        mock_run.side_effect = err
        with pytest.raises(_sp.CalledProcessError):
            promote("2.0.34")


def test_promote_prints_stderr_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = _sp.CalledProcessError(1, "git tag")
    err.stderr = "fatal: tag already exists\n"
    err.stdout = ""
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.side_effect = err
        with pytest.raises(_sp.CalledProcessError):
            promote("2.0.34")
    captured = capsys.readouterr()
    assert "tag already exists" in captured.err


def test_promote_prints_stderr_on_push_failure(capsys: pytest.CaptureFixture[str]) -> None:
    tag_ok = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    push_err = _sp.CalledProcessError(1, "git push")
    push_err.stderr = "fatal: could not read remote\n"
    push_err.stdout = ""
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.side_effect = [tag_ok, push_err]
        with pytest.raises(_sp.CalledProcessError):
            promote("2.0.34")
    captured = capsys.readouterr()
    assert "could not read remote" in captured.err


def test_promote_push_failure_no_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    tag_ok = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    push_err = _sp.CalledProcessError(1, "git push")
    push_err.stderr = ""
    push_err.stdout = ""
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.side_effect = [tag_ok, push_err]
        with pytest.raises(_sp.CalledProcessError):
            promote("2.0.34")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_promote_invalid_version_raises() -> None:
    with pytest.raises(ValueError, match="not valid"):
        promote("invalid")
