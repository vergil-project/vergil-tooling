"""The abandon step of a stray merge (spec §5.4 and §5.8 of epic vergil-project/.github#1664)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import FakeLockOwner
from vergil_tooling.lib.release import abandon
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import merge_release
from vergil_tooling.lib.release.state import initial_state
from vergil_tooling.lib.release_lock import sweeps
from vergil_tooling.lib.release_lock.model import RefWriteError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MOD = "vergil_tooling.lib.release.abandon"
_GUARD = "vergil_tooling.lib.release.lock_guard"
_SUPERSEDE = "vergil_tooling.lib.release.supersede"
_PR = "https://github.com/owner/repo/pull/12"
_STRAY = "https://github.com/owner/repo/pull/77"
_REARMED = "https://github.com/owner/repo/pull/30"
_HELD = "https://github.com/owner/repo/pull/31"


def _ctx(tmp_path: Path, owner: FakeLockOwner | None) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path,
        version_override=None,
        release_branch="release/2.1.0",
    )
    ctx.issue_number = 42
    ctx.issue_url = "https://github.com/owner/repo/issues/42"
    ctx.release_pr_url = _PR
    ctx.cut = "c" * 40
    ctx.state = initial_state(version="2.1.0", cut="c" * 40, issues=(), without_deps=False)
    ctx.worktree_path = tmp_path / ".worktrees" / "release-2.1.0"
    ctx.lock = owner  # ty: ignore[invalid-assignment]
    return ctx


class _Recorder:
    """Every side effect of the abandon step, in order."""

    def __init__(self, owner: FakeLockOwner) -> None:
        self.events: list[tuple[object, ...]] = owner.events

    def gh(self, *args: str) -> None:
        self.events.append(("gh", *args))

    def remove(self, repo_root: Path, branch: str, **kwargs: str) -> None:
        self.events.append(("remove", branch, kwargs["reason"]))

    def rearm(self, repo: str) -> sweeps.RearmResult:
        self.events.append(("rearm", repo))
        return sweeps.RearmResult(armed=[_REARMED], merged=[], held=[_HELD], refused=[])

    def comment(self, ctx: ReleaseContext, phase: str, details: str) -> None:
        self.events.append(("comment", phase, details))


@pytest.fixture
def owner() -> FakeLockOwner:
    return FakeLockOwner()


@pytest.fixture
def recorder(owner: FakeLockOwner) -> Iterator[_Recorder]:
    rec = _Recorder(owner)
    with (
        patch(_MOD + ".known_pr_state", return_value="OPEN"),
        patch(_MOD + ".github.run", side_effect=rec.gh),
        patch(_MOD + ".remove_release_branch", side_effect=rec.remove),
        patch(_MOD + ".os.chdir"),
        patch(_GUARD + ".sweeps.rearm_all", side_effect=rec.rearm),
        patch(_MOD + ".comment_phase_complete", side_effect=rec.comment),
    ):
        yield rec


def _names(events: list[tuple[object, ...]]) -> list[object]:
    return [event[0] if event[0] != "gh" else f"gh {event[1]} {event[2]}" for event in events]


def test_abandon_undoes_prepare_then_runs_the_full_unlock_path(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    ctx = _ctx(tmp_path, owner)
    abandon.abandon_release(ctx, reason=f"{_STRAY} merged into develop after the cut")
    # The release PR, then the branch and worktree, then the tracking issue,
    # and only then the lock and the re-arm sweep (spec §5.4).
    assert _names(recorder.events) == [
        "gh pr close",
        "remove",
        "gh issue close",
        "release",
        "rearm",
        "comment",
    ]
    pr_close = recorder.events[0]
    assert pr_close[3] == _PR
    issue_close = recorder.events[2]
    assert issue_close[3:7] == ("42", "--repo", "owner/repo", "--reason")
    assert "not planned" in issue_close
    body = str(issue_close[-1])
    assert "abandoned" in body
    assert _STRAY in body
    assert recorder.events[1][1] == "release/2.1.0"
    # The lock is gone and no longer this run's; the worktree is gone too.
    assert ctx.lock is None
    assert ctx.worktree_path is None
    # PRs left unarmed are listed on the tracking issue.
    details = str(recorder.events[-1][2])
    assert _REARMED in details
    assert f"Not re-armed (held): {_HELD}" in details


def test_abandon_skips_a_release_pr_already_closed(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    with patch(_MOD + ".known_pr_state", return_value="CLOSED"):
        abandon.abandon_release(_ctx(tmp_path, owner), reason="stray")
    assert "gh pr close" not in _names(recorder.events)


def test_abandon_refuses_a_merged_release_pr(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    with (
        patch(_MOD + ".known_pr_state", return_value="MERGED"),
        pytest.raises(ReleaseError, match="already merged"),
    ):
        abandon.abandon_release(_ctx(tmp_path, owner), reason="stray")
    # Nothing is undone, and the lock stays held.
    assert recorder.events == []


def test_abandon_failed_close_raises_and_keeps_the_lock(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    failure = subprocess.CalledProcessError(1, ("gh", "pr", "close"), stderr="HTTP 502")
    ctx = _ctx(tmp_path, owner)
    with (
        patch(_MOD + ".github.run", side_effect=failure),
        pytest.raises(ReleaseError, match="could not be closed") as info,
    ):
        abandon.abandon_release(ctx, reason="stray")
    assert "HTTP 502" in (info.value.detail or "")
    assert ("release",) not in owner.events
    assert ctx.lock is owner


def test_abandon_failed_lock_release_names_the_break(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    owner.release_error = RefWriteError("push rejected")
    with pytest.raises(ReleaseError, match="push rejected") as info:
        abandon.abandon_release(_ctx(tmp_path, owner), reason="stray")
    assert "vrg-release-lock break" in str(info.value)
    assert "rearm" not in _names(recorder.events)


def test_abandon_failed_rearm_names_what_is_still_disarmed(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    ctx = _ctx(tmp_path, owner)
    with (
        patch(_GUARD + ".sweeps.rearm_all", side_effect=sweeps.SweepError("gh pr list failed")),
        pytest.raises(ReleaseError, match="gh pr list failed") as info,
    ):
        abandon.abandon_release(ctx, reason="stray")
    assert "still disarmed" in str(info.value)
    # The lock was released before the sweep: it is no longer this run's.
    assert ctx.lock is None


def test_abandon_removal_logged(
    tmp_path: Path, owner: FakeLockOwner, capsys: pytest.CaptureFixture[str]
) -> None:
    """The abandon step's removal is sanctioned, and logged as every removal is (§5.8)."""
    ctx = _ctx(tmp_path, owner)
    assert ctx.worktree_path is not None
    ctx.worktree_path.mkdir(parents=True)
    with (
        patch(_MOD + ".known_pr_state", return_value="OPEN"),
        patch(_MOD + ".github.run"),
        patch(_MOD + ".os.chdir"),
        patch("vergil_tooling.lib.managed_worktree.git.run") as m_worktree_git,
        patch(_SUPERSEDE + ".git.ref_exists", return_value=False),
        patch(_SUPERSEDE + ".git.read_output", return_value=""),
        patch(_GUARD + ".sweeps.rearm_all", return_value=sweeps.RearmResult([], [], [], [])),
        patch(_MOD + ".comment_phase_complete"),
    ):
        abandon.abandon_release(ctx, reason=f"{_STRAY} merged after the cut")
    worktree = tmp_path / ".worktrees" / "release-2.1.0"
    m_worktree_git.assert_called_once_with("worktree", "remove", "--force", str(worktree))
    err = capsys.readouterr().err
    assert (
        f"removed worktree {worktree} (branch release/2.1.0): "
        f"the release was abandoned: {_STRAY} merged after the cut"
    ) in err


