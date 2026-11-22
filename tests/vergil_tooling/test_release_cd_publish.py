"""The cd-publish stage (spec §4.5 of epic vergil-project/.github#1440)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from vergil_tooling.lib.release.cd_publish import deliver_failed_jobs
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.state import FailedJob, StateBlock, initial_state

_MOD = "vergil_tooling.lib.release.cd_publish"
_CONFIRM = "vergil_tooling.lib.release.confirm"

_DOCS = "docs / docs"
_PYPI = "pypi / publish"
_RELEASE = "release / release"


def _job(name: str, conclusion: str = "success", job_id: int = 0) -> dict[str, Any]:
    return {"name": name, "status": "completed", "conclusion": conclusion, "databaseId": job_id}


def _ctx(*failed: FailedJob, resume: bool = True) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    ctx.resume = resume
    ctx.state = replace(
        initial_state(version="2.1.0", cut="c" * 40, issues=(), without_deps=False),
        main_cd_run="100",
        develop_cd_run="200",
        failed_jobs=failed,
    )
    return ctx


@pytest.fixture(autouse=True)
def write_state() -> Iterator[MagicMock]:
    """Stand in for the tracking-issue write: keep the new state on *ctx*."""

    def fake(ctx: ReleaseContext, new: StateBlock) -> None:
        ctx.state = new

    with patch(_CONFIRM + ".write_state", side_effect=fake) as m_write:
        yield m_write


def _jobs_by_run(runs: dict[str, list[list[dict[str, Any]]]]) -> Any:
    """A ``_settled_run_jobs``/``_fetch_run_jobs`` stand-in: each run's snapshots in turn."""
    calls = {run_id: iter(snapshots) for run_id, snapshots in runs.items()}
    last: dict[str, list[dict[str, Any]]] = {}

    def jobs(_ctx: ReleaseContext, run_id: str, *_args: Any) -> list[dict[str, Any]]:
        last[run_id] = next(calls[run_id], last.get(run_id, []))
        return last[run_id]

    return jobs


def test_no_failed_jobs_is_a_noop(write_state: MagicMock) -> None:
    ctx = _ctx()
    with patch(_MOD + ".rerun_and_watch") as rerun:
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()
    write_state.assert_not_called()


