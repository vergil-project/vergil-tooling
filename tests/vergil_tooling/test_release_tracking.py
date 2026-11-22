from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import defer_publish
from vergil_tooling.lib.pending_release import (
    ChangeKind,
    PendingChange,
    PendingRelease,
    ReleaseTags,
)
from vergil_tooling.lib.release import checklist, guard, state
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import (
    _MAX_COMMENT_CHARS,
    _truncate_for_comment,
    _write_issue_body,
    close_tracking_issue,
    comment_phase_complete,
    comment_phase_failed,
    create_tracking_issue,
    ensure_checklist,
    read_issue_body,
    read_state,
    tick_stage,
    write_state,
)
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError

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


_HUMAN_ONLY = guard.IssueIdentities(author="human", editors=("human",), state_actors=(), traces=())


@pytest.fixture(autouse=True)
def guard_calls() -> Any:
    """The tracking issue's guard, passing by default: locked, and human-only identities."""
    with (
        patch(
            _MOD + ".trusted_authors.load_trusted_authors",
            return_value=TrustedAuthors(human=("human",), agents=("agent",)),
        ),
        patch(_MOD + ".guard.ensure_locked") as locked,
        patch(_MOD + ".guard.read_identities", return_value=_HUMAN_ONLY) as identities,
    ):
        yield {"ensure_locked": locked, "read_identities": identities}


def test_read_issue_body_queries_gh() -> None:
    ctx = _ctx_with_issue()
    with patch(_MOD + ".github.read_output", return_value="body text") as ro:
        assert read_issue_body(ctx) == "body text"
    assert "view" in ro.call_args.args


def test_read_issue_body_retries_404_on_just_created_issue() -> None:
    """The tracking issue number always comes from gh issue create/list (#3137)."""
    ctx = _ctx_with_issue()
    err = subprocess.CalledProcessError(
        1, ["gh"], stderr="GraphQL: Could not resolve to an issue or pull request"
    )
    with (
        patch(_MOD + ".github.read_output", side_effect=[err, "body text"]) as ro,
        patch("vergil_tooling.lib.retry.time.sleep"),
    ):
        assert read_issue_body(ctx) == "body text"
    assert ro.call_count == 2


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


_CUT = "c" * 40


def _pending(*changes: PendingChange) -> PendingRelease:
    return PendingRelease(
        tags=ReleaseTags(latest="v2.0.9", rolling="v2.0", rolling_target="v2.0.9"),
        main_moved_ts=0.0,
        changes=changes,
    )


def _change(number: int | None, title: str, branch: str | None = "feature/1-x") -> PendingChange:
    return PendingChange(
        pr_number=number,
        branch=branch if number is not None else None,
        title=title,
        kind=ChangeKind.OTHER if number is not None else ChangeKind.NO_PR,
        type_label="feat" if number is not None else "-",
        merged_ts=0.0,
        shas=("f" * 40,),
    )


def _creatable_ctx() -> ReleaseContext:
    ctx = _ctx()
    ctx.progress_block = checklist.render(["audit", "prepare"], checked={"audit"})
    ctx.cut = _CUT
    ctx.pending_release = _pending(_change(30, "feat: a thing"))
    return ctx


def _created_body(ctx: ReleaseContext) -> str:
    bodies: list[str] = []

    def fake_read_output(*args: str) -> str:
        bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
        return "https://github.com/owner/repo/issues/42"

    with (
        patch(_MOD + ".github.read_output", side_effect=fake_read_output),
        patch(_MOD + ".github.run"),
    ):
        create_tracking_issue(ctx)
    assert len(bodies) == 1
    return bodies[0]


def test_create_tracking_issue_body_has_contents_and_state() -> None:
    ctx = _creatable_ctx()
    ctx.without_deps = True
    ctx.pending_release = _pending(_change(30, "feat: a thing"), _change(None, "direct push"))
    body = _created_body(ctx)
    assert "## Contents" in body
    assert "#30      feature/1-x  feat  feat: a thing" in body
    assert "PROBLEM  fffffff      -     direct push" in body
    assert "### Issues" in body
    assert "None: the release's PRs close no issues." in body
    # Contents comes before the checklist, the state block last.
    assert body.index("## Contents") < body.index(checklist.BEGIN) < body.index(state.BEGIN)
    parsed = state.parse_state(body)
    assert parsed == state.initial_state(version="2.1.0", cut=_CUT, issues=(), without_deps=True)
    assert ctx.state == parsed


