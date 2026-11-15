"""Tests for the pip-audit network retry (issue #4423)."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.lib import audit_retry
from vergil_tooling.lib.audit_retry import RETRY_DELAYS, network_error, run_pip_audit

_CMD = ["pip-audit", "--requirement=r.txt", "--format=json", "--output=pip-audit.json"]

# The tail of the traceback the 2.1.310 dependency update hit (PR #4364).
_TIMEOUT_STDERR = (
    "Traceback (most recent call last):\n"
    "urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='pypi.org', "
    "port=443): Read timed out. (read timeout=15)\n"
    "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', "
    "port=443): Read timed out. (read timeout=15)"
)
_FINDING_STDERR = "Found 2 known vulnerabilities in 1 package"


def _fail(stderr: str, rc: int = 1) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(rc, _CMD, output="", stderr=stderr)


class _Runner:
    """A fake ``progress.run``: each call returns or raises the next outcome."""

    def __init__(self, *outcomes: int | subprocess.CalledProcessError) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, cmd: list[str], *, check: bool) -> int:
        self.calls.append((list(cmd), check))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, subprocess.CalledProcessError):
            raise outcome
        return outcome


# -- network_error -------------------------------------------------------------


def test_network_error_names_the_timeout_line() -> None:
    line = network_error(_TIMEOUT_STDERR)
    assert line is not None
    assert line.startswith("urllib3.exceptions.ReadTimeoutError")


@pytest.mark.parametrize(
    "stderr",
    [
        "Could not connect to PyPI's vulnerability feed",
        "PyPI is not redirecting properly",
        "requests.exceptions.ConnectionError: Max retries exceeded with url: /pypi/x/1/json",
        "socket.gaierror: [Errno -3] Temporary failure in name resolution",
        "requests.exceptions.ConnectTimeout: connect timeout=15",
    ],
)
def test_network_error_recognises_network_failures(stderr: str) -> None:
    assert network_error(stderr) is not None


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "ERROR:pip_audit._cli:invalid requirements input: r.txt",
        "ERROR: hash mismatch for package foo",
    ],
)
def test_network_error_ignores_other_failures(stderr: str) -> None:
    assert network_error(stderr) is None


def test_network_error_never_classifies_a_finding_as_transient() -> None:
    assert network_error(_FINDING_STDERR + "\n" + _TIMEOUT_STDERR) is None


# -- run_pip_audit -------------------------------------------------------------


def test_success_first_time_runs_once() -> None:
    runner = _Runner(0)
    sleeps: list[float] = []
    assert run_pip_audit(_CMD, run=runner, sleep=sleeps.append) == 0
    assert runner.calls == [(_CMD, True)]
    assert sleeps == []


def test_success_on_a_retry_after_a_transient_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _Runner(_fail(_TIMEOUT_STDERR), 0)
    sleeps: list[float] = []
    assert run_pip_audit(_CMD, run=runner, sleep=sleeps.append) == 0
    assert len(runner.calls) == 2
    assert sleeps == [RETRY_DELAYS[0]]
    out = capsys.readouterr().out
    assert "attempt 1 of 3 failed on a network error" in out
    assert "ReadTimeoutError" in out


def test_a_vulnerability_finding_is_not_retried() -> None:
    runner = _Runner(_fail(_FINDING_STDERR))
    sleeps: list[float] = []
    assert run_pip_audit(_CMD, run=runner, sleep=sleeps.append) == 1
    assert len(runner.calls) == 1
    assert sleeps == []


def test_a_non_network_failure_is_not_retried() -> None:
    runner = _Runner(_fail("ERROR:pip_audit._cli:invalid requirements input: r.txt", rc=2))
    sleeps: list[float] = []
    assert run_pip_audit(_CMD, run=runner, sleep=sleeps.append) == 2
    assert len(runner.calls) == 1
    assert sleeps == []


def test_exhausting_the_attempts_fails_loudly(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _Runner(_fail(_TIMEOUT_STDERR), _fail(_TIMEOUT_STDERR), _fail(_TIMEOUT_STDERR))
    sleeps: list[float] = []
    assert run_pip_audit(_CMD, run=runner, sleep=sleeps.append) == 1
    assert len(runner.calls) == len(RETRY_DELAYS) + 1
    assert sleeps == list(RETRY_DELAYS)
    out = capsys.readouterr().out
    assert "ERROR: pip-audit failed on a network error on all 3 attempts" in out
    assert "ReadTimeoutError" in out


def test_a_finding_after_a_transient_failure_stops_the_retry() -> None:
    runner = _Runner(_fail(_TIMEOUT_STDERR), _fail(_FINDING_STDERR))
    sleeps: list[float] = []
    assert run_pip_audit(_CMD, run=runner, sleep=sleeps.append) == 1
    assert len(runner.calls) == 2
    assert sleeps == [RETRY_DELAYS[0]]


def test_missing_stderr_is_not_retried() -> None:
    runner = _Runner(subprocess.CalledProcessError(1, _CMD))
    assert run_pip_audit(_CMD, run=runner, sleep=lambda _s: None) == 1
    assert len(runner.calls) == 1


def test_defaults_to_progress_run() -> None:
    with patch.object(audit_retry.progress, "run", return_value=0) as m_run:
        assert run_pip_audit(_CMD, sleep=lambda _s: None) == 0
    m_run.assert_called_once_with(_CMD, check=True)
