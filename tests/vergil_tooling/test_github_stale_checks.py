"""Tests for the check waiter's in-wait stale/orphaned check-run probe (#3358).

``wait_for_checks`` used to probe for orphaned check-runs only at its 1800s
deadline, so a check-run GitHub kept reporting ``pending`` after its backing job
had finished held a merge for the full timeout (release v2.1.252, PR #3356).
The waiter now probes while waiting: once a pending check's backing run is
``completed`` and the check has had a grace period to turn terminal, the
backing job's own conclusion decides — ``success`` passes the check (named
loudly); anything else, or an unreadable conclusion, makes it a suspected
orphan. A suspect is re-polled for the orphan grace period, because GitHub can
write results back late during an Actions incident (#3431). Only a suspect
still pending when the grace runs out raises ``OrphanedCheckError``.
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
    monkeypatch.delenv(github.ORPHAN_GRACE_ENV, raising=False)


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
    run_completed: Callable[[str, str], bool],
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

    _stub(monkeypatch, run_completed=lambda repo, rid: clock.now >= run_done_at, job_conclusion=job)
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

    def completed(repo: str, rid: str) -> bool:
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
        run_completed=lambda repo, rid: True,
        job_conclusion=lambda repo, job: pytest.fail("job must not be read within grace"),
    )
    monkeypatch.setattr(github, "pr_checks", fake_checks)
    github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now == 20


# --- wait_for_checks: stale check whose job did not succeed -------------------


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
def test_stale_check_without_successful_job_fails_after_grace(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, conclusion: str | None
) -> None:
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: conclusion)
    with pytest.raises(github.OrphanedCheckError) as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now == github._ORPHAN_GRACE_SECS
    msg = str(excinfo.value)
    assert _MATRIX in msg
    assert (conclusion or "no conclusion") in msg
    assert "no result arrived within the 5m00s grace period" in msg
    assert "still pending 5m00s after its run completed" in msg
    assert "Close and reopen the PR" in msg
    assert _MATRIX_LINK in msg


def test_stale_check_with_unreadable_job_fails_after_grace(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    cause = github.GitHubAPIError(1, ("gh", "api"), stderr="HTTP 404: Not Found")

    def unreadable(repo: str, job_id: str) -> str | None:
        raise cause

    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=unreadable)
    with pytest.raises(github.OrphanedCheckError, match="could not be read") as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert excinfo.value.__cause__ is cause
    assert _MATRIX in str(excinfo.value)
    assert clock.now == github._ORPHAN_GRACE_SECS


# --- wait_for_checks: suspected orphans and the grace period (#3431) ----------


def _late_result(clock: _Clock, at: float) -> Callable[[str], list[dict[str, str]]]:
    """``pr_checks`` stub whose matrix check stays pending until *at*, then passes."""

    def fake(pr: str) -> list[dict[str, str]]:
        if clock.now < at:
            return _checks((_MATRIX, _MATRIX_LINK))(pr)
        passed = {"name": _MATRIX, "bucket": "pass", "state": "SUCCESS", "link": _MATRIX_LINK}
        return [*_checks()(pr), passed]

    return fake


@pytest.mark.parametrize("conclusion", ["failure", None])
def test_late_result_within_grace_is_not_an_orphan(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    capsys: pytest.CaptureFixture[str],
    conclusion: str | None,
) -> None:
    # Replay of the 2026-10-22 Actions incident (vrg-finalize-pr 651): the run
    # completed, the job had no result yet, and GitHub wrote the result back
    # minutes later. The waiter must wait it out rather than declare an orphan.
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: conclusion)
    monkeypatch.setattr(github, "pr_checks", _late_result(clock, 200))
    github.wait_for_checks("651", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now == 200
    err = capsys.readouterr().err
    assert err.count("GitHub may be delayed") == 1  # announced once, not per poll
    assert f"1 check-run(s) have a completed run but no result yet ({_MATRIX})" in err
    assert "re-checking for up to 5m00s" in err


def test_stale_probe_looks_up_the_run_in_the_links_repo(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """The run is probed in the repo its link names, not the cwd's (#3829)."""
    probed: list[tuple[str, str]] = []

    def completed(repo: str, rid: str) -> bool:
        probed.append((repo, rid))
        return True

    _stub(monkeypatch, run_completed=completed, job_conclusion=lambda r, j: "success")
    github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert probed
    assert set(probed) == {("vergil-project/vergil-tooling", "39619708036")}
    del clock


