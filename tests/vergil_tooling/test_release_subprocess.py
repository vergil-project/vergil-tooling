"""Tests for vergil_tooling.lib.release.subprocess."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.lib import retry
from vergil_tooling.lib.release import subprocess as release_subprocess
from vergil_tooling.lib.release.subprocess import watch_workflow

_MOD = "vergil_tooling.lib.release.subprocess"


def _cpe(stdout: str = "", stderr: str = "") -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(1, ["gh"], output=stdout, stderr=stderr)
    return exc


class TestStreamWithRetry:
    def test_passes_gh_env(self) -> None:
        with (
            patch(_MOD + ".progress") as m_progress,
            patch(_MOD + "._gh_env", return_value={"GH_TOKEN": "x"}),
        ):
            release_subprocess._stream_with_retry(("gh", "run", "watch"))
        m_progress.run.assert_called_once_with(("gh", "run", "watch"), env={"GH_TOKEN": "x"})

    def test_retries_transient_then_succeeds(self) -> None:
        transient = subprocess.CalledProcessError(1, ("gh",), output="", stderr="HTTP 502")
        with (
            patch(_MOD + ".progress") as m_progress,
            patch(_MOD + "._gh_env", return_value=None),
            patch(_MOD + ".time"),
        ):
            m_progress.run.side_effect = [transient, None]
            release_subprocess._stream_with_retry(("gh", "run", "watch"))
        assert m_progress.run.call_count == 2

    def test_retries_transient_401_then_succeeds(self) -> None:
        """A transient 'HTTP 401: Bad credentials' is retried, not propagated (#1819)."""
        transient = subprocess.CalledProcessError(
            1,
            ("gh",),
            output="",
            stderr="HTTP 401: Bad credentials (https://api.github.com/graphql)",
        )
        with (
            patch(_MOD + ".progress") as m_progress,
            patch(_MOD + "._gh_env", return_value=None),
            patch(_MOD + ".time"),
        ):
            m_progress.run.side_effect = [transient, None]
            release_subprocess._stream_with_retry(("gh", "pr", "checks", "PR", "--watch"))
        assert m_progress.run.call_count == 2

    def test_retries_graphql_something_went_wrong_then_succeeds(self) -> None:
        """GitHub's transient GraphQL failure is retried in the release wait path (#3148)."""
        transient = subprocess.CalledProcessError(
            1,
            ("gh",),
            output="",
            stderr="GraphQL: Something went wrong while executing your query.",
        )
        with (
            patch(_MOD + ".progress") as m_progress,
            patch(_MOD + "._gh_env", return_value=None),
            patch(_MOD + ".time"),
        ):
            m_progress.run.side_effect = [transient, None]
            release_subprocess._stream_with_retry(("gh", "pr", "checks", "PR", "--watch"))
        assert m_progress.run.call_count == 2
        m_progress.emit.assert_called_once()

    def test_propagates_non_transient(self) -> None:
        fatal = subprocess.CalledProcessError(1, ("gh",), output="", stderr="not found")
        with (
            patch(_MOD + ".progress") as m_progress,
            patch(_MOD + "._gh_env", return_value=None),
            pytest.raises(subprocess.CalledProcessError),
        ):
            m_progress.run.side_effect = fatal
            release_subprocess._stream_with_retry(("gh", "run", "watch"))
        assert m_progress.run.call_count == 1

    def test_gives_up_after_max(self) -> None:
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


class TestWatchWorkflow:
    def test_failure_raises_with_output(self) -> None:
        with (
            patch(_MOD + "._stream_with_retry", side_effect=_cpe(stdout="fail", stderr="err")),
            pytest.raises(subprocess.CalledProcessError) as exc_info,
        ):
            watch_workflow("owner/repo", "12345")
        assert exc_info.value.stdout == "fail"
        assert exc_info.value.stderr == "err"

    def test_check_status_false_omits_exit_status(self) -> None:
        with patch(_MOD + "._stream_with_retry") as m:
            watch_workflow("owner/repo", "12345", check_status=False)
        cmd = m.call_args[0][0]
        assert "--exit-status" not in cmd

    def test_check_status_true_includes_exit_status(self) -> None:
        with patch(_MOD + "._stream_with_retry") as m:
            watch_workflow("owner/repo", "12345", check_status=True)
        cmd = m.call_args[0][0]
        assert "--exit-status" in cmd
