"""Tests for the check waiter's in-wait stale/orphaned check-run probe (#3358).

``wait_for_checks`` used to probe for orphaned check-runs only at its 1800s
deadline, so a check-run GitHub kept reporting ``pending`` after its backing job
had finished held a merge for the full timeout (release v2.1.252, PR #3356).
The waiter now probes while waiting: once a pending check's backing run is
``completed`` and the check has had a grace period to turn terminal, the
backing job's own conclusion decides — ``success`` passes the check (named
loudly); anything else, or an unreadable conclusion, raises
``OrphanedCheckError`` at once.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import github

if TYPE_CHECKING:
    from collections.abc import Callable

_MATRIX_LINK = (
    "https://github.com/vergil-project/vergil-tooling/actions/runs/39619708036/job/119289579201"
)
_MATRIX = "test / matrix"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_gh_env", lambda: None)
    github._warned_stale_links.clear()
    monkeypatch.delenv(github.CHECKS_TIMEOUT_ENV, raising=False)


class _Clock:
    """A fake monotonic clock that ``time.sleep`` advances."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(github.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(github.time, "sleep", fake.sleep)
    return fake


def _checks(*pending: tuple[str, str]) -> Callable[[str], list[dict[str, str]]]:
    """``pr_checks`` stub: a passing check plus *pending* ``(name, link)`` ones, fresh per call."""

    def fake(pr: str) -> list[dict[str, str]]:
        del pr
        checks = [{"name": "quality / common", "bucket": "pass", "state": "SUCCESS", "link": ""}]
        checks += [
            {"name": name, "bucket": "pending", "state": "IN_PROGRESS", "link": link}
            for name, link in pending
        ]
        return checks

    return fake


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_completed: Callable[[str], bool],
    job_conclusion: Callable[[str, str], str | None],
    pending: tuple[tuple[str, str], ...] = ((_MATRIX, _MATRIX_LINK),),
) -> None:
    monkeypatch.setattr(github, "pr_checks", _checks(*pending))
    monkeypatch.setattr(github, "run_completed", run_completed)
    monkeypatch.setattr(github, "job_conclusion", job_conclusion)
    monkeypatch.setattr(
        github,
        "orphaned_check_names",
        lambda pr: pytest.fail("the deadline orphan probe must not run"),
    )


# --- job_conclusion ----------------------------------------------------------


def test_job_conclusion_reads_the_jobs_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_read_json(*args: str) -> dict[str, object]:
        calls.append(args)
        return {"status": "completed", "conclusion": "success"}

    monkeypatch.setattr(github, "read_json", fake_read_json)
    assert github.job_conclusion("o/r", "77") == "success"
    assert calls == [("api", "repos/o/r/actions/jobs/77")]


