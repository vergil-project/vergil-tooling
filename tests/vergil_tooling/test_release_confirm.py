from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.confirm import (
    _CD_POLL_ATTEMPTS,
    _DEVELOP_EXPECTED_JOBS,
    _MAIN_EXPECTED_JOBS,
    confirm_develop,
    confirm_main,
)
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

_MOD = "vergil_tooling.lib.release.confirm"
_SHA = "abc123def456"


def _job(
    name: str, status: str = "completed", conclusion: str | None = "success"
) -> dict[str, str | None]:
    return {"name": name, "status": status, "conclusion": conclusion}


# Reusable-workflow leaf jobs are surfaced as "<caller> / <job>".
_MAIN_JOBS_OK = [_job("docs / docs"), _job("release / release")]
_DEVELOP_JOBS_OK = [_job("docs / docs")]


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_watch_cd_check_status_false_reports_completed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vergil_tooling.lib.release.confirm import _watch_cd

    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=["12345", "https://github.com/o/r/actions/runs/12345"],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
    ):
        run_id, run_url = _watch_cd(ctx, branch="main", check_status=False)
    assert run_id == "12345"
    out = capsys.readouterr().out
    assert "CD workflow completed" in out
    assert "CD workflow succeeded" not in out


def test_main_expected_jobs() -> None:
    assert _MAIN_EXPECTED_JOBS == ("docs", "release")


def test_develop_expected_jobs() -> None:
    assert _DEVELOP_EXPECTED_JOBS == ("docs",)


def test_confirm_main_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
        patch(_MOD + "._fetch_run_jobs", return_value=_MAIN_JOBS_OK),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".git.ref_exists", return_value=True),
    ):
        confirm_main(ctx)

    assert ctx.cd_run_id == "12345"
    assert ctx.cd_run_url == "https://github.com/o/r/actions/runs/12345"
    assert ctx.tag == "v2.1.0"
    assert ctx.develop_tag == "develop-v2.1.0"
    assert ctx.release_url == "https://github.com/o/r/releases/tag/v2.1.0"


def test_confirm_main_polls_until_run_appears() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "",
                "",
                "12345",
                "https://github.com/o/r/actions/runs/12345",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
        patch(_MOD + "._fetch_run_jobs", return_value=_MAIN_JOBS_OK),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".time.sleep"),
    ):
        confirm_main(ctx)

    assert ctx.cd_run_id == "12345"


def test_confirm_main_fails_no_cd_run() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output", return_value=""),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".time.sleep"),
        pytest.raises(ReleaseError, match="No CD workflow run found on main"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_job_not_found() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
            ],
        ),
        # 'docs' never appears — poll exhausts and it is reported missing.
        patch(_MOD + "._fetch_run_jobs", return_value=[_job("release / release")]),
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        pytest.raises(ReleaseError, match="not found in workflow run"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_job_not_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
            ],
        ),
        patch(
            _MOD + "._fetch_run_jobs",
            return_value=[_job("docs / docs", conclusion="failure"), _job("release / release")],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        pytest.raises(ReleaseError, match="did not succeed"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_tag_missing() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
            ],
        ),
        patch(_MOD + "._fetch_run_jobs", return_value=_MAIN_JOBS_OK),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".git.ref_exists", return_value=False),
        pytest.raises(ReleaseError, match="Tag.*does not exist"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_develop_tag_missing() -> None:
    ctx = _ctx()
    ref_exists_calls = iter([True, False])
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
        patch(_MOD + "._fetch_run_jobs", return_value=_MAIN_JOBS_OK),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".git.ref_exists", side_effect=ref_exists_calls),
        pytest.raises(ReleaseError, match="Develop boundary tag"),
    ):
        confirm_main(ctx)


def test_confirm_develop_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "67890",
                "https://github.com/o/r/actions/runs/67890",
            ],
        ),
        patch(_MOD + "._fetch_run_jobs", return_value=_DEVELOP_JOBS_OK),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
    ):
        confirm_develop(ctx)

    assert ctx.develop_cd_run_id == "67890"
    assert ctx.develop_cd_run_url == "https://github.com/o/r/actions/runs/67890"


def test_confirm_develop_fails_no_cd_run() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output", return_value=""),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".time.sleep"),
        pytest.raises(ReleaseError, match="No CD workflow run found on develop"),
    ):
        confirm_develop(ctx)


def test_confirm_develop_fails_job_not_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "67890",
                "https://github.com/o/r/actions/runs/67890",
            ],
        ),
        patch(
            _MOD + "._fetch_run_jobs",
            return_value=[_job("docs / docs", conclusion="failure")],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        pytest.raises(ReleaseError, match="did not succeed"),
    ):
        confirm_develop(ctx)


def test_fetch_run_jobs_parses_jobs_array() -> None:
    from vergil_tooling.lib.release.confirm import _fetch_run_jobs

    ctx = _ctx()
    payload = '{"jobs": [{"name": "docs / docs", "status": "completed", "conclusion": "success"}]}'
    with patch(_MOD + ".github.read_output", return_value=payload):
        jobs = _fetch_run_jobs(ctx, "12345")
    assert jobs == [{"name": "docs / docs", "status": "completed", "conclusion": "success"}]


