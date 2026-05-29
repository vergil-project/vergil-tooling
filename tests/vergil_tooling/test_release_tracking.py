from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import (
    close_tracking_issue,
    comment_phase_complete,
    comment_phase_failed,
    create_tracking_issue,
    find_existing_tracking_issue,
)

_MOD = "vergil_tooling.lib.release.tracking"


def _ctx() -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )


def test_create_tracking_issue() -> None:
    ctx = _ctx()
    with patch(
        _MOD + ".github.read_output",
        return_value="https://github.com/owner/repo/issues/42",
    ):
        create_tracking_issue(ctx)
    assert ctx.issue_number == 42
    assert ctx.issue_url == "https://github.com/owner/repo/issues/42"


def test_create_tracking_issue_extracts_number_from_url() -> None:
    ctx = _ctx()
    with patch(
        _MOD + ".github.read_output",
        return_value="https://github.com/owner/repo/issues/999",
    ):
        create_tracking_issue(ctx)
    assert ctx.issue_number == 999


def test_find_existing_tracking_issue_returns_url() -> None:
    with patch(
        _MOD + ".github.read_json",
        return_value=[
            {"title": "release: 2.1.0", "url": "https://github.com/owner/repo/issues/10"},
        ],
    ):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result == "https://github.com/owner/repo/issues/10"


def test_find_existing_tracking_issue_returns_none() -> None:
    with patch(_MOD + ".github.read_json", return_value=[]):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result is None


def test_find_existing_tracking_issue_ignores_partial_title_match() -> None:
    with patch(
        _MOD + ".github.read_json",
        return_value=[
            {
                "title": "chore(release): bump version to 2.1.0 for prod image rebuild",
                "url": "https://github.com/owner/repo/issues/277",
            },
        ],
    ):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result is None


def test_find_existing_tracking_issue_skips_non_dict_items() -> None:
    with patch(
        _MOD + ".github.read_json",
        return_value=[
            "not-a-dict",
            {"title": "release: 2.1.0", "url": "https://github.com/owner/repo/issues/10"},
        ],
    ):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result == "https://github.com/owner/repo/issues/10"


def test_find_existing_tracking_issue_returns_none_for_dict_response() -> None:
    with patch(_MOD + ".github.read_json", return_value={}):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result is None


def test_find_existing_tracking_issue_picks_exact_match_among_multiple() -> None:
    with patch(
        _MOD + ".github.read_json",
        return_value=[
            {
                "title": "chore(release): bump version to 2.1.0",
                "url": "https://github.com/owner/repo/issues/277",
            },
            {
                "title": "release: 2.1.0",
                "url": "https://github.com/owner/repo/issues/300",
            },
        ],
    ):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result == "https://github.com/owner/repo/issues/300"


def test_comment_phase_complete() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    ctx.release_pr_url = "https://github.com/owner/repo/pull/100"
    written_bodies: list[str] = []

    original_open = tempfile.NamedTemporaryFile

    def capture_tmpfile(**kwargs: Any) -> Any:
        f = original_open(**kwargs)
        original_write = f.write

        def write_and_capture(data: str) -> int:
            written_bodies.append(data)
            return original_write(data)

        f.write = write_and_capture  # type: ignore[method-assign]
        return f

    with (
        patch(_MOD + ".tempfile.NamedTemporaryFile", side_effect=capture_tmpfile),
        patch(_MOD + ".github.run"),
    ):
        comment_phase_complete(ctx, "prepare", "Branch: release/2.1.0\nPR: https://...")
    assert any("vrg-release:prepare:complete" in b for b in written_bodies)
    assert any("Branch: release/2.1.0" in b for b in written_bodies)


def test_comment_phase_failed() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    exc = ReleaseError(
        phase="merge-release",
        command="gh pr merge ...",
        message="CI failed",
        detail="lint check failed",
    )
    written_bodies: list[str] = []

    original_open = tempfile.NamedTemporaryFile

    def capture_tmpfile(**kwargs: Any) -> Any:
        f = original_open(**kwargs)
        original_write = f.write

        def write_and_capture(data: str) -> int:
            written_bodies.append(data)
            return original_write(data)

        f.write = write_and_capture  # type: ignore[method-assign]
        return f

    with (
        patch(_MOD + ".tempfile.NamedTemporaryFile", side_effect=capture_tmpfile),
        patch(_MOD + ".github.run"),
    ):
        comment_phase_failed(ctx, "merge-release", exc)
    assert any("vrg-release:merge-release:failed" in b for b in written_bodies)
    assert any("CI failed" in b for b in written_bodies)


def test_close_tracking_issue() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    with patch(_MOD + ".github.run") as mock_run:
        close_tracking_issue(ctx, "Summary text here")
    assert mock_run.call_count == 2


def test_create_tracking_issue_bad_url() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output", return_value="not-a-url"),
        pytest.raises(ValueError, match="Could not extract issue number"),
    ):
        create_tracking_issue(ctx)


def test_comment_phase_failed_no_detail() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    exc = ReleaseError(
        phase="merge-release",
        command="gh pr merge ...",
        message="CI failed",
    )
    with (
        patch(_MOD + ".tempfile.NamedTemporaryFile", side_effect=tempfile.NamedTemporaryFile),
        patch(_MOD + ".github.run"),
    ):
        comment_phase_failed(ctx, "merge-release", exc)
