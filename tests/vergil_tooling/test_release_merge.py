"""Tests for vergil_tooling.lib.release.merge (thin wrapper over pr_merge)."""

from __future__ import annotations

import itertools
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_merge import MergeAbortError
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import (
    _is_ancestor,
    known_pr_state,
    sync_local_branch,
    wait_and_merge,
)
from vergil_tooling.lib.retry import KnownResourceNotFoundError

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.release.merge"
_PR = "https://github.com/o/r/pull/5"
_RETRY_SLEEP = "vergil_tooling.lib.retry.time.sleep"
_RETRY_CLOCK = "vergil_tooling.lib.retry.time.monotonic"


def _not_found() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, ["gh"], stderr="GraphQL: Could not resolve to a PullRequest with the number of 5."
    )


def test_delegates_with_merge_strategy() -> None:
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
    ):
        wait_and_merge(_PR, phase="phase-2")
    engine.assert_called_once()
    args, kwargs = engine.call_args
    assert args == (_PR,)
    assert kwargs["strategy"] == "merge"
    # Regression (#3170): no injected streaming waiter — the engine's bounded,
    # stale-check-aware default poller (github.wait_for_checks) does the waiting.
    assert "wait_checks" not in kwargs


def test_wraps_merge_abort_in_release_error() -> None:
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbortError("merge conflicts")),
        pytest.raises(ReleaseError, match="merge conflicts") as excinfo,
    ):
        wait_and_merge(_PR, phase="phase-3")
    assert excinfo.value.phase == "phase-3"


# --- #3137: a PR URL GitHub just returned may 404 briefly ---


def test_known_pr_state_retries_404() -> None:
    with (
        patch(_MOD + ".github.pr_state", side_effect=[_not_found(), "OPEN"]) as state,
        patch(_RETRY_SLEEP),
    ):
        assert known_pr_state(_PR) == "OPEN"
    assert state.call_count == 2


def test_known_pr_state_non_404_is_not_retried() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403: Forbidden")
    with (
        patch(_MOD + ".github.pr_state", side_effect=err) as state,
        patch(_RETRY_SLEEP) as sleep,
        pytest.raises(subprocess.CalledProcessError, match="returned non-zero"),
    ):
        known_pr_state(_PR)
    state.assert_called_once()
    sleep.assert_not_called()


def test_wait_and_merge_waits_for_new_pr_to_be_readable() -> None:
    """A just-created PR 404s briefly; the merge engine starts once it is readable."""
    calls: list[str] = []

    def pr_state(pr: str) -> str:
        calls.append("state")
        if len(calls) == 1:
            raise _not_found()
        return "OPEN"

    def engine(*_args: object, **_kwargs: object) -> None:
        calls.append("engine")

    with (
        patch(_MOD + ".github.pr_state", side_effect=pr_state),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=engine),
        patch(_RETRY_SLEEP),
    ):
        wait_and_merge(_PR, phase="back-merge-bump")
    assert calls == ["state", "state", "engine"]


def test_wait_and_merge_persistent_404_never_reaches_engine() -> None:
    with (
        patch(_MOD + ".github.pr_state", side_effect=_not_found()),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_RETRY_SLEEP),
        patch(_RETRY_CLOCK, side_effect=itertools.count(0.0, 10.0)),
        pytest.raises(KnownResourceNotFoundError, match=f"PR {_PR} was reported by GitHub"),
    ):
        wait_and_merge(_PR, phase="back-merge-bump")
    engine.assert_not_called()


def test_forwards_on_branch_updated_hook() -> None:
    def hook() -> None:
        return None

    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
    ):
        wait_and_merge(_PR, phase="merge-release", on_branch_updated=hook)
    assert engine.call_args.kwargs["on_branch_updated"] is hook


# --- #3175: sync the local release branch to the merged, server-updated head ---

