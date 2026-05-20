from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.bump import merge_bump

_MOD = "vergil_tooling.lib.release.bump"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_merge_bump_finds_and_merges_pr() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._verify_issue_linkage"),
        patch(_MOD + ".wait_and_merge"),
    ):
        merge_bump(ctx)
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    assert ctx.next_version == "2.1.1"


def test_merge_bump_times_out() -> None:
    ctx = _ctx()
    fake_time = iter([0.0, 301.0])
    with (
        patch(_MOD + ".github.read_output", return_value=""),
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".time.monotonic", side_effect=fake_time),
        pytest.raises(ReleaseError, match="did not appear"),
    ):
        merge_bump(ctx)


def test_merge_bump_fails_on_missing_linkage() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "https://github.com/owner/repo/pull/101",
                "No linkage body",
            ],
        ),
        pytest.raises(ReleaseError, match="linkage"),
    ):
        merge_bump(ctx)