def test_late_required_check_with_successful_job_is_not_an_orphan(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # Replay of release PR #3425: `test / evidence` (required) stayed pending over
    # a successful job until GitHub caught up; it must not fail the release.
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: "success")
    monkeypatch.setattr(github, "pr_checks", _late_result(clock, 240))
    github.wait_for_checks("3425", poll_interval=5, poll_timeout=1800, required={_MATRIX})
    assert clock.now == 240


def test_suspect_job_is_reread_throttled_then_once_more_for_the_verdict(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    reads: list[float] = []

    def job(repo: str, job_id: str) -> str | None:
        reads.append(clock.now)
        return None

    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=job)
    with pytest.raises(github.OrphanedCheckError):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    grace, stale = github._ORPHAN_GRACE_SECS, github._STALE_GRACE_SECS
    step = github._RUN_PROBE_INTERVAL_SECS
    assert reads == [float(t) for t in range(stale, grace, step)] + [float(grace)]


def test_suspect_whose_job_succeeds_during_grace_passes(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    # A non-required check whose job result is itself written back late passes
    # as soon as the job reads success, without waiting out the grace.
    _stub(
        monkeypatch,
        run_completed=lambda repo, rid: True,
        job_conclusion=lambda r, j: "success" if clock.now >= 120 else None,
    )
    github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now == 120
    assert f"{_MATRIX!r} check-run stale; backing job succeeded" in capsys.readouterr().err


def test_suspects_are_reannounced_when_the_set_changes(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    other = ("docs / docs", "https://github.com/o/r/actions/runs/7/job/5")
    _stub(
        monkeypatch,
        run_completed=lambda repo, rid: rid == "39619708036" or clock.now >= 100,
        job_conclusion=lambda r, j: None,
        pending=((_MATRIX, _MATRIX_LINK), other),
    )
    with pytest.raises(github.OrphanedCheckError) as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    err = capsys.readouterr().err
    assert f"1 check-run(s) have a completed run but no result yet ({_MATRIX})" in err
    assert f"2 check-run(s) have a completed run but no result yet ({_MATRIX}, docs / docs)" in err
    # The first suspect's grace expires first; the later one is still waiting.
    assert clock.now == github._ORPHAN_GRACE_SECS
    assert _MATRIX in str(excinfo.value)
    assert "docs / docs" not in str(excinfo.value)


def test_orphan_grace_env_override_shortens_the_wait(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    monkeypatch.setenv(github.ORPHAN_GRACE_ENV, "120")
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: None)
    with pytest.raises(github.OrphanedCheckError, match="within the 2m00s grace period"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert clock.now == 120


def test_bad_orphan_grace_override_fails_before_any_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(github.ORPHAN_GRACE_ENV, "soon")
    monkeypatch.setattr(github, "pr_checks", lambda pr: pytest.fail("no poll on a config error"))
    with pytest.raises(ValueError, match="VRG_ORPHAN_GRACE must be a positive whole number"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())


def test_stale_checks_are_all_named_in_one_error(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    other = ("docs / docs", "https://github.com/o/r/actions/runs/39619708036/job/5")
    _stub(
        monkeypatch,
        run_completed=lambda repo, rid: True,
        job_conclusion=lambda r, j: "failure",
        pending=((_MATRIX, _MATRIX_LINK), other),
    )
    with pytest.raises(github.OrphanedCheckError) as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    assert _MATRIX in str(excinfo.value)
    assert "docs / docs" in str(excinfo.value)


def test_stale_check_with_unparseable_job_link_fails_loud(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # A job link whose repo cannot be parsed names no repo to look the run up
    # in, so it fails loud rather than being probed in some other repo (#3829).
    link = "https://ghe.example/o/r/actions/runs/7/job/8"
    _stub(
        monkeypatch,
        run_completed=lambda repo, rid: pytest.fail("no repo to query"),
        job_conclusion=lambda r, j: pytest.fail("no repo to query"),
        pending=((_MATRIX, link),),
    )
    with pytest.raises(ValueError, match="cannot read the repository"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    del clock


def test_stale_check_with_unrecognised_job_id_fails_after_grace(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # The run's repo is readable but the job id is not: no job to read, an orphan.
    link = "https://github.com/o/r/actions/runs/7/job/not-a-number"
    _stub(
        monkeypatch,
        run_completed=lambda repo, rid: True,
        job_conclusion=lambda r, j: pytest.fail("no job to query"),
        pending=((_MATRIX, link),),
    )
    with pytest.raises(github.OrphanedCheckError, match="unrecognised job link"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())
    del clock


# --- wait_for_checks: genuinely running checks --------------------------------


def test_running_check_waits_until_deadline_with_throttled_probes(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    probes: list[float] = []

    def running(repo: str, rid: str) -> bool:
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
        run_completed=lambda repo, rid: pytest.fail("app status has no run to probe"),
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

    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: "success")
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
        run_completed=lambda repo, rid: clock.now >= 590,
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

    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=unreadable)
    with pytest.raises(github.OrphanedCheckError, match="boom"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=())


# --- stale required checks (#3379) --------------------------------------------


def test_stale_required_check_with_successful_job_fails_after_grace(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    # GitHub keeps blocking the merge on a required check it reports pending, so
    # passing it here would only move the stall into pr_merge's not-ready loop.
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: "success")
    with pytest.raises(github.OrphanedCheckError) as excinfo:
        github.wait_for_checks(
            "1", poll_interval=5, poll_timeout=1800, required=frozenset({_MATRIX})
        )
    assert clock.now == github._ORPHAN_GRACE_SECS
    msg = str(excinfo.value)
    assert _MATRIX in msg
    assert "required status check" in msg
    assert "GitHub will block the merge until it settles" in msg
    assert "Close and reopen the PR" in msg
    assert _MATRIX_LINK in msg
    assert "treated as passed" not in capsys.readouterr().err


def test_stale_non_required_check_passes_with_a_required_set_present(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: "success")
    github.wait_for_checks(
        "1", poll_interval=5, poll_timeout=1800, required=frozenset({"quality / common"})
    )
    assert f"{_MATRIX!r} check-run stale; backing job succeeded" in capsys.readouterr().err


def test_stale_check_with_unknown_required_set_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # pr_merge passes a failed lookup through as None: whether GitHub will block
    # on the stale check is unknown, so it must not be passed.
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: "success")
    monkeypatch.setattr(github, "required_check_names", lambda pr: pytest.fail("no second lookup"))
    with pytest.raises(github.OrphanedCheckError) as excinfo:
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=None)
    msg = str(excinfo.value)
    assert _MATRIX in msg
    assert "required status checks could not be read" in msg
    assert "Close and reopen the PR" in msg


def test_self_resolved_lookup_failure_fails_stale_check_loudly(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # The waiter's own lookup (vrg-wait-until-green) failing is "unknown" too.
    lookups: list[str] = []
    _stub(monkeypatch, run_completed=lambda repo, rid: True, job_conclusion=lambda r, j: "success")
    monkeypatch.setattr(github, "required_check_names", lambda pr: bool(lookups.append(pr)) or None)
    with pytest.raises(github.OrphanedCheckError, match="could not be read"):
        github.wait_for_checks("1", poll_interval=5, poll_timeout=1800)
    assert lookups == ["1"]


def test_unknown_required_set_keeps_the_registered_checks_fallback(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    # With no stale check involved, an unknown set still waits on registered
    # checks only and returns once they are terminal (#3061 fallback).
    def settles(pr: str) -> list[dict[str, str]]:
        return _checks(*(() if clock.now >= 20 else ((_MATRIX, _MATRIX_LINK),)))(pr)

    _stub(
        monkeypatch,
        run_completed=lambda repo, rid: False,
        job_conclusion=lambda r, j: pytest.fail("not stale"),
    )
    monkeypatch.setattr(github, "pr_checks", settles)
    github.wait_for_checks("1", poll_interval=5, poll_timeout=1800, required=None)
    assert clock.now == 20