@pytest.mark.parametrize("payload", [{"conclusion": None}, {}, ["not", "an", "object"]])
def test_job_conclusion_none_when_job_has_no_conclusion(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(github, "read_json", lambda *a: payload)
    assert github.job_conclusion("o/r", "77") is None


# --- wait_for_checks: stale check whose job succeeded --------------------------


def test_replay_pr_3356_stale_matrix_check_passes_within_grace(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    # PR #3356: `test / matrix` ran for 9s and its run concluded success, but the
    # check-run stayed pending. The old waiter printed "Pending checks" for 30
    # minutes and timed out. Now it must return within the grace period.
    run_done_at = 30.0
    jobs_read: list[tuple[str, str]] = []

    def job(repo: str, job_id: str) -> str:
        jobs_read.append((repo, job_id))
        return "success"

    _stub(monkeypatch, run_completed=lambda rid: clock.now >= run_done_at, job_conclusion=job)
    github.wait_for_checks("3356", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now <= run_done_at + github._RUN_PROBE_INTERVAL_SECS + github._STALE_GRACE_SECS
    assert clock.now >= run_done_at + github._STALE_GRACE_SECS
    assert jobs_read == [("vergil-project/vergil-tooling", "119289579201")]
    err = capsys.readouterr().err
    assert f"{_MATRIX!r} check-run stale; backing job succeeded" in err
    assert _MATRIX_LINK in err


def test_stale_passed_check_is_not_rechecked_on_later_polls(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    # Another check is still genuinely running, so the waiter keeps polling; the
    # stale-passed check must stay passed without another jobs read or warning.
    jobs_read: list[str] = []
    running = "test / 3.14"
    running_link = "https://github.com/o/r/actions/runs/2/job/9"

    def completed(rid: str) -> bool:
        return rid == "39619708036" or clock.now >= 300

    def job(repo: str, job_id: str) -> str:
        del repo
        jobs_read.append(job_id)
        return "success"

    _stub(
        monkeypatch,
        run_completed=completed,
        job_conclusion=job,
        pending=((_MATRIX, _MATRIX_LINK), (running, running_link)),
    )
    github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert jobs_read == ["119289579201", "9"]
    assert capsys.readouterr().err.count(f"{_MATRIX!r} check-run stale") == 1


def test_check_that_settles_within_grace_never_reads_the_job(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # The run finishing a moment before its check-run turns terminal is normal
    # API lag, not a stale check: no jobs read, no error.
    def fake_checks(pr: str) -> list[dict[str, str]]:
        settled = clock.now >= 20
        return _checks(*(() if settled else ((_MATRIX, _MATRIX_LINK),)))(pr)

    _stub(
        monkeypatch,
        run_completed=lambda rid: True,
        job_conclusion=lambda repo, job: pytest.fail("job must not be read within grace"),
    )
    monkeypatch.setattr(github, "pr_checks", fake_checks)
    github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now == 20


# --- wait_for_checks: stale check whose job did not succeed -------------------


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
def test_stale_check_without_successful_job_fails_fast(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, conclusion: str | None
) -> None:
    _stub(monkeypatch, run_completed=lambda rid: True, job_conclusion=lambda r, j: conclusion)
    with pytest.raises(github.OrphanedCheckError) as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now <= github._STALE_GRACE_SECS + 5
    msg = str(excinfo.value)
    assert _MATRIX in msg
    assert (conclusion or "no conclusion") in msg
    assert "Close and reopen the PR" in msg
    assert _MATRIX_LINK in msg


def test_stale_check_with_unreadable_job_fails_fast(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    cause = github.GitHubAPIError(1, ("gh", "api"), stderr="HTTP 404: Not Found")

    def unreadable(repo: str, job_id: str) -> str | None:
        raise cause

    _stub(monkeypatch, run_completed=lambda rid: True, job_conclusion=unreadable)
    with pytest.raises(github.OrphanedCheckError, match="could not be read") as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert excinfo.value.__cause__ is cause
    assert _MATRIX in str(excinfo.value)
    assert clock.now <= github._STALE_GRACE_SECS + 5


def test_stale_checks_are_all_named_in_one_error(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    other = ("docs / docs", "https://github.com/o/r/actions/runs/39619708036/job/5")
    _stub(
        monkeypatch,
        run_completed=lambda rid: True,
        job_conclusion=lambda r, j: "failure",
        pending=((_MATRIX, _MATRIX_LINK), other),
    )
    with pytest.raises(github.OrphanedCheckError) as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert _MATRIX in str(excinfo.value)
    assert "docs / docs" in str(excinfo.value)


def test_stale_check_with_unparseable_job_link_fails_fast(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # A job link whose repo cannot be parsed has no readable job: fail fast.
    link = "https://ghe.example/o/r/actions/runs/7/job/8"
    _stub(
        monkeypatch,
        run_completed=lambda rid: True,
        job_conclusion=lambda r, j: pytest.fail("no repo to query"),
        pending=((_MATRIX, link),),
    )
    with pytest.raises(github.OrphanedCheckError, match="could not be read"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())


# --- wait_for_checks: genuinely running checks --------------------------------


def test_running_check_waits_until_deadline_with_throttled_probes(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    probes: list[float] = []

    def running(rid: str) -> bool:
        probes.append(clock.now)
        return False

    _stub(
        monkeypatch,
        run_completed=running,
        job_conclusion=lambda r, j: pytest.fail("a running job is never read"),
    )
    monkeypatch.setattr(github, "orphaned_check_names", lambda pr: [])
    with pytest.raises(github.ChecksTimeoutError, match="still pending after 600s"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=600, required=())
    assert clock.now == 600
    # Probed once per probe interval, not on every 5s poll.
    assert len(probes) == 600 // github._RUN_PROBE_INTERVAL_SECS
    gaps = {b - a for a, b in zip(probes, probes[1:], strict=False)}
    assert gaps == {float(github._RUN_PROBE_INTERVAL_SECS)}


def test_app_posted_pending_check_is_never_probed(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _stub(
        monkeypatch,
        run_completed=lambda rid: pytest.fail("app status has no run to probe"),
        job_conclusion=lambda r, j: pytest.fail("app status has no job"),
        pending=(("CodeQL", "https://github.com/o/r/runs/5"),),
    )
    monkeypatch.setattr(github, "orphaned_check_names", lambda pr: [])
    with pytest.raises(github.ChecksTimeoutError):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=60, required=())


def test_failed_check_still_gates_after_stale_pass(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # The stale probe only re-buckets the stale pending check; a genuinely
    # failing check is left for failed_check_names (pr_merge's gate) to report.
    seen: list[list[str]] = []

    def fake_checks(pr: str) -> list[dict[str, str]]:
        checks = _checks((_MATRIX, _MATRIX_LINK))(pr)
        checks.append({"name": "lint", "bucket": "fail", "state": "FAILURE", "link": ""})
        return checks

    real_terminal = github._checks_terminal

    def spy(checks: list[dict[str, str]], required: object) -> bool:
        seen.append([str(c["bucket"]) for c in checks])
        return real_terminal(checks, ())

    _stub(monkeypatch, run_completed=lambda rid: True, job_conclusion=lambda r, j: "success")
    monkeypatch.setattr(github, "pr_checks", fake_checks)
    monkeypatch.setattr(github, "_checks_terminal", spy)
    github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert seen[-1] == ["pass", "pass", "fail"]


def test_deadline_backstop_still_raises_orphan_inside_grace(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # A run that completes just before the deadline (inside the grace period)
    # is still caught by the existing deadline orphan probe.
    _stub(
        monkeypatch,
        run_completed=lambda rid: clock.now >= 590,
        job_conclusion=lambda r, j: pytest.fail("still inside grace at the deadline"),
    )
    monkeypatch.setattr(github, "orphaned_check_names", lambda pr: [_MATRIX])
    with pytest.raises(github.OrphanedCheckError, match="test / matrix"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=600, required=())


def test_unreadable_job_error_types_include_called_process_error(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    def unreadable(repo: str, job_id: str) -> str | None:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="boom")

    _stub(monkeypatch, run_completed=lambda rid: True, job_conclusion=unreadable)
    with pytest.raises(github.OrphanedCheckError, match="boom"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