def test_cd_publish_fresh_run_defers(write_state: MagicMock) -> None:
    job = FailedJob("main", "100", _PYPI)
    ctx = _ctx(job, resume=False)
    with (
        patch(_MOD + ".rerun_and_watch") as rerun,
        pytest.raises(ReleaseError, match="did not succeed") as raised,
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()
    write_state.assert_not_called()
    assert raised.value.phase == "cd-publish"
    assert "pypi / publish (CD run 100 on main)" in str(raised.value)
    assert "vrg-release --resume 2.1.0" in str(raised.value)
    assert ctx.state is not None
    assert ctx.state.failed_jobs == (job,)


def test_cd_publish_resume_reruns_main() -> None:
    ctx = _ctx(FailedJob("main", "100", _PYPI))
    before = [_job(_RELEASE), _job(_PYPI, "failure")]
    after = [_job(_RELEASE), _job(_PYPI)]
    jobs = _jobs_by_run({"100": [before, after]})
    with (
        patch(_MOD + "._settled_run_jobs", side_effect=jobs),
        patch(_MOD + ".rerun_and_watch") as rerun,
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_called_once_with(
        ctx, "100", rerun_args=("--failed",), check_status=False, phase="cd-publish"
    )
    assert ctx.state is not None
    assert ctx.state.failed_jobs == ()
    assert ctx.deferred_publish_failures == []


def test_cd_publish_resume_main_job_still_failing_stays_recorded() -> None:
    job = FailedJob("main", "100", _PYPI)
    ctx = _ctx(job)
    failing = [_job(_RELEASE), _job(_PYPI, "failure")]
    with (
        patch(_MOD + "._settled_run_jobs", return_value=failing),
        patch(_MOD + ".rerun_and_watch") as rerun,
        pytest.raises(ReleaseError, match="still not delivered: pypi / publish"),
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_called_once()
    assert ctx.state is not None
    assert ctx.state.failed_jobs == (job,)


def test_cd_publish_main_rerun_that_breaks_the_release_job_fails() -> None:
    ctx = _ctx(FailedJob("main", "100", _PYPI))
    before = [_job(_RELEASE), _job(_PYPI, "failure")]
    after = [_job(_RELEASE, "failure"), _job(_PYPI)]
    with (
        patch(_MOD + "._settled_run_jobs", side_effect=_jobs_by_run({"100": [before, after]})),
        patch(_MOD + ".rerun_and_watch"),
        pytest.raises(ReleaseError, match="Release job did not succeed"),
    ):
        deliver_failed_jobs(ctx)


def test_cd_publish_resume_skips_a_job_rerun_by_hand(write_state: MagicMock) -> None:
    """A run whose recorded jobs have succeeded since is delivered without a re-run."""
    ctx = _ctx(FailedJob("main", "100", _PYPI))
    with (
        patch(_MOD + "._settled_run_jobs", return_value=[_job(_RELEASE), _job(_PYPI)]),
        patch(_MOD + ".rerun_and_watch") as rerun,
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()
    write_state.assert_called_once()
    assert ctx.state is not None
    assert ctx.state.failed_jobs == ()


def _develop_runs(*runs: tuple[int, str, str]) -> str:
    return json.dumps(
        [
            {"databaseId": run_id, "createdAt": created, "status": status}
            for run_id, created, status in runs
        ]
    )


_RECORDED_AT = "2026-11-20T10:00:00Z"


def test_cd_publish_resume_develop_superseded_by_later_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _ctx(FailedJob("develop", "200", _DOCS))
    listing = _develop_runs(
        (300, "2026-11-21T10:00:00Z", "completed"),
        (200, _RECORDED_AT, "completed"),
    )
    with (
        patch(_MOD + "._settled_run_jobs", return_value=[_job(_DOCS, "failure")]),
        patch(_MOD + ".github.read_output", side_effect=[_RECORDED_AT, listing]),
        patch(_MOD + "._fetch_run_jobs", return_value=[_job(_DOCS)]) as fetch,
        patch(_MOD + ".rerun_and_watch") as rerun,
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()
    fetch.assert_called_once_with(ctx, "300")
    assert ctx.state is not None
    assert ctx.state.failed_jobs == ()
    assert "delivered by later develop CD run 300" in capsys.readouterr().out


def test_cd_publish_resume_develop_reruns_only_the_job_no_later_run_delivered() -> None:
    """A later run that delivered docs but not pypi: only pypi is re-run, by itself."""
    ctx = _ctx(FailedJob("develop", "200", _DOCS), FailedJob("develop", "200", _PYPI))
    recorded = [_job(_DOCS, "failure", 1), _job(_PYPI, "failure", 2)]
    rerun_pypi = [_job(_DOCS, "failure", 1), _job(_PYPI, "success", 3)]
    listing = _develop_runs(
        (400, "2026-11-22T10:00:00Z", "in_progress"),
        (300, "2026-11-21T10:00:00Z", "completed"),
        (200, _RECORDED_AT, "completed"),
    )
    runs = {"200": [recorded, recorded, rerun_pypi], "300": [[_job(_DOCS), _job(_PYPI, "failure")]]}
    jobs = _jobs_by_run(runs)
    with (
        patch(_MOD + "._settled_run_jobs", side_effect=jobs),
        patch(_MOD + "._fetch_run_jobs", side_effect=jobs),
        patch(_MOD + ".github.read_output", side_effect=[_RECORDED_AT, listing]),
        patch(_MOD + ".rerun_and_watch") as rerun,
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_called_once_with(
        ctx, "200", rerun_args=("--job", "2"), check_status=False, phase="cd-publish"
    )
    assert ctx.state is not None
    assert ctx.state.failed_jobs == ()


def test_cd_publish_develop_listing_short_of_the_recorded_run_refuses() -> None:
    ctx = _ctx(FailedJob("develop", "200", _DOCS))
    listing = _develop_runs((300, "2026-11-21T10:00:00Z", "completed"))
    with (
        patch(_MOD + "._settled_run_jobs", return_value=[_job(_DOCS, "failure")]),
        patch(_MOD + "._fetch_run_jobs", return_value=[_job(_DOCS, "failure")]),
        patch(_MOD + ".github.read_output", side_effect=[_RECORDED_AT, listing]),
        patch(_MOD + ".rerun_and_watch") as rerun,
        pytest.raises(ReleaseError, match="do not reach back to run 200"),
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()


def test_cd_publish_second_resume_reruns_only_remaining() -> None:
    """A first resume delivers main's job but not develop's; the second re-runs only develop's."""
    main_job = FailedJob("main", "100", _PYPI)
    develop_job = FailedJob("develop", "200", _DOCS)
    ctx = _ctx(main_job, develop_job)
    main_before = [_job(_RELEASE), _job(_PYPI, "failure")]
    main_after = [_job(_RELEASE), _job(_PYPI)]
    docs_failed = [_job(_DOCS, "failure", 7)]
    listing = _develop_runs((200, _RECORDED_AT, "completed"))
    first = _jobs_by_run({"100": [main_before, main_after], "200": [docs_failed]})
    with (
        patch(_MOD + "._settled_run_jobs", side_effect=first),
        patch(_MOD + "._fetch_run_jobs", side_effect=first),
        patch(_MOD + ".github.read_output", side_effect=[_RECORDED_AT, listing]),
        patch(_MOD + ".rerun_and_watch") as rerun,
        pytest.raises(ReleaseError, match=r"still not delivered: docs / docs \(CD run 200"),
    ):
        deliver_failed_jobs(ctx)
    assert [c.args[1] for c in rerun.call_args_list] == ["100", "200"]
    assert ctx.state is not None
    assert ctx.state.failed_jobs == (develop_job,)

    second = _jobs_by_run({"200": [docs_failed, docs_failed, [_job(_DOCS, "success", 8)]]})
    with (
        patch(_MOD + "._settled_run_jobs", side_effect=second),
        patch(_MOD + "._fetch_run_jobs", side_effect=second),
        patch(_MOD + ".github.read_output", side_effect=[_RECORDED_AT, listing]),
        patch(_MOD + ".rerun_and_watch") as rerun,
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_called_once_with(
        ctx, "200", rerun_args=("--job", "7"), check_status=False, phase="cd-publish"
    )
    assert ctx.state.failed_jobs == ()


def test_cd_publish_refuses_a_job_on_an_unrecorded_run() -> None:
    ctx = _ctx(FailedJob("main", "999", _PYPI))
    with (
        patch(_MOD + ".rerun_and_watch") as rerun,
        pytest.raises(ReleaseError, match="state records 100 as the release's main CD run"),
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()


def test_cd_publish_without_a_state_block_refuses() -> None:
    ctx = _ctx()
    ctx.state = None
    with pytest.raises(ReleaseError, match="no release state block"):
        deliver_failed_jobs(ctx)


def test_deferred_publish_failures_derived_from_state() -> None:
    ctx = _ctx(
        FailedJob("main", "100", _PYPI),
        FailedJob("develop", "200", _DOCS),
        FailedJob("main", "100", _DOCS),
    )
    assert ctx.deferred_publish_failures == [_PYPI, _DOCS]
    ctx.index_missed = True
    assert ctx.deferred_publish_failures == [_PYPI, _DOCS, "package-index"]
    assert ctx.state is not None
    ctx.state = replace(ctx.state, failed_jobs=(), index_listed=True)
    assert ctx.deferred_publish_failures == []
    # A view: writing to the list it returns changes nothing.
    ctx.deferred_publish_failures.append("x")
    assert ctx.deferred_publish_failures == []


def test_cd_publish_develop_job_rerun_by_hand_is_delivered_without_a_rerun() -> None:
    ctx = _ctx(FailedJob("develop", "200", _DOCS))
    with (
        patch(_MOD + "._settled_run_jobs", return_value=[_job(_DOCS)]),
        patch(_MOD + ".github.read_output") as read,
        patch(_MOD + ".rerun_and_watch") as rerun,
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()
    read.assert_not_called()
    assert ctx.state is not None
    assert ctx.state.failed_jobs == ()


def test_cd_publish_develop_job_missing_from_the_run_refuses() -> None:
    """A recorded job the run no longer has is not delivered, and cannot be re-run."""
    ctx = _ctx(FailedJob("develop", "200", _DOCS))
    listing = _develop_runs((200, _RECORDED_AT, "completed"))
    with (
        patch(_MOD + "._settled_run_jobs", return_value=[_job(_PYPI)]),
        patch(_MOD + "._fetch_run_jobs", return_value=[_job(_PYPI)]),
        patch(_MOD + ".github.read_output", side_effect=[_RECORDED_AT, listing]),
        patch(_MOD + ".rerun_and_watch") as rerun,
        pytest.raises(ReleaseError, match="is not in CD run 200"),
    ):
        deliver_failed_jobs(ctx)
    rerun.assert_not_called()
