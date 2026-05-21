from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.confirm import confirm_publish
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

_MOD = "vergil_tooling.lib.release.confirm"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_confirm_publish_release_and_docs() -> None:
    """Both release and docs enabled — watches CD, verifies all artifacts."""
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",  # CD run id
                "https://github.com/o/r/actions/runs/12345",  # CD run url
                "https://github.com/o/r/releases/tag/v2.1.0",  # release url
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".config.read_config") as mock_config,
    ):
        mock_config.return_value.publish.release = True
        mock_config.return_value.publish.docs = True
        confirm_publish(ctx)

    assert ctx.cd_run_id == "12345"
    assert ctx.cd_run_url == "https://github.com/o/r/actions/runs/12345"
    assert ctx.tag == "v2.1.0"
    assert ctx.develop_tag == "develop-v2.1.0"
    assert ctx.release_url == "https://github.com/o/r/releases/tag/v2.1.0"


def test_confirm_publish_docs_only() -> None:
    """release=false, docs=true — watches CD, verifies develop tag only."""
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",  # CD run id
                "https://github.com/o/r/actions/runs/12345",  # CD run url
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".config.read_config") as mock_config,
    ):
        mock_config.return_value.publish.release = False
        mock_config.return_value.publish.docs = True
        confirm_publish(ctx)

    assert ctx.cd_run_id == "12345"
    assert ctx.develop_tag == "develop-v2.1.0"
    assert ctx.tag is None
    assert ctx.release_url is None


def test_confirm_publish_skips_cd_when_nothing_published() -> None:
    """release=false, docs=false — skips CD, still verifies develop tag."""
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output") as mock_gh,
        patch(_MOD + ".watch_workflow") as mock_watch,
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".config.read_config") as mock_config,
    ):
        mock_config.return_value.publish.release = False
        mock_config.return_value.publish.docs = False
        confirm_publish(ctx)

    mock_gh.assert_not_called()
    mock_watch.assert_not_called()
    assert ctx.cd_run_id is None
    assert ctx.develop_tag == "develop-v2.1.0"


def test_confirm_publish_fails_if_no_cd_run() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output", return_value=""),
        patch(_MOD + ".config.read_config") as mock_config,
        pytest.raises(ReleaseError, match="No CD workflow run found"),
    ):
        mock_config.return_value.publish.release = True
        mock_config.return_value.publish.docs = True
        confirm_publish(ctx)


def test_confirm_publish_fails_if_tag_missing() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".config.read_config") as mock_config,
        pytest.raises(ReleaseError, match="Tag.*does not exist"),
    ):
        mock_config.return_value.publish.release = True
        mock_config.return_value.publish.docs = True
        confirm_publish(ctx)


def test_confirm_publish_fails_if_develop_tag_missing() -> None:
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
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", side_effect=ref_exists_calls),
        patch(_MOD + ".config.read_config") as mock_config,
        pytest.raises(ReleaseError, match="Develop boundary tag"),
    ):
        mock_config.return_value.publish.release = True
        mock_config.return_value.publish.docs = True
        confirm_publish(ctx)
