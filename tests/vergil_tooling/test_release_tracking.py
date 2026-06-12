from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release import checklist
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import (
    _MAX_COMMENT_CHARS,
    _truncate_for_comment,
    _write_issue_body,
    close_tracking_issue,
    comment_phase_complete,
    comment_phase_failed,
    create_tracking_issue,
    cursor,
    ensure_checklist,
    find_existing_tracking_issue,
    read_issue_body,
    tick_stage,
)

_MOD = "vergil_tooling.lib.release.tracking"


def _ctx() -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )


def _ctx_with_issue() -> ReleaseContext:
    ctx = _ctx()
    ctx.issue_number = 42
    return ctx


def test_read_issue_body_queries_gh() -> None:
    ctx = _ctx_with_issue()
    with patch(_MOD + ".github.read_output", return_value="body text") as ro:
        assert read_issue_body(ctx) == "body text"
    assert "view" in ro.call_args.args


def test_write_issue_body_edits_via_body_file() -> None:
    ctx = _ctx_with_issue()
    with patch(_MOD + ".github.run") as run:
        _write_issue_body(ctx, "new body")
    assert run.call_args.args[:2] == ("issue", "edit")


def test_ensure_checklist_adds_block_when_absent() -> None:
    ctx = _ctx_with_issue()
    with (
        patch(_MOD + ".read_issue_body", return_value="## Release 2.1.0\n"),
        patch(_MOD + "._write_issue_body") as write,
    ):
        ensure_checklist(ctx, ["audit", "prepare"])
    written = write.call_args.args[1]
    assert checklist.BEGIN in written
    assert "- [ ] audit" in written


def test_ensure_checklist_pre_checks_completed_stages() -> None:
    ctx = _ctx_with_issue()
    with (
        patch(_MOD + ".read_issue_body", return_value="## Release 2.1.0\n"),
        patch(_MOD + "._write_issue_body") as write,
    ):
        ensure_checklist(ctx, ["audit", "prepare"], checked=["audit"])
    written = write.call_args.args[1]
    assert ("audit", True) in checklist.parse(written)
    assert ("prepare", False) in checklist.parse(written)


def test_ensure_checklist_noop_when_present() -> None:
    ctx = _ctx_with_issue()
    body = "## Release\n" + checklist.render(["audit"])
    with (
        patch(_MOD + ".read_issue_body", return_value=body),
        patch(_MOD + "._write_issue_body") as write,
    ):
        ensure_checklist(ctx, ["audit"])
    write.assert_not_called()


def test_tick_stage_checks_box() -> None:
    ctx = _ctx_with_issue()
    body = checklist.render(["audit", "prepare"])
    with (
        patch(_MOD + ".read_issue_body", return_value=body),
        patch(_MOD + "._write_issue_body") as write,
    ):
        tick_stage(ctx, "audit")
    assert ("audit", True) in checklist.parse(write.call_args.args[1])


def test_cursor_returns_first_unchecked() -> None:
    ctx = _ctx_with_issue()
    body = checklist.render(["audit", "prepare"], checked={"audit"})
    with patch(_MOD + ".read_issue_body", return_value=body):
        assert cursor(ctx, ["audit", "prepare"]) == "prepare"


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


def test_truncate_for_comment_passes_through_short_body() -> None:
    body = "<!-- vrg-release:prepare:complete -->\n\nall good"
    assert _truncate_for_comment(body) == body


def test_truncate_for_comment_shrinks_oversized_body_below_limit() -> None:
    head = "<!-- vrg-release:confirm-main:failed -->\nHEADMARKER"
    tail = "REAL-ERROR-AT-THE-END"
    body = head + ("x" * 200_000) + tail

    result = _truncate_for_comment(body)

    assert len(result) <= _MAX_COMMENT_CHARS
    # Head preserved (marker survives for humans and marker-based tooling).
    assert result.startswith(head)
    # Tail preserved (the actual error usually lives at the end of a log).
    assert result.endswith(tail)
    # Middle replaced with a clear truncation marker.
    assert "characters truncated" in result


def test_comment_phase_failed_truncates_oversized_detail() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    exc = ReleaseError(
        phase="confirm-main",
        command="gh run watch ...",
        message="CD failed",
        detail="x" * 200_000,
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
        comment_phase_failed(ctx, "confirm-main", exc)

    assert written_bodies
    body = written_bodies[0]
    assert len(body) <= _MAX_COMMENT_CHARS
    assert "vrg-release:confirm-main:failed" in body
    assert "characters truncated" in body


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
