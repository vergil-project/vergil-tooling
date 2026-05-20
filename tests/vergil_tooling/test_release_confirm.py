from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.confirm import confirm_publish

_MOD = "vergil_tooling.lib.release.confirm"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_confirm_publish_succeeds() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",                                             # publish run id
                "https://github.com/o/r/actions/runs/12345",        # publish run url
                "67890",                                             # docs run id
                "https://github.com/o/r/actions/runs/67890",        # docs run url
                "",                                                  # tag check (no error)
                "",                                                  # develop tag check
                "https://github.com/o/r/releases/tag/v2.1.0",       # release url
            ],
        ),
        patch(_MOD + ".github.run"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".config.read_config") as mock_config,
    ):
        mock_config.return_value.publish.docs_workflow = "Documentation"
        confirm_publish(ctx)

    assert ctx.publish_run_id == "12345"
    assert ctx.docs_run_id == "67890"
    assert ctx.tag == "v2.1.0"


def test_confirm_publish_fails_if_tag_missing() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
                "67890",
                "https://github.com/o/r/actions/runs/67890",
            ],
        ),
        patch(_MOD + ".github.run"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".config.read_config") as mock_config,
        pytest.raises(ReleaseError, match="Tag.*does not exist"),
    ):
        mock_config.return_value.publish.docs_workflow = "Documentation"
        confirm_publish(ctx)