def test_create_tracking_issue_lists_the_issues() -> None:
    ctx = _creatable_ctx()
    ctx.issues = (
        state.IssueEntry(ref="owner/repo#12", prs=(30,)),
        state.IssueEntry(ref="owner/repo#13", prs=(31, 32)),
    )
    body = _created_body(ctx)
    assert "- owner/repo#12 (#30)" in body
    assert "- owner/repo#13 (#31, #32)" in body
    assert state.parse_state(body).issues == ctx.issues


def test_create_tracking_issue_with_nothing_pending() -> None:
    ctx = _creatable_ctx()
    ctx.pending_release = _pending()
    assert "No changes pending release." in _created_body(ctx)


@pytest.mark.parametrize("marker", [checklist.BEGIN, checklist.END, state.BEGIN, state.END, "```"])
def test_create_tracking_issue_neutralises_markers_in_titles_and_branches(marker: str) -> None:
    """PR titles and branches are others' text: none can forge a marker or close the fence."""
    stages = ["audit", "prepare"]
    ctx = _creatable_ctx()
    ctx.pending_release = _pending(_change(30, f"feat: {marker}", branch=f"feature/{marker}"))
    body = _created_body(ctx)
    contents = body[: body.index(checklist.BEGIN)]
    assert "<" not in contents
    assert ">" not in contents
    assert contents.count("```") == 2
    assert body.count(checklist.BEGIN) == 1
    assert body.count(checklist.END) == 1
    assert checklist.parse(checklist.tick(body, "prepare")) == [(name, True) for name in stages]
    assert state.parse_state(body).cut == _CUT


@pytest.mark.parametrize("missing", ["cut", "pending_release"])
def test_create_tracking_issue_refuses_without_contents(missing: str) -> None:
    ctx = _creatable_ctx()
    setattr(ctx, missing, None)
    with (
        patch(_MOD + ".github.read_output") as m_read,
        pytest.raises(ReleaseError, match="No release cut or contents"),
    ):
        create_tracking_issue(ctx)
    m_read.assert_not_called()


def _issue_json(title: str, body: str) -> str:
    return json.dumps({"title": title, "body": body})


def _state_body(version: str = "2.1.0") -> tuple[str, state.StateBlock]:
    block = state.initial_state(version=version, cut=_CUT, issues=(), without_deps=False)
    body = f"## Release\n\n{checklist.render(['audit'])}\n\n{state.render_state(block)}\n"
    return body, block


def test_read_state_returns_the_block() -> None:
    ctx = _ctx_with_issue()
    body, block = _state_body()
    with patch(
        _MOD + ".github.read_output", return_value=_issue_json("release: 2.1.0", body)
    ) as ro:
        assert read_state(ctx) == block
    assert ctx.state == block
    assert ro.call_args.args[:3] == ("issue", "view", "42")


def test_read_state_version_mismatch_raises() -> None:
    ctx = _ctx_with_issue()
    body, _ = _state_body(version="2.0.9")
    with (
        patch(_MOD + ".github.read_output", return_value=_issue_json("release: 2.1.0", body)),
        pytest.raises(ReleaseError, match="names version '2.0.9'") as info,
    ):
        read_state(ctx)
    assert "state block" in str(info.value)
    assert ctx.state is None


def test_read_state_missing_block_raises() -> None:
    ctx = _ctx_with_issue()
    with (
        patch(_MOD + ".github.read_output", return_value=_issue_json("release: 2.1.0", "body")),
        pytest.raises(ReleaseError, match="no release state block"),
    ):
        read_state(ctx)