def test_abandon_without_a_release_pr_refuses(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    ctx = _ctx(tmp_path, owner)
    ctx.release_pr_url = None
    with pytest.raises(ReleaseError, match="no release PR"):
        abandon.abandon_release(ctx, reason="stray")
    assert recorder.events == []


def test_abandon_failed_tracking_issue_close_keeps_the_lock(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    def gh(*args: str) -> None:
        if args[:2] == ("issue", "close"):
            raise subprocess.CalledProcessError(1, ("gh", "issue", "close"), stderr="HTTP 500")
        recorder.gh(*args)

    ctx = _ctx(tmp_path, owner)
    with (
        patch(_MOD + ".github.run", side_effect=gh),
        pytest.raises(ReleaseError, match="tracking issue #42 could not be closed"),
    ):
        abandon.abandon_release(ctx, reason="stray")
    assert ("release",) not in owner.events
    assert ctx.lock is owner


# --- merge-release abandons on a stray merge (spec §5.4) --------------------------


def test_stray_merge_before_merge_release_abandons(
    tmp_path: Path, owner: FakeLockOwner, recorder: _Recorder
) -> None:
    ctx = _ctx(tmp_path, owner)
    wait = MagicMock()
    with (
        patch("vergil_tooling.lib.release.orchestrator.known_pr_state", return_value="OPEN"),
        patch("vergil_tooling.lib.release.orchestrator.stray_merges", return_value=[_STRAY]),
        patch("vergil_tooling.lib.release.orchestrator.wait_and_merge", wait),
        pytest.raises(abandon.ReleaseAbandoned) as info,
    ):
        merge_release(ctx)
    # The stray PR is named, and the release is never merged into main.
    assert _STRAY in str(info.value)
    assert "fresh `vrg-release`" in str(info.value)
    wait.assert_not_called()
    names = _names(recorder.events)
    assert names[:3] == ["gh pr close", "remove", "gh issue close"]
    assert _STRAY in str(recorder.events[2][-1])
    # The lock is gone, and the disarmed PRs are re-armed.
    assert "release" in names
    assert "rearm" in names
    assert ctx.lock is None


def test_no_stray_merge_goes_on_to_merge(tmp_path: Path, owner: FakeLockOwner) -> None:
    ctx = _ctx(tmp_path, owner)
    with (
        patch("vergil_tooling.lib.release.orchestrator.known_pr_state", return_value="OPEN"),
        patch("vergil_tooling.lib.release.orchestrator.stray_merges", return_value=[]),
        patch("vergil_tooling.lib.release.orchestrator.wait_and_merge") as m_wait,
        patch("vergil_tooling.lib.release.orchestrator.abandon_release") as m_abandon,
    ):
        merge_release(ctx)
    m_wait.assert_called_once()
    m_abandon.assert_not_called()


def test_merged_release_pr_skips_the_stray_check(tmp_path: Path, owner: FakeLockOwner) -> None:
    """A release PR a prior run merged is published: there is nothing left to abandon."""
    ctx = _ctx(tmp_path, owner)
    with (
        patch("vergil_tooling.lib.release.orchestrator.known_pr_state", return_value="MERGED"),
        patch("vergil_tooling.lib.release.orchestrator.stray_merges") as m_stray,
    ):
        merge_release(ctx)
    m_stray.assert_not_called()