def test_fetch_run_jobs_empty_output_returns_empty() -> None:
    from vergil_tooling.lib.release.confirm import _fetch_run_jobs

    ctx = _ctx()
    with patch(_MOD + ".github.read_output", return_value=""):
        assert _fetch_run_jobs(ctx, "12345") == []


def test_find_job_substring_matches_reusable_leaf() -> None:
    from vergil_tooling.lib.release.confirm import _find_job

    jobs = [_job("docs / docs"), _job("release / release")]
    matched = _find_job(jobs, "docs")
    assert matched is not None
    assert matched["name"] == "docs / docs"
    assert _find_job(jobs, "missing") is None


def test_settled_run_jobs_polls_until_leaf_conclusion_settles() -> None:
    """Regression #1611: a reusable-workflow leaf whose conclusion lags the
    run-level status is polled until it settles, not read once."""
    from vergil_tooling.lib.release.confirm import _settled_run_jobs

    ctx = _ctx()
    lagging = [
        _job("docs / docs", status="in_progress", conclusion=None),
        _job("release / release"),
    ]
    settled = [_job("docs / docs"), _job("release / release")]
    with (
        patch(_MOD + "._fetch_run_jobs", side_effect=[lagging, settled]),
        patch(_MOD + ".time.sleep") as sleep,
    ):
        jobs = _settled_run_jobs(ctx, "12345", _MAIN_EXPECTED_JOBS)
    assert jobs == settled
    sleep.assert_called_once()


def test_poll_attempts_constant() -> None:
    assert _CD_POLL_ATTEMPTS == 30


def test_confirm_main_prints_run_url_before_watching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _ctx()
    watch_output: list[str] = []

    def fake_watch(*_args: object, **_kwargs: object) -> None:
        watch_output.append(capsys.readouterr().out)

    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
        patch(_MOD + "._fetch_run_jobs", return_value=_MAIN_JOBS_OK),
        patch(_MOD + ".watch_workflow", side_effect=fake_watch),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".git.ref_exists", return_value=True),
    ):
        confirm_main(ctx)

    assert "https://github.com/o/r/actions/runs/12345" in watch_output[0]


def test_confirm_develop_prints_run_url_before_watching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _ctx()
    watch_output: list[str] = []

    def fake_watch(*_args: object, **_kwargs: object) -> None:
        watch_output.append(capsys.readouterr().out)

    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "67890",
                "https://github.com/o/r/actions/runs/67890",
            ],
        ),
        patch(_MOD + "._fetch_run_jobs", return_value=_DEVELOP_JOBS_OK),
        patch(_MOD + ".watch_workflow", side_effect=fake_watch),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
    ):
        confirm_develop(ctx)

    assert "https://github.com/o/r/actions/runs/67890" in watch_output[0]


def test_verify_release_job_success_returns() -> None:
    from vergil_tooling.lib.release.confirm import _verify_release_job

    _verify_release_job([_job("release / release")])  # no raise


def test_verify_release_job_failure_raises() -> None:
    from vergil_tooling.lib.release.confirm import _verify_release_job

    with pytest.raises(ReleaseError, match="did not succeed"):
        _verify_release_job([_job("release / release", conclusion="failure")])


def test_verify_release_job_missing_raises() -> None:
    from vergil_tooling.lib.release.confirm import _verify_release_job

    with pytest.raises(ReleaseError, match="not found"):
        _verify_release_job([_job("docs / docs")])


def test_verify_release_job_is_exact_not_substring() -> None:
    from vergil_tooling.lib.release.confirm import _verify_release_job

    # a "release-notes" job must NOT satisfy the hard gate
    with pytest.raises(ReleaseError, match="not found"):
        _verify_release_job([_job("release-notes / build")])


def test_collect_deferred_publish_collapses_families() -> None:
    from vergil_tooling.lib.release.confirm import _collect_deferred_publish

    jobs = [
        _job("release / release"),
        _job("docker-publish / publish: prod-base:latest", conclusion="failure"),
        _job("docker-publish / publish: prod-python:3.14", conclusion="failure"),
        _job("docs / docs", conclusion="failure"),
    ]
    assert _collect_deferred_publish(jobs) == ["docker-publish", "docs"]


def test_collect_deferred_publish_ignores_success_and_skipped() -> None:
    from vergil_tooling.lib.release.confirm import _collect_deferred_publish

    jobs = [
        _job("release / release"),
        _job("docs / docs"),  # success
        _job("codeql / analyze", conclusion="skipped"),
    ]
    assert _collect_deferred_publish(jobs) == []


def test_release_job_name_constant() -> None:
    from vergil_tooling.lib.release.confirm import _RELEASE_JOB_NAME

    assert _RELEASE_JOB_NAME == "release / release"