def test_read_state_read_failure_raises() -> None:
    ctx = _ctx_with_issue()
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
    with (
        patch(_MOD + ".github.read_output", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        read_state(ctx)


def test_read_state_malformed_response_raises() -> None:
    ctx = _ctx_with_issue()
    with (
        patch(_MOD + ".github.read_output", return_value=json.dumps({"title": 1, "body": ""})),
        pytest.raises(TypeError, match="non-string title or body"),
    ):
        read_state(ctx)


def test_read_state_non_object_response_raises() -> None:
    ctx = _ctx_with_issue()
    with (
        patch(_MOD + ".github.read_output", return_value="[]"),
        pytest.raises(TypeError, match="not an object"),
    ):
        read_state(ctx)


def test_write_state_replaces_the_block_and_keeps_the_rest() -> None:
    ctx = _ctx_with_issue()
    body, block = _state_body()
    new = dataclasses.replace(block, done=("prepare",), release_pr="https://x/pull/1")
    with (
        patch(_MOD + ".read_issue_body", return_value=body),
        patch(_MOD + "._write_issue_body") as write,
    ):
        write_state(ctx, new)
    written = write.call_args.args[1]
    assert state.parse_state(written) == new
    assert checklist.parse(written) == checklist.parse(body)
    assert ctx.state == new


def test_write_state_refuses_a_duplicated_block() -> None:
    ctx = _ctx_with_issue()
    body, block = _state_body()
    with (
        patch(_MOD + ".read_issue_body", return_value=body + state.render_state(block)),
        patch(_MOD + "._write_issue_body") as write,
        pytest.raises(ReleaseError, match="exactly once"),
    ):
        write_state(ctx, block)
    write.assert_not_called()


def _gh_calls(body: str) -> tuple[MagicMock, MagicMock]:
    """Patches of the gh read and write calls, the read returning the issue with *body*."""
    read = MagicMock(
        side_effect=lambda *args: (
            _issue_json("release: 2.1.0", body) if "title,body" in args else body
        )
    )
    return read, MagicMock()


def test_read_state_locks_then_checks_the_identities(guard_calls: dict[str, MagicMock]) -> None:
    ctx = _ctx_with_issue()
    body, block = _state_body()
    with patch(_MOD + ".github.read_output", return_value=_issue_json("release: 2.1.0", body)):
        assert read_state(ctx) == block
    guard_calls["ensure_locked"].assert_called_once_with("owner/repo", 42)
    guard_calls["read_identities"].assert_called_once_with("owner/repo", 42)


def test_read_state_stops_before_any_write(guard_calls: dict[str, MagicMock]) -> None:
    ctx = _ctx_with_issue()
    body, _ = _state_body()
    guard_calls["read_identities"].return_value = dataclasses.replace(
        _HUMAN_ONLY, editors=("human", "agent")
    )
    read, run = _gh_calls(body)
    with (
        patch(_MOD + ".github.read_output", read),
        patch(_MOD + ".github.run", run),
        pytest.raises(guard.TrackingIssueBreach) as info,
    ):
        read_state(ctx)
    assert str(info.value).startswith(
        "suspected security breach on tracking issue #42: agent edited the body"
    )
    assert "resume" in str(info.value)
    run.assert_not_called()
    assert all("edit" not in c.args and "comment" not in c.args for c in read.call_args_list)
    assert ctx.state is None


def test_write_state_stops_on_edit_between_read_and_write(
    guard_calls: dict[str, MagicMock],
) -> None:
    ctx = _ctx_with_issue()
    body, block = _state_body()
    read, run = _gh_calls(body)
    with patch(_MOD + ".github.read_output", read), patch(_MOD + ".github.run", run):
        assert read_state(ctx) == block
        # An agent edits the body after the guarded read.
        guard_calls["read_identities"].return_value = dataclasses.replace(
            _HUMAN_ONLY, editors=("human", "agent")
        )
        new = dataclasses.replace(block, done=("prepare",))
        with pytest.raises(guard.TrackingIssueBreach, match="agent edited the body"):
            write_state(ctx, new)
    run.assert_not_called()
    assert ctx.state == block


def test_write_state_runs_the_guard(guard_calls: dict[str, MagicMock]) -> None:
    ctx = _ctx_with_issue()
    body, block = _state_body()
    with (
        patch(_MOD + ".read_issue_body", return_value=body),
        patch(_MOD + "._write_issue_body") as write,
    ):
        write_state(ctx, block)
    guard_calls["ensure_locked"].assert_called_once_with("owner/repo", 42)
    guard_calls["read_identities"].assert_called_once_with("owner/repo", 42)
    write.assert_called_once()


def test_read_state_warns_once_per_run(
    guard_calls: dict[str, MagicMock], capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _ctx_with_issue()
    body, _ = _state_body()
    guard_calls["read_identities"].return_value = dataclasses.replace(
        _HUMAN_ONLY, traces=(("agent", "commented"), ("human", "commented"))
    )
    with patch(_MOD + ".github.read_output", return_value=_issue_json("release: 2.1.0", body)):
        read_state(ctx)
        read_state(ctx)
    assert capsys.readouterr().err == "warning: tracking issue #42: agent commented\n"


@pytest.mark.parametrize(
    "error",
    [TrustedAuthorsNotConfiguredError("not configured"), ValueError("'human' is empty")],
)
def test_read_state_trusted_authors_failure_raises(
    error: Exception, guard_calls: dict[str, MagicMock]
) -> None:
    ctx = _ctx_with_issue()
    body, _ = _state_body()
    with (
        patch(_MOD + ".trusted_authors.load_trusted_authors", side_effect=error),
        patch(_MOD + ".github.read_output", return_value=_issue_json("release: 2.1.0", body)),
        pytest.raises(ReleaseError, match="Cannot check the identities") as info,
    ):
        read_state(ctx)
    assert info.value.command == "vrg-trusted-authors"
    guard_calls["read_identities"].assert_not_called()
    assert ctx.state is None


def test_read_state_identity_read_failure_raises(guard_calls: dict[str, MagicMock]) -> None:
    ctx = _ctx_with_issue()
    body, _ = _state_body()
    guard_calls["read_identities"].side_effect = subprocess.CalledProcessError(
        1, ["gh"], stderr="HTTP 502"
    )
    with (
        patch(_MOD + ".github.read_output", return_value=_issue_json("release: 2.1.0", body)),
        pytest.raises(subprocess.CalledProcessError),
    ):
        read_state(ctx)
    assert ctx.state is None


def test_create_tracking_issue_locks_the_issue() -> None:
    ctx = _creatable_ctx()
    with (
        patch(_MOD + ".github.read_output", return_value="https://github.com/owner/repo/issues/42"),
        patch(_MOD + ".github.run") as run,
    ):
        create_tracking_issue(ctx)
    run.assert_called_once_with("issue", "lock", "42", "--repo", "owner/repo")


def test_create_tracking_issue() -> None:
    ctx = _creatable_ctx()
    with (
        patch(_MOD + ".github.read_output", return_value="https://github.com/owner/repo/issues/42"),
        patch(_MOD + ".github.run"),
    ):
        create_tracking_issue(ctx)
    assert ctx.issue_number == 42
    assert ctx.issue_url == "https://github.com/owner/repo/issues/42"


def test_create_tracking_issue_creates_the_issue_with_its_checklist() -> None:
    # #4367: the checklist is in the creation body, never a later edit, so an
    # interrupt right after `gh issue create` still leaves a resumable issue.
    ctx = _creatable_ctx()
    bodies: list[str] = []

    def fake_read_output(*args: str) -> str:
        bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
        return "https://github.com/owner/repo/issues/42"

    with (
        patch(_MOD + ".github.read_output", side_effect=fake_read_output),
        patch(_MOD + ".github.run"),
    ):
        create_tracking_issue(ctx)
    assert len(bodies) == 1
    assert checklist.parse(bodies[0]) == [("audit", True), ("prepare", False)]
    assert bodies[0].startswith("## Release 2.1.0\n\nRepo: owner/repo\n")


def test_create_tracking_issue_refuses_without_a_checklist() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output") as m_read,
        pytest.raises(ReleaseError, match="No stage checklist"),
    ):
        create_tracking_issue(ctx)
    m_read.assert_not_called()


def test_create_tracking_issue_extracts_number_from_url() -> None:
    ctx = _creatable_ctx()
    with (
        patch(
            _MOD + ".github.read_output", return_value="https://github.com/owner/repo/issues/999"
        ),
        patch(_MOD + ".github.run"),
    ):
        create_tracking_issue(ctx)
    assert ctx.issue_number == 999


def test_comment_phase_complete(monkeypatch: pytest.MonkeyPatch) -> None:
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

        monkeypatch.setattr(f, "write", write_and_capture)
        return f

    with (
        patch(_MOD + ".tempfile.NamedTemporaryFile", side_effect=capture_tmpfile),
        patch(_MOD + ".github.run"),
    ):
        comment_phase_complete(ctx, "prepare", "Branch: release/2.1.0\nPR: https://...")
    assert any("vrg-release:prepare:complete" in b for b in written_bodies)
    assert any("Branch: release/2.1.0" in b for b in written_bodies)


def test_comment_phase_failed(monkeypatch: pytest.MonkeyPatch) -> None:
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

        monkeypatch.setattr(f, "write", write_and_capture)
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
    ctx = _creatable_ctx()
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


def test_comment_phase_failed_truncates_oversized_detail(monkeypatch: pytest.MonkeyPatch) -> None:
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

        monkeypatch.setattr(f, "write", write_and_capture)
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


def test_comment_publish_deferred_names_jobs_and_warns_resume() -> None:
    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    ctx = _ctx()
    ctx.tag = "v2.1.2"
    ctx.cd_run_url = "https://run/55"
    with patch(_MOD + "._comment") as comment:
        comment_publish_deferred(ctx, ["docker-publish", "docs"])
    body = comment.call_args[0][1]
    assert "docker-publish" in body and "docs" in body
    assert "--resume" in body  # explicitly steers away from it
    assert "https://run/55" in body
    assert "`vrg-release --resume` re-runs them" in body
    assert "publish-index" not in body  # no packages advice for CD jobs alone


def _vendor_config(vendor: str | None = "vergil", count: int = 1) -> Any:
    from types import SimpleNamespace

    pkgs = () if vendor is None else tuple(SimpleNamespace(vendor=vendor) for _ in range(count))
    return SimpleNamespace(packages=pkgs)


def test_publish_deferred_remedy_several_packages_share_vendor() -> None:
    # Several [[package]] entries share one vendor, so one packages repo (epic .github#472).
    from vergil_tooling.lib.release.tracking import publish_deferred_remedy

    with patch(_MOD + ".config.read_config", return_value=_vendor_config(count=2)):
        assert "vergil-project/packages" in publish_deferred_remedy(_ctx(), ["package-index"])


def test_comment_publish_deferred_package_index_gives_packages_advice() -> None:
    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    ctx = _ctx()
    ctx.tag = "v2.1.0"
    with (
        patch(_MOD + "._comment") as comment,
        patch(_MOD + ".config.read_config", return_value=_vendor_config()),
    ):
        comment_publish_deferred(ctx, ["package-index"])
    body = comment.call_args[0][1]
    assert "**package-index**" in body
    assert "`publish-index` workflow in `vergil-project/packages`" in body
    assert "`2.1.0` packages appear in the index" in body
    assert "cd.yml" not in body  # CD re-run is the wrong fix for the index
    assert "CD publish jobs" not in body
    assert "vrg-release --resume" in body


def test_comment_publish_deferred_names_the_pending_promote() -> None:
    """With package-index deferred, promote held vX.Y back: the comment says
    vrg-release --resume moves it once the index has the release (#4293), and
    that --resume finishes the release (spec §4.3 of epic vergil-project/.github#1440)."""
    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    ctx = _ctx()
    ctx.tag = "v2.1.0"
    defer_publish(ctx, "package-index")
    with (
        patch(_MOD + "._comment") as comment,
        patch(_MOD + ".config.read_config", return_value=_vendor_config()),
    ):
        comment_publish_deferred(ctx, ["package-index"])
    body = comment.call_args[0][1]
    assert "**Promote pending**: the rolling tag `v2.1` was not moved" in body
    assert "`vrg-release --resume` moves it once the packages appear" in body
    assert "**Then finish the release** with `vrg-release --resume`" in body
    assert "Do not" not in body
    assert "vrg-promote" not in body


def test_comment_publish_deferred_no_pending_promote_with_no_promote() -> None:
    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    ctx = _ctx()
    ctx.promote = False
    defer_publish(ctx, "package-index")
    with (
        patch(_MOD + "._comment") as comment,
        patch(_MOD + ".config.read_config", return_value=_vendor_config()),
    ):
        comment_publish_deferred(ctx, ["package-index"])
    assert "vrg-promote" not in comment.call_args[0][1]


def test_comment_publish_deferred_cd_only_names_no_promote() -> None:
    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    ctx = _ctx()
    defer_publish(ctx, "docker-publish")
    with patch(_MOD + "._comment") as comment:
        comment_publish_deferred(ctx, ["docker-publish"])
    assert "vrg-promote" not in comment.call_args[0][1]


def test_publish_deferred_remedy_ends_with_the_resume() -> None:
    from vergil_tooling.lib.release.tracking import promote_pending, publish_deferred_remedy

    ctx = _ctx()
    defer_publish(ctx, "docs", "package-index")
    assert promote_pending(ctx)
    with patch(_MOD + ".config.read_config", return_value=_vendor_config()):
        remedy = publish_deferred_remedy(ctx, ctx.deferred_publish_failures)
    assert remedy == (
        "fix the cause of the failed CD publish jobs (the resume re-runs them); "
        "re-run (or wait for) the "
        "publish-index workflow in vergil-project/packages and confirm the "
        "release's packages appear in the index, then finish the release with vrg-release --resume"
    )


def test_promote_pending_needs_promote_and_an_index_deferral() -> None:
    from vergil_tooling.lib.release.tracking import promote_pending

    ctx = _ctx()
    assert not promote_pending(ctx)
    defer_publish(ctx, "docs")
    assert not promote_pending(ctx)
    defer_publish(ctx, "package-index")
    assert promote_pending(ctx)
    ctx.promote = False
    assert not promote_pending(ctx)


def test_comment_publish_deferred_mixed_gives_both_advices() -> None:
    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    ctx = _ctx()
    ctx.tag = "v2.1.0"
    with (
        patch(_MOD + "._comment") as comment,
        patch(_MOD + ".config.read_config", return_value=_vendor_config()),
    ):
        comment_publish_deferred(ctx, ["docker-publish", "package-index"])
    body = comment.call_args[0][1]
    assert "CD publish jobs did not succeed: **docker-publish** (the CD run)" in body
    assert "`vrg-release --resume` re-runs them" in body
    assert "`publish-index` workflow in `vergil-project/packages`" in body
    assert "vrg-release --resume" in body


def test_comment_publish_deferred_package_index_without_package_block_raises() -> None:
    from vergil_tooling.lib.release.tracking import comment_publish_deferred

    with (
        patch(_MOD + "._comment") as comment,
        patch(_MOD + ".config.read_config", return_value=_vendor_config(None)),
        pytest.raises(ReleaseError, match=r"no \[package\] block"),
    ):
        comment_publish_deferred(_ctx(), ["package-index"])
    comment.assert_not_called()


_RESUME = ", then finish the release with vrg-release --resume"


@pytest.mark.parametrize(
    ("jobs", "expected"),
    [
        (
            ["docs"],
            "fix the cause of the failed CD publish jobs (the resume re-runs them)" + _RESUME,
        ),
        (
            ["package-index"],
            "re-run (or wait for) the publish-index workflow in vergil-project/packages "
            "and confirm the release's packages appear in the index" + _RESUME,
        ),
        (
            ["docs", "package-index"],
            "fix the cause of the failed CD publish jobs (the resume re-runs them); "
            "re-run (or wait for) the "
            "publish-index workflow in vergil-project/packages and confirm the "
            "release's packages appear in the index" + _RESUME,
        ),
    ],
)
def test_publish_deferred_remedy_per_kind(jobs: list[str], expected: str) -> None:
    from vergil_tooling.lib.release.tracking import publish_deferred_remedy

    with patch(_MOD + ".config.read_config", return_value=_vendor_config()):
        assert publish_deferred_remedy(_ctx(), jobs) == expected