_BRANCH = "release/1.2.3"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """An origin, a release worktree clone on _BRANCH, and a second clone.

    The second clone stands in for GitHub's server-side update-branch: it adds a
    merge commit to _BRANCH on origin that the release clone has never seen.
    """
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    release = tmp_path / "release"
    _git(tmp_path, "clone", "-q", str(origin), str(release))
    _git(release, "checkout", "-q", "-b", "main")
    (release / "base.txt").write_text("base\n")
    _git(release, "add", "base.txt")
    _git(release, "commit", "-qm", "base")
    _git(release, "push", "-q", "origin", "main")
    _git(release, "checkout", "-q", "-b", _BRANCH)
    (release / "notes.txt").write_text("notes\n")
    _git(release, "add", "notes.txt")
    _git(release, "commit", "-qm", "prepare")
    _git(release, "push", "-q", "-u", "origin", _BRANCH)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    monkeypatch.chdir(release)
    return release, other


def _server_update_branch(other: Path) -> str:
    """Advance main, merge it into _BRANCH on origin (as update-branch does)."""
    _git(other, "checkout", "-q", "main")
    (other / "hotfix.txt").write_text("hotfix\n")
    _git(other, "add", "hotfix.txt")
    _git(other, "commit", "-qm", "hotfix")
    _git(other, "push", "-q", "origin", "main")
    _git(other, "checkout", "-q", _BRANCH)
    _git(other, "merge", "-q", "--no-ff", "--no-edit", "main")
    _git(other, "push", "-q", "origin", _BRANCH)
    return _git(other, "rev-parse", "HEAD")


def test_sync_fast_forwards_local_branch_to_updated_head(repos: tuple[Path, Path]) -> None:
    release, other = repos
    new_head = _server_update_branch(other)
    with patch(_MOD + ".github.head_sha", return_value=new_head):
        sync_local_branch(_PR, branch=_BRANCH, worktree=release, phase="merge-release")
    assert _git(release, "rev-parse", _BRANCH) == new_head
    assert _git(release, "rev-parse", "HEAD") == new_head
    assert (release / "hotfix.txt").read_text() == "hotfix\n"


def test_sync_is_a_no_op_when_already_at_head(repos: tuple[Path, Path]) -> None:
    release, _other = repos
    tip = _git(release, "rev-parse", _BRANCH)
    with (
        patch(_MOD + ".github.head_sha", return_value=tip),
        patch(_MOD + ".git.run") as run,
    ):
        sync_local_branch(_PR, branch=_BRANCH, worktree=release, phase="merge-release")
    run.assert_not_called()


def test_sync_refuses_non_descendant_head(repos: tuple[Path, Path]) -> None:
    release, other = repos
    new_head = _server_update_branch(other)
    # A local commit the merged head does not contain.
    (release / "local.txt").write_text("local\n")
    _git(release, "add", "local.txt")
    _git(release, "commit", "-qm", "local only")
    local = _git(release, "rev-parse", _BRANCH)
    with (
        patch(_MOD + ".github.head_sha", return_value=new_head),
        pytest.raises(ReleaseError, match="is not an ancestor of the merged PR head") as excinfo,
    ):
        sync_local_branch(_PR, branch=_BRANCH, worktree=release, phase="merge-release")
    assert excinfo.value.phase == "merge-release"
    assert _git(release, "rev-parse", _BRANCH) == local


def test_sync_refuses_worktree_on_another_branch(repos: tuple[Path, Path]) -> None:
    release, other = repos
    new_head = _server_update_branch(other)
    _git(release, "checkout", "-q", "main")
    with (
        patch(_MOD + ".github.head_sha", return_value=new_head),
        pytest.raises(ReleaseError, match="has main checked out"),
    ):
        sync_local_branch(_PR, branch=_BRANCH, worktree=release, phase="merge-release")


def test_is_ancestor_raises_on_bad_revision(repos: tuple[Path, Path]) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        _is_ancestor("no-such-rev", "HEAD")
