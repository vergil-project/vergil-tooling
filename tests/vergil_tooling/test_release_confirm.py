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


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


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
                "success",
                "success",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
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
                "success",
                "success",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
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
                "",
            ],
        ),
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
                "failure",
            ],
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
                "success",
                "success",
            ],
        ),
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
                "success",
                "success",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
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
                "success",
            ],
        ),
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
                "failure",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        pytest.raises(ReleaseError, match="did not succeed"),
    ):
        confirm_develop(ctx)


def test_confirm_main_skip_cd_docs_skips_docs_job() -> None:
    ctx = _ctx()
    ctx.skip_cd_docs = True
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
                "success",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
        patch(_MOD + ".watch_workflow") as m_watch,
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
        patch(_MOD + ".git.ref_exists", return_value=True),
    ):
        confirm_main(ctx)

    m_watch.assert_called_once_with(
        "owner/repo",
        "12345",
        verbose=False,
        check_status=False,
    )
    assert ctx.tag == "v2.1.0"


def test_confirm_develop_skip_cd_docs_skips_verify() -> None:
    ctx = _ctx()
    ctx.skip_cd_docs = True
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "67890",
                "https://github.com/o/r/actions/runs/67890",
            ],
        ),
        patch(_MOD + ".watch_workflow") as m_watch,
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_SHA),
    ):
        confirm_develop(ctx)

    m_watch.assert_called_once_with(
        "owner/repo",
        "67890",
        verbose=False,
        check_status=False,
    )
    assert ctx.develop_cd_run_id == "67890"


def test_poll_attempts_constant() -> None:
    assert _CD_POLL_ATTEMPTS == 30
