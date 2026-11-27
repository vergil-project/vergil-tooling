"""Tests for vergil_tooling.bin.vrg_repo_status."""

from __future__ import annotations

import contextlib
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_repo_status import (
    _TITLE_WIDTH,
    _format_age,
    _pending_rows,
    _pending_summary,
    _release_line,
    _row,
    main,
)
from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.finalize_watch import FinalizeWatchError
from vergil_tooling.lib.open_prs import OpenPR, OpenPRKind, OpenPRsError
from vergil_tooling.lib.pending_release import (
    ChangeKind,
    PendingChange,
    PendingRelease,
    PendingReleaseError,
    ReleaseTags,
)
from vergil_tooling.lib.pr_await import AwaitResult
from vergil_tooling.lib.release_lock.model import LockRecord, RefReadError
from vergil_tooling.lib.release_lock.refs import LockState
from vergil_tooling.lib.release_lock.stale import Staleness
from vergil_tooling.lib.release_lock.status import HeldPR, LockStatus
from vergil_tooling.lib.worktrees import (
    MainCheckoutStatus,
    RawWorktree,
    RepoWorktrees,
    Worktree,
    WorktreeState,
    WorktreeStatus,
)

_MOD = "vergil_tooling.bin.vrg_repo_status"
_ROOT = Path("/repo")


def _status(
    branch: str,
    state: WorktreeState,
    *,
    pr: int | None = None,
    ahead: int = 0,
    dirty: bool = False,
    detail: str | None = None,
    needs_attention: bool = False,
    workflow_status: str | None = None,
    workflow_error: str | None = None,
    pr_prepared: bool = False,
    last_commit_ts: float | None = None,
    last_modified_ts: float | None = None,
    path: Path | None = None,
) -> WorktreeStatus:
    wt_path = path or _ROOT / ".worktrees" / branch.replace("/", "-")
    wt = Worktree(path=wt_path, branch=branch)
    return WorktreeStatus(
        worktree=wt,
        state=state,
        pr_number=pr,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
        needs_attention=needs_attention,
        workflow_status=workflow_status,
        workflow_error=workflow_error,
        pr_prepared=pr_prepared,
        last_commit_ts=last_commit_ts,
        last_modified_ts=last_modified_ts,
    )


def _main_status(**overrides: Any) -> MainCheckoutStatus:
    base = MainCheckoutStatus(
        path=_ROOT,
        branch="develop",
        integration="develop",
        dirty=False,
        rebasing=False,
        notes=(),
        last_commit_ts=None,
    )
    return replace(base, **overrides)


# Column index of WORKFLOW in a rendered row (between STATE and AHEAD).
_BRANCH_COL = 1
_WORKFLOW_COL = 4
_LAST_COMMIT_COL = 7
_LAST_MODIFIED_COL = 8

_NOW = 1_700_000_000.0
_HOUR = 3600.0

# A branching model no real config uses; _run maps it to any branch.
_STUB_MODEL = "stub-model"


_LOCK_REPO = "vergil-project/vergil-tooling"


def _no_lock() -> LockStatus:
    return LockStatus(
        repo=_LOCK_REPO,
        state=LockState(sha=None, record=None, error=""),
        staleness=Staleness(
            stale=False, live=False, reason="no release lock held", tracking_issue=""
        ),
        heartbeat_age_secs=None,
        held_prs=(),
    )


def _raws(statuses: list[WorktreeStatus]) -> list[RawWorktree]:
    """Mirror a list of branched statuses as the RawWorktree records
    discovery would return for them."""
    return [RawWorktree(path=s.worktree.path, branch=s.worktree.branch) for s in statuses]


def _run(
    linked: list[RawWorktree],
    *,
    gather: Any = (),
    detached: Any = (),
    main_status: MainCheckoutStatus | None = None,
    integration: str | Exception | None = "develop",
    publish: bool = False,
    pending: PendingRelease | Exception | None = None,
    open_prs: tuple[OpenPR, ...] | Exception = (),
    gone: set[str] | Exception | None = None,
    still_present: bool | None = None,
    merge: Any = None,
    lock: LockStatus | Exception | None = None,
    lock_repo: str | None = "vergil-project/vergil-tooling",
    argv: list[str] | None = None,
) -> tuple[int, dict[str, MagicMock]]:
    """Run main() against a mocked repo: main checkout at /repo plus *linked*.

    ``gather`` / ``detached`` are ``side_effect`` values (a list of results or
    an exception); ``integration`` / ``gone`` / ``pending`` / ``open_prs`` are raised when
    they are exceptions and returned otherwise. ``merge`` is what each open
    PR's merge-state read (``pr_await.standing_block``) gives: ``None`` (can
    still merge, the default), an ``AwaitResult``, or an exception it raises.
    ``lock`` is what the release-lock read gives (no lock held by default), or
    an exception it raises; ``lock_repo`` is the repo ``origin`` names.
    ``integration=None`` means no
    ``vergil.toml``; a string is the integration branch of a stub branching
    model, whose ``[publish].release`` is ``publish``.
    """
    mocks: dict[str, MagicMock] = {}
    repo = RepoWorktrees(main=RawWorktree(path=_ROOT, branch="develop"), linked=tuple(linked))
    with contextlib.ExitStack() as stack:

        def _patch(name: str, **kwargs: Any) -> None:
            mocks[name] = stack.enter_context(patch(f"{_MOD}.{name}", **kwargs))

        _patch("worktrees.discover_repo_worktrees", return_value=repo)
        if isinstance(integration, Exception):
            _patch("config.read_config", side_effect=integration)
        elif integration is None:
            _patch("config.read_config", side_effect=FileNotFoundError("vergil.toml not found"))
        else:
            cfg = SimpleNamespace(
                project=SimpleNamespace(branching_model=_STUB_MODEL),
                publish=SimpleNamespace(release=publish),
            )
            _patch("config.read_config", return_value=cfg)
            _patch("config.INTEGRATION_BRANCH_BY_MODEL", new={_STUB_MODEL: integration})
        if isinstance(pending, Exception):
            _patch("gather_pending_release", side_effect=pending)
        else:
            _patch("gather_pending_release", return_value=pending)
        if isinstance(open_prs, Exception):
            _patch("gather_open_prs", side_effect=open_prs)
        else:
            _patch("gather_open_prs", return_value=open_prs)
        _patch(
            "worktrees.gather_main_checkout_status",
            return_value=main_status or _main_status(),
        )
        _patch("worktrees.gather_worktree_status", side_effect=gather)
        _patch("worktrees.gather_detached_status", side_effect=detached)
        if isinstance(gone, Exception):
            _patch("worktrees.gone_branches", side_effect=gone)
        else:
            _patch("worktrees.gone_branches", return_value=gone or set())
        if still_present is not None:
            _patch("worktrees.worktree_still_present", return_value=still_present)
        if isinstance(merge, Exception):
            _patch("pr_await.standing_block", side_effect=merge)
        else:
            _patch("pr_await.standing_block", return_value=merge)
        _patch("lock_status.origin_repo", return_value=lock_repo)
        if isinstance(lock, Exception):
            _patch("lock_status.gather", side_effect=lock)
        else:
            _patch("lock_status.gather", return_value=lock or _no_lock())
        rc = main(argv or [])
    return rc, mocks


# -- row rendering --------------------------------------------------------------


def test_row_renders_loaded_workflow_status_verbatim() -> None:
    row = _row(
        _status("feature/1-x", WorktreeState.NO_PR, ahead=1, workflow_status="approved"), _NOW
    )
    assert row[_WORKFLOW_COL] == "approved"


def test_row_renders_dash_when_no_workflow_file() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1), _NOW)
    assert row[_WORKFLOW_COL] == "-"


def test_row_renders_unknown_on_workflow_read_error() -> None:
    row = _row(
        _status("feature/1-x", WorktreeState.NO_PR, ahead=1, workflow_error="bad json"), _NOW
    )
    assert row[_WORKFLOW_COL] == "unknown"


def test_row_marks_branch_gone() -> None:
    row = _row(_status("feature/1-x", WorktreeState.MERGED, pr=2), _NOW, gone=True)
    assert row[_BRANCH_COL] == "feature/1-x (branch gone)"


def test_row_uses_label_for_worktree_cell() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR), _NOW, label="/elsewhere/x")
    assert row[0] == "/elsewhere/x"


def test_row_renders_relative_ages() -> None:
    row = _row(
        _status(
            "feature/1-x",
            WorktreeState.NO_PR,
            ahead=1,
            last_commit_ts=_NOW - 3 * 86400,
            last_modified_ts=_NOW - 2 * 3600,
        ),
        _NOW,
    )
    assert row[_LAST_COMMIT_COL] == "3d ago"
    assert row[_LAST_MODIFIED_COL] == "2h ago"


def test_row_renders_dash_for_missing_timestamps() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1), _NOW)
    assert row[_LAST_COMMIT_COL] == "-"
    assert row[_LAST_MODIFIED_COL] == "-"


def test_format_age_hours() -> None:
    assert _format_age(_NOW - 2 * 3600, _NOW) == "2h ago"


def test_format_age_days() -> None:
    assert _format_age(_NOW - 3 * 86400, _NOW) == "3d ago"


def test_format_age_none_is_dash() -> None:
    assert _format_age(None, _NOW) == "-"


def test_format_age_future_clamps_to_zero() -> None:
    assert _format_age(_NOW + 5000, _NOW) == "0h ago"


# -- development section (today's table) ----------------------------------------


def test_main_summary_reports_prepared_count(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status(
            "feature/1-a",
            WorktreeState.NO_PR,
            ahead=1,
            workflow_status="approved",
            pr_prepared=True,
        ),
        _status("feature/2-b", WorktreeState.NO_PR, ahead=1, workflow_status="implementing"),
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "WORKFLOW" in out
    assert "1 PR prepared." in out


@pytest.mark.parametrize(
    "state", [WorktreeState.OPEN_PR, WorktreeState.MERGED, WorktreeState.CLOSED]
)
def test_prepared_count_leaves_out_a_branch_that_has_a_pr(
    capsys: pytest.CaptureFixture[str], state: WorktreeState
) -> None:
    """A ready state file whose branch already has a PR (one opened by hand never
    stamps the state) is not counted as prepared: the count follows the row (#4993)."""
    statuses = [
        _status("feature/1-a", WorktreeState.NO_PR, ahead=1, pr_prepared=True),
        _status("feature/2-b", state, pr=2, ahead=1, pr_prepared=True),
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    assert rc == 0
    assert "1 PR prepared." in capsys.readouterr().out


def test_main_surfaces_workflow_read_error_note(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status("feature/3-c", WorktreeState.NO_PR, ahead=1, workflow_error="not valid JSON"),
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out
    assert "note: feature/3-c:" in out
    assert "not valid JSON" in out
    assert "0 PRs prepared." in out


def test_main_groups_cruft_last_and_summarizes(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status("feature/1470-merged", WorktreeState.MERGED, pr=1471, ahead=2),
        _status("feature/1534-open", WorktreeState.OPEN_PR, pr=1544, ahead=2),
        _status("feature/1543-nopr", WorktreeState.NO_PR, ahead=1),
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("open-pr") < out.index("merged")
    assert out.index("no-pr") < out.index("merged")
    assert "1 active" in out
    assert "1 stalled (no-pr)" in out
    assert "1 cruft (removable)" in out
    assert "Run vrg-finalize-pr to clean cruft." in out


def test_main_surfaces_rebasing_worktree_in_development(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #2634: a detached-HEAD / mid-rebase worktree must be listed with a
    REBASING state and its resolved branch — in Development, where its
    recovered feature branch belongs."""
    rebasing = _status(
        "feature/2607-resume-attach",
        WorktreeState.REBASING,
        ahead=3,
        detail="rebase in progress (detached HEAD) — resolve conflicts and continue",
    )
    raw = RawWorktree(path=rebasing.worktree.path, branch=None)
    rc, _ = _run([raw], detached=[rebasing])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Development\n" in out
    assert "Other / unknown" not in out
    assert "rebasing" in out
    assert "feature/2607-resume-attach" in out
    assert "note: feature/2607-resume-attach:" in out
    assert "rebase in progress" in out


# -- an open PR's merge state (#4678) -------------------------------------------

_BRANCH_4529 = "feature/4529-docs-review-parallel-merging"
_REBASE_DETAIL = (
    "rebase in progress (detached HEAD) — resolve conflicts and run "
    "'vrg-git rebase --continue', or 'vrg-git rebase --abort' to undo"
)


def _conflict() -> AwaitResult:
    return AwaitResult(
        pr="https://github.com/o/r/pull/4673",
        reason="conflict",
        detail="the PR has a merge conflict with its base",
        head_sha="a" * 40,
        merge_state_status="DIRTY",
    )


def _dev_rows(out: str) -> list[list[str]]:
    """The Development table's rows, header first, split on column gaps."""
    lines = out.splitlines()
    start = lines.index("Development") + 1
    rows = []
    for line in lines[start:]:
        if not line or line.startswith("  note:"):
            break
        rows.append(re.split(r"\s{2,}", line.strip()))
    return rows


def test_open_pr_that_conflicts_on_a_clean_worktree(capsys: pytest.CaptureFixture[str]) -> None:
    status = _status(_BRANCH_4529, WorktreeState.OPEN_PR, pr=4673, ahead=1)
    rc, mocks = _run(_raws([status]), gather=[status], merge=_conflict())
    out = capsys.readouterr().out
    assert rc == 0
    mocks["pr_await.standing_block"].assert_called_once_with("4673")
    row = _dev_rows(out)[1]
    assert row[2:4] == ["#4673", "merge failed: conflict"]
    assert (
        f"  note: {_BRANCH_4529}: PR #4673 cannot merge (conflict): "
        "the PR has a merge conflict with its base\n"
    ) in out
    assert "rebase" not in out
    assert "1 PR cannot merge (see notes above)." in out


def test_open_pr_that_conflicts_while_its_branch_is_mid_rebase(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The #4673 case: the PR and both states are shown, and the note names the
    merge failure instead of how to finish a rebase."""
    status = _status(_BRANCH_4529, WorktreeState.REBASING, pr=4673, ahead=1, detail=_REBASE_DETAIL)
    raw = RawWorktree(path=status.worktree.path, branch=None)
    rc, _ = _run([raw], detached=[status], merge=_conflict())
    out = capsys.readouterr().out
    assert rc == 0
    row = _dev_rows(out)[1]
    assert row[2:4] == ["#4673", "rebasing, merge failed: conflict"]
    assert (
        f"  note: {_BRANCH_4529}: PR #4673 cannot merge (conflict): the PR has a merge "
        "conflict with its base; pr-watch is resolving it (rebase in progress)\n"
    ) in out
    assert "vrg-git rebase --continue" not in out


def test_mid_rebase_with_no_pr_keeps_the_rebase_note(capsys: pytest.CaptureFixture[str]) -> None:
    status = _status(_BRANCH_4529, WorktreeState.REBASING, ahead=1, detail=_REBASE_DETAIL)
    raw = RawWorktree(path=status.worktree.path, branch=None)
    rc, mocks = _run([raw], detached=[status])
    out = capsys.readouterr().out
    assert rc == 0
    mocks["pr_await.standing_block"].assert_not_called()
    row = _dev_rows(out)[1]
    assert row[2:4] == ["-", "rebasing"]
    assert f"  note: {_BRANCH_4529}: {_REBASE_DETAIL}\n" in out
    assert "cannot merge" not in out


def test_mid_rebase_with_an_open_pr_that_can_merge(capsys: pytest.CaptureFixture[str]) -> None:
    status = _status(_BRANCH_4529, WorktreeState.REBASING, pr=4673, ahead=1, detail=_REBASE_DETAIL)
    raw = RawWorktree(path=status.worktree.path, branch=None)
    rc, _ = _run([raw], detached=[status])
    out = capsys.readouterr().out
    assert rc == 0
    assert _dev_rows(out)[1][2:4] == ["#4673", "rebasing"]
    assert (
        f"  note: {_BRANCH_4529}: PR #4673 is open; its branch is mid-rebase "
        "(pr-watch updating it)\n"
    ) in out
    assert "vrg-git rebase --continue" not in out
    assert "cannot merge" not in out


def test_open_pr_that_can_merge_is_plain_open_pr(capsys: pytest.CaptureFixture[str]) -> None:
    status = _status("feature/1-a", WorktreeState.OPEN_PR, pr=5, ahead=1)
    rc, _ = _run(_raws([status]), gather=[status])
    out = capsys.readouterr().out
    assert rc == 0
    assert _dev_rows(out)[1][2:4] == ["#5", "open-pr"]
    assert "note:" not in out
    assert "cannot merge" not in out


def test_unreadable_merge_state_is_shown_not_hidden(capsys: pytest.CaptureFixture[str]) -> None:
    status = _status("feature/1-a", WorktreeState.OPEN_PR, pr=5, ahead=1)
    rc, _ = _run(
        _raws([status]), gather=[status], merge=FinalizeWatchError("5: reading the PR failed: 502")
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert _dev_rows(out)[1][2:4] == ["#5", "merge unknown"]
    assert (
        "  note: feature/1-a: PR #5: merge state unreadable: 5: reading the PR failed: 502\n" in out
    )


def test_merge_state_is_not_read_for_a_machinery_pr(capsys: pytest.CaptureFixture[str]) -> None:
    """A release or deps PR is merged directly, never with auto-merge, so it is
    never judged by vrg-pr-await's reasons."""
    statuses = [
        _status("release/2.1.200", WorktreeState.OPEN_PR, pr=7, ahead=1),
        _status("deps/update-2.1.200", WorktreeState.OPEN_PR, pr=8, ahead=1),
    ]
    rc, mocks = _run(_raws(statuses), gather=statuses, merge=_conflict())
    out = capsys.readouterr().out
    assert rc == 0
    mocks["pr_await.standing_block"].assert_not_called()
    assert "Release and dependency machinery" in out
    assert "merge failed" not in out


def test_merge_state_is_not_read_for_a_finished_pr(capsys: pytest.CaptureFixture[str]) -> None:
    status = _status("feature/1-a", WorktreeState.MERGED, pr=5)
    rc, mocks = _run(_raws([status]), gather=[status], merge=_conflict())
    capsys.readouterr()
    assert rc == 0
    mocks["pr_await.standing_block"].assert_not_called()


def test_main_surfaces_unknown_detail(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/9-x", WorktreeState.UNKNOWN, detail="gh boom")]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh boom" in out
    assert "0 cruft" in out
    assert "Run vrg-finalize-pr" not in out


def test_main_surfaces_reused_branch_detail(capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #1719: a reused-branch mismatch lands as NO_PR with a detail
    note — surfaced too, not just UNKNOWN, so the reuse never hides."""
    statuses = [
        _status(
            "feature/286-build-buckets",
            WorktreeState.NO_PR,
            pr=293,
            ahead=9,
            detail="closed PR #293 head 0ldd0cs does not match branch tip 7ead128 — reused",
        )
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "note: feature/286-build-buckets:" in out
    assert "#293" in out
    assert "0 cruft" in out


def test_main_merged_dirty_is_needs_attention_not_active(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #2347: merged-but-dirty worktrees land in needs-attention, never
    active, and are never hidden by a 'nothing to clean' message."""
    statuses = [
        _status(
            "feature/1-merged-dirty",
            WorktreeState.MERGED,
            pr=101,
            ahead=2,
            dirty=True,
            needs_attention=True,
            detail="merged PR #101 but worktree is dirty — 1 uncommitted path(s): dist/out.js. "
            "Commit, stash, or discard the changes, then run vrg-finalize-pr.",
        ),
        _status("feature/2-open", WorktreeState.OPEN_PR, pr=102, ahead=1),
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 active" in out
    assert "1 needs-attention" in out
    assert "0 cruft (removable)" in out
    assert "Some worktrees need attention" in out
    assert "note: feature/1-merged-dirty:" in out
    assert "dist/out.js" in out


def test_main_four_merged_dirty_never_reported_all_active(
    capsys: pytest.CaptureFixture[str],
) -> None:
    statuses = [
        _status(
            f"feature/{i}-stuck",
            WorktreeState.MERGED,
            pr=200 + i,
            ahead=1,
            dirty=True,
            needs_attention=True,
            detail=f"merged PR #{200 + i} but worktree is dirty — 1 uncommitted path(s): x.log.",
        )
        for i in range(4)
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "0 active" in out
    assert "4 needs-attention" in out
    assert "0 cruft (removable)" in out


def test_main_reused_branch_is_needs_attention_not_stalled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    statuses = [
        _status(
            "feature/286-build-buckets",
            WorktreeState.NO_PR,
            pr=293,
            ahead=9,
            needs_attention=True,
            detail="closed PR #293 head 0ldd0cs does not match branch tip 7ead128 — reused",
        )
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 needs-attention" in out
    assert "0 stalled (no-pr)" in out
    assert "note: feature/286-build-buckets:" in out


def test_main_includes_timestamp_headers(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/1-a", WorktreeState.NO_PR, ahead=1)]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "LAST COMMIT" in out
    assert "LAST MODIFIED" in out


# -- sections (issue #3386) -----------------------------------------------------


def test_main_renders_all_four_sections_in_order(capsys: pytest.CaptureFixture[str]) -> None:
    dev = _status("feature/1-a", WorktreeState.OPEN_PR, pr=5, ahead=1)
    release = _status("release/1.4.0", WorktreeState.MERGED, pr=6, ahead=0)
    deps = _status("deps/update-1.0.3", WorktreeState.OPEN_PR, pr=7, ahead=1, dirty=True)
    other = _status("experiment", WorktreeState.NO_PR, ahead=2)
    statuses = [other, deps, dev, release]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    headers = [
        "Main checkout",
        "Development",
        "Release and dependency machinery",
        "Other / unknown",
    ]
    positions = [out.index(h) for h in headers]
    assert positions == sorted(positions)
    machinery = out[out.index("Release and dependency machinery") : out.index("Other / unknown")]
    assert "release/1.4.0" in machinery
    assert "deps/update-1.0.3" in machinery
    assert "merged" in machinery
    assert "#6" in machinery
    assert "yes" in machinery  # the dirty deps worktree
    assert "experiment" in out[out.index("Other / unknown") :]
    assert (
        "5 worktrees: main checkout (ok), 1 development, "
        "2 release and dependency machinery, 1 other / unknown." in out
    )


def test_main_every_issue_branch_type_is_development(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status("bugfix/2-b", WorktreeState.NO_PR, ahead=1),
        _status("hotfix/3-c", WorktreeState.OPEN_PR, pr=9, ahead=1),
        _status("chore/4-dep-update-1.0.3", WorktreeState.NO_PR, ahead=1),
        _status("chore/no-number", WorktreeState.NO_PR, ahead=1),
        _status("spike/5-idea", WorktreeState.NO_PR, ahead=1),
    ]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    development = out[out.index("Development\n") : out.index("Other / unknown")]
    for branch in ("bugfix/2-b", "hotfix/3-c", "chore/4-dep-update-1.0.3"):
        assert branch in development
    other = out[out.index("Other / unknown\n") :]
    assert "chore/no-number" in other
    assert "spike/5-idea" in other
    assert "3 development" in out
    assert "2 other / unknown" in out


def test_main_omits_empty_sections_but_counts_them(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Main checkout" in out
    assert "Development\n" not in out
    assert "Release and dependency machinery\n" not in out
    assert "Other / unknown\n" not in out
    assert (
        "1 worktree: main checkout (ok), 0 development, "
        "0 release and dependency machinery, 0 other / unknown." in out
    )
    assert "Development: 0 active" in out


def test_main_machinery_cruft_is_not_counted_as_finalize_cruft(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finalize only acts on development worktrees, so a merged release
    worktree is shown as merged but never sold as vrg-finalize-pr cruft."""
    statuses = [_status("release/1.4.0", WorktreeState.MERGED, pr=6)]
    rc, _ = _run(_raws(statuses), gather=statuses)
    out = capsys.readouterr().out
    assert rc == 0
    assert "merged" in out
    assert "0 cruft (removable)" in out
    assert "Run vrg-finalize-pr" not in out


def test_main_detached_non_issue_worktree_is_other(capsys: pytest.CaptureFixture[str]) -> None:
    path = _ROOT / ".worktrees" / "tmp-wheel-build"
    detached = _status(
        "(detached)",
        WorktreeState.DETACHED,
        detail="detached HEAD — no branch checked out; check out a branch to resume work",
        path=path,
    )
    rc, _ = _run([RawWorktree(path=path, branch=None)], detached=[detached])
    out = capsys.readouterr().out
    assert rc == 0
    other = out[out.index("Other / unknown") :]
    assert "tmp-wheel-build" in other
    assert "detached" in other
    assert "note: tmp-wheel-build (detached): detached HEAD" in out
    assert "Development\n" not in out


def test_main_worktree_outside_container_is_other_with_full_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rogue = _status("feature/9-rogue", WorktreeState.NO_PR, ahead=1, path=Path("/elsewhere/rogue"))
    rc, _ = _run(_raws([rogue]), gather=[rogue])
    out = capsys.readouterr().out
    assert rc == 0
    other = out[out.index("Other / unknown") :]
    assert "/elsewhere/rogue" in other
    assert "feature/9-rogue" in other
    assert "Development\n" not in out


def test_main_checkout_ok_row(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mocks = _run([], main_status=_main_status(last_commit_ts=_NOW))
    out = capsys.readouterr().out
    assert rc == 0
    section = out[out.index("Main checkout") :]
    assert "CHECKOUT" in section
    assert "repo" in section
    assert "develop" in section
    assert " ok" in section
    mocks["worktrees.gather_main_checkout_status"].assert_called_once_with(
        RawWorktree(path=_ROOT, branch="develop"), integration="develop"
    )


def test_main_checkout_off_integration_is_flagged(capsys: pytest.CaptureFixture[str]) -> None:
    status = _main_status(branch="feature/worktree-and-hidden-repo-discovery")
    rc, _ = _run([], main_status=status)
    out = capsys.readouterr().out
    assert rc == 0
    assert "PROBLEM: not on develop" in out
    assert "main checkout (PROBLEM: not on develop)" in out


def test_main_checkout_dirty_and_mid_rebase_are_flagged_with_notes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = _main_status(
        branch=None,
        dirty=True,
        rebasing=True,
        notes=("1 uncommitted path(s): src/x.py", "rebase in progress — continue or abort"),
    )
    rc, _ = _run([], main_status=status)
    out = capsys.readouterr().out
    assert rc == 0
    assert "(detached)" in out
    assert "PROBLEM: not on develop, dirty, mid-rebase" in out
    assert "note: main checkout: 1 uncommitted path(s): src/x.py" in out
    assert "note: main checkout: rebase in progress" in out


def test_main_uses_configured_integration_branch(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mocks = _run([], integration="main", main_status=_main_status(integration="main"))
    assert rc == 0
    mocks["worktrees.gather_main_checkout_status"].assert_called_once_with(
        RawWorktree(path=_ROOT, branch="develop"), integration="main"
    )
    mocks["config.read_config"].assert_called_once_with(_ROOT)
    assert "integration branch: main" in capsys.readouterr().out


def test_main_without_vergil_toml_defaults_to_develop_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, mocks = _run([], integration=None)
    out = capsys.readouterr().out
    assert rc == 0
    mocks["worktrees.gather_main_checkout_status"].assert_called_once_with(
        RawWorktree(path=_ROOT, branch="develop"), integration="develop"
    )
    assert "Main checkout (integration branch: develop)" in out
    assert "note: no vergil.toml; integration branch defaults to develop" in out


def test_main_invalid_config_fails_with_reason(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([], integration=ConfigError("vergil.toml is not valid TOML"))
    assert rc == 1
    assert "vergil.toml is not valid TOML" in capsys.readouterr().err


def test_main_marks_gone_branches(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status("feature/1-merged", WorktreeState.MERGED, pr=2),
        _status("feature/3-live", WorktreeState.OPEN_PR, pr=4, ahead=1),
    ]
    rc, mocks = _run(
        _raws(statuses),
        gather=statuses,
        gone={"feature/1-merged", "develop"},
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "feature/1-merged (branch gone)" in out
    assert "feature/3-live (branch gone)" not in out
    assert "develop (branch gone)" in out  # the main checkout's branch too
    (branches,), _ = mocks["worktrees.gone_branches"].call_args
    assert set(branches) == {"develop", "feature/1-merged", "feature/3-live"}


def test_main_gone_lookup_failure_is_shown_with_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    statuses = [_status("feature/1-a", WorktreeState.NO_PR, ahead=1)]
    err = subprocess.CalledProcessError(128, ("git", "ls-remote"), stderr="fatal: no network\n")
    rc, _ = _run(_raws(statuses), gather=statuses, gone=err)
    out = capsys.readouterr().out
    assert rc == 0
    assert "feature/1-a" in out
    assert "note: branch-gone check failed: fatal: no network" in out


def test_main_gone_lookup_failure_without_stderr_uses_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = subprocess.CalledProcessError(128, ("git", "ls-remote"))
    rc, _ = _run([], gone=err)
    assert rc == 0
    assert "note: branch-gone check failed: Command" in capsys.readouterr().out


# -- concurrent removal (issue #3187) --------------------------------------------

_RACE_ERR = subprocess.CalledProcessError(128, ("git", "rev-list"))


def test_main_skips_worktree_removed_mid_scan(capsys: pytest.CaptureFixture[str]) -> None:
    """A worktree a concurrent finalize removes between discovery and
    status-gathering is dropped with a visible note — no traceback."""
    live = _status("feature/1-live", WorktreeState.NO_PR, ahead=1)
    gone = RawWorktree(path=_ROOT / ".worktrees" / "issue-327-gone", branch="feature/327-gone")
    rc, mocks = _run([*_raws([live]), gone], gather=[live, _RACE_ERR], still_present=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "feature/1-live" in out
    assert "2 worktrees" in out
    assert "note: issue-327-gone (feature/327-gone): removed by a concurrent operation" in out
    mocks["worktrees.worktree_still_present"].assert_called_once_with(
        gone, _ROOT, any_location=True
    )


def test_main_all_worktrees_removed_mid_scan(capsys: pytest.CaptureFixture[str]) -> None:
    gone = RawWorktree(path=_ROOT / ".worktrees" / "issue-9-gone", branch=None)
    rc, _ = _run([gone], detached=_RACE_ERR, still_present=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 worktree:" in out
    assert "note: issue-9-gone ((detached)): removed by a concurrent operation" in out


def test_main_reraises_git_failure_on_present_worktree() -> None:
    """A git failure on a worktree that still exists is real — never swallowed."""
    raw = RawWorktree(path=_ROOT / ".worktrees" / "issue-5-x", branch="feature/5-x")
    with pytest.raises(subprocess.CalledProcessError):
        _run([raw], gather=_RACE_ERR, still_present=True)


# -- pending release (epic vergil-project/.github#1049) ---------------------------


def _ago(hours: float) -> float:
    """A timestamp *hours* (plus half an hour) before the real clock, so the
    rendered age is ``<hours>h ago`` however long the test takes."""
    return time.time() - (hours + 0.5) * _HOUR


def _sha(n: int) -> str:
    return f"{n:040x}"


def _change(
    pr: int | None,
    branch: str | None,
    title: str,
    kind: ChangeKind,
    type_label: str,
    merged_ts: float,
    *,
    sha: int = 1,
) -> PendingChange:
    return PendingChange(
        pr_number=pr,
        branch=branch,
        title=title,
        kind=kind,
        type_label=type_label,
        merged_ts=merged_ts,
        shas=(_sha(sha),),
    )


_TAGS = ReleaseTags(latest="v2.1.303", rolling="v2.1", rolling_target="v2.1.303")


def _pending(*changes: PendingChange, tags: ReleaseTags = _TAGS) -> PendingRelease:
    return PendingRelease(tags=tags, main_moved_ts=_ago(4), changes=tuple(changes))


def _mockup() -> PendingRelease:
    """The spec's §5.3 mock-up data."""
    return _pending(
        _change(
            4251,
            "release/post-2.1.303",
            "chore(release): back-merge 2.1.303 and bump to 2.1.304",
            ChangeKind.RELEASE,
            "release",
            _ago(4),
            sha=1,
        ),
        _change(
            4258,
            "deps/update-2.1.304",
            "chore(deps): dependency update for 2.1.304",
            ChangeKind.DEPS,
            "deps",
            _ago(4),
            sha=2,
        ),
        _change(
            4252,
            "feature/4249-refuse-ubuntu",
            "feat(validate): refuse ubuntu-latest runner labels",
            ChangeKind.OTHER,
            "feat",
            _ago(1),
            sha=3,
        ),
        _change(
            4255,
            "feature/4253-batch-docs",
            "docs(batch): document batched same-repo PRs",
            ChangeKind.OTHER,
            "docs",
            _ago(0),
            sha=4,
        ),
    )


def _section(out: str) -> list[str]:
    """The Pending release section's lines, heading first, up to the blank line."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Pending release ("))
    end = lines.index("", start)
    return lines[start:end]


def test_pending_section_renders_rows_in_order(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mocks = _run([], publish=True, pending=_mockup())
    out = capsys.readouterr().out
    assert rc == 0
    mocks["gather_pending_release"].assert_called_once_with(integration="develop")
    section = _section(out)
    assert section[0] == (
        "Pending release (develop → main): "
        "latest release v2.1.303, main moved 4h ago; rolling tag v2.1 → v2.1.303"
    )
    assert section[1].split() == ["PR", "BRANCH", "TYPE", "MERGED", "TITLE"]
    rows = [line.split()[:4] for line in section[2:6]]
    assert rows == [
        ["#4251", "release/post-2.1.303", "release", "4h"],
        ["#4258", "deps/update-2.1.304", "deps", "4h"],
        ["#4252", "feature/4249-refuse-ubuntu", "feat", "1h"],
        ["#4255", "feature/4253-batch-docs", "docs", "0h"],
    ]
    assert section[5].endswith("docs(batch): document batched same-repo PRs")
    assert section[6] == (
        "4 changes pending release: 1 dependency update, 1 release back-merge, "
        "0 automation PRs, 0 lifecycle PRs, 2 other PRs."
    )
    assert len(section) == 7
    # The section sits after the worktree sections, before the summary lines.
    assert out.index("Main checkout") < out.index("Pending release (") < out.index("1 worktree:")


def test_pending_section_uses_configured_integration_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, mocks = _run(
        [],
        integration="trunk",
        main_status=_main_status(integration="trunk"),
        publish=True,
        pending=_pending(),
    )
    out = capsys.readouterr().out
    assert rc == 0
    mocks["gather_pending_release"].assert_called_once_with(integration="trunk")
    assert "Pending release (trunk → main): latest release v2.1.303," in out
    assert "Nothing pending release: trunk and main are level." in out


def test_release_line_lag() -> None:
    tags = ReleaseTags(latest="v2.1.303", rolling="v2.1", rolling_target="v2.1.302")
    line = _release_line(tags, _NOW - 2 * _HOUR, _NOW)
    assert line.endswith("rolling tag v2.1 → v2.1.302 (lags v2.1.303)")


def test_release_line_no_tags() -> None:
    line = _release_line(ReleaseTags(None, None, None), _NOW - 30 * _HOUR, _NOW)
    assert line == "latest release no release yet, main moved 1d ago; no rolling tag"


def test_release_line_rolling_on_untagged_commit() -> None:
    line = _release_line(ReleaseTags("v2.1.303", "v2.1", "abc1234"), _NOW, _NOW)
    assert line.endswith("rolling tag v2.1 → abc1234")
    assert "lags" not in line


def test_release_line_rolling_without_release() -> None:
    line = _release_line(ReleaseTags(None, "v2.1", "abc1234"), _NOW, _NOW)
    assert line == "latest release no release yet, main moved 0h ago; rolling tag v2.1 → abc1234"
    assert "lags" not in line
    assert "None" not in line


def test_release_line_rolling_tag_without_latest_has_no_lag() -> None:
    line = _release_line(ReleaseTags(None, "v2.1", "v2.1.302"), _NOW, _NOW)
    assert line.endswith("rolling tag v2.1 → v2.1.302")
    assert "lags" not in line


def test_nothing_pending(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([], publish=True, pending=_pending())
    out = capsys.readouterr().out
    assert rc == 0
    section = _section(out)
    assert section == [
        "Pending release (develop → main): "
        "latest release v2.1.303, main moved 4h ago; rolling tag v2.1 → v2.1.303",
        "Nothing pending release: develop and main are level.",
    ]
    assert "TITLE" not in out


def test_no_pr_change_is_problem(capsys: pytest.CaptureFixture[str]) -> None:
    change = _change(None, None, "Fix a typo", ChangeKind.NO_PR, "-", _ago(3), sha=0xABC1234)
    rc, _ = _run([], publish=True, pending=_pending(change))
    out = capsys.readouterr().out
    assert rc == 0
    section = _section(out)
    assert section[2].split() == ["PROBLEM", "-", "-", "3h", "ago", "Fix", "a", "typo"]
    sha7 = change.shas[0][:7]
    assert section[3] == f"  PROBLEM: {sha7} reached develop without a PR"
    assert section[4] == (
        "1 change pending release: 0 dependency updates, 0 release back-merges, "
        "0 automation PRs, 0 lifecycle PRs, 0 other PRs, 1 PROBLEM: commits with no PR."
    )


def test_untrusted_pr_change_is_problem(capsys: pytest.CaptureFixture[str]) -> None:
    change = _change(9, None, "Fix a typo", ChangeKind.UNTRUSTED, "untrusted", _ago(3))
    rc, _ = _run([], publish=True, pending=_pending(change))
    out = capsys.readouterr().out
    assert rc == 0
    section = _section(out)
    assert section[2].split() == ["#9", "-", "untrusted", "3h", "ago", "Fix", "a", "typo"]
    assert section[3] == (
        "  PROBLEM: #9 is not a same-repo PR with a trusted author; "
        "its title, branch and labels are not read"
    )
    assert section[4] == (
        "1 change pending release: 0 dependency updates, 0 release back-merges, "
        "0 automation PRs, 0 lifecycle PRs, 0 other PRs, "
        "1 PROBLEM: not a same-repo PR with a trusted author."
    )


def test_automation_pr_shows_as_automation(capsys: pytest.CaptureFixture[str]) -> None:
    sync = _change(
        4301,
        "automation/repo-config-sync/123",
        "chore(repo-config): sync managed files",
        ChangeKind.AUTOMATION,
        "automation",
        _ago(2),
        sha=1,
    )
    dev = _change(4302, "feature/4302-x", "feat: x", ChangeKind.OTHER, "feat", _ago(1), sha=2)
    rc, _ = _run([], publish=True, pending=_pending(sync, dev))
    out = capsys.readouterr().out
    assert rc == 0
    section = _section(out)
    rows = [line.split()[:3] for line in section[2:4]]
    assert rows == [
        ["#4301", "automation/repo-config-sync/123", "automation"],
        ["#4302", "feature/4302-x", "feat"],
    ]
    # Counted apart from development (other) PRs.
    assert section[4] == (
        "2 changes pending release: 0 dependency updates, 0 release back-merges, "
        "1 automation PR, 0 lifecycle PRs, 1 other PR."
    )


def test_lifecycle_pr_shows_as_lifecycle(capsys: pytest.CaptureFixture[str]) -> None:
    lifecycle = _change(
        4401,
        "lifecycle/rust-add-1.99",
        "feat(versions): add rust 1.99",
        ChangeKind.LIFECYCLE,
        "lifecycle",
        _ago(2),
        sha=1,
    )
    dev = _change(4402, "feature/4402-x", "feat: x", ChangeKind.OTHER, "feat", _ago(1), sha=2)
    rc, _ = _run([], publish=True, pending=_pending(lifecycle, dev))
    out = capsys.readouterr().out
    assert rc == 0
    section = _section(out)
    rows = [line.split()[:3] for line in section[2:4]]
    assert rows == [
        ["#4401", "lifecycle/rust-add-1.99", "lifecycle"],
        ["#4402", "feature/4402-x", "feat"],
    ]
    # Counted apart from development (other) PRs, as the open-PR section does.
    assert section[4] == (
        "2 changes pending release: 0 dependency updates, 0 release back-merges, "
        "0 automation PRs, 1 lifecycle PR, 1 other PR."
    )


def test_pending_summary_counts_buckets() -> None:
    changes = [
        _change(1, "deps/update-1", "chore(deps): a", ChangeKind.DEPS, "deps", _NOW),
        _change(2, "deps/update-2", "chore(deps): b", ChangeKind.DEPS, "deps", _NOW),
        _change(3, "release/post-1", "chore(release): c", ChangeKind.RELEASE, "release", _NOW),
        _change(4, "feature/4-d", "feat: d", ChangeKind.OTHER, "feat", _NOW),
        _change(5, "automation/a/1", "chore: e", ChangeKind.AUTOMATION, "automation", _NOW),
        _change(6, "automation/a/2", "chore: f", ChangeKind.AUTOMATION, "automation", _NOW),
        _change(7, "automation/b/3", "chore: g", ChangeKind.AUTOMATION, "automation", _NOW),
        _change(8, "lifecycle/rust-add-1.99", "feat: h", ChangeKind.LIFECYCLE, "lifecycle", _NOW),
        _change(None, None, "e", ChangeKind.NO_PR, "-", _NOW),
        _change(None, None, "f", ChangeKind.NO_PR, "-", _NOW),
    ]
    assert _pending_summary(changes) == (
        "10 changes pending release: 2 dependency updates, 1 release back-merge, "
        "3 automation PRs, 1 lifecycle PR, 1 other PR, 2 PROBLEM: commits with no PR."
    )


def test_pending_error_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/1-a", WorktreeState.OPEN_PR, pr=5, ahead=1)]
    rc, _ = _run(
        _raws(statuses), gather=statuses, publish=True, pending=PendingReleaseError("boom")
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert _section(out) == [
        "Pending release (develop → main)",
        "ERROR: pending release: boom",
    ]
    assert "2 worktrees: main checkout (ok), 1 development" in out
    assert "Development: 1 active" in out


def test_section_skipped_when_not_published(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mocks = _run([], publish=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Pending release" not in out
    assert "pending-release" not in out
    mocks["gather_pending_release"].assert_not_called()


def test_section_skipped_without_config(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mocks = _run([], integration=None)
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    note = lines.index("  note: no vergil.toml; integration branch defaults to develop")
    assert lines[note + 1] == "  note: no vergil.toml; pending-release section skipped"
    assert not any(line.startswith("Pending release") for line in lines)
    mocks["gather_pending_release"].assert_not_called()


def test_long_title_truncated() -> None:
    title = "x" * 100
    change = _change(7, "feature/7-x", title, ChangeKind.OTHER, "-", _NOW)
    rows = _pending_rows([change], _NOW)
    cell = rows[0][-1]
    assert len(cell) == _TITLE_WIDTH == 72
    assert cell == "x" * 71 + "…"


def test_title_at_width_is_kept() -> None:
    title = "y" * 72
    change = _change(7, "feature/7-y", title, ChangeKind.OTHER, "-", _NOW)
    assert _pending_rows([change], _NOW)[0][-1] == title


# -- open PRs with no local worktree (issue #4581) -------------------------------


def _open(
    number: int,
    branch: str,
    title: str = "feat(x): a thing",
    *,
    kind: OpenPRKind = OpenPRKind.OTHER,
    type_label: str = "feat",
    author: str = "someone",
    head_owner: str | None = None,
    base: str = "develop",
    draft: bool = False,
    ci: str = "success",
    created_ts: float | None = None,
) -> OpenPR:
    return OpenPR(
        number=number,
        title=title,
        branch=branch,
        head_owner=head_owner,
        base=base,
        kind=kind,
        type_label=type_label,
        author=author,
        draft=draft,
        ci=ci,
        created_ts=created_ts if created_ts is not None else _ago(2),
    )


def _open_section(out: str) -> list[str]:
    """The open-PR section's lines, heading first, up to the blank line."""
    lines = out.splitlines()
    start = lines.index("Open PRs on origin with no local worktree")
    end = lines.index("", start)
    return lines[start:end]


def test_open_prs_without_worktree_are_listed(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/1-a", WorktreeState.OPEN_PR, pr=5, ahead=1)]
    prs = (
        _open(5, "feature/1-a"),
        _open(
            875,
            "lifecycle/rust-add-1.99",
            "feat(versions): add rust 1.99",
            kind=OpenPRKind.LIFECYCLE,
            type_label="lifecycle",
            author="vergil-project-automation",
            ci="pending",
        ),
        _open(
            880,
            "automation/sync/123",
            "chore: sync repo config",
            kind=OpenPRKind.AUTOMATION,
            type_label="automation",
            author="vergil-project-automation",
            ci="failure",
        ),
        _open(881, "feature/9-cloud", "fix: from a cloud VM", type_label="fix", draft=True),
    )
    rc, mocks = _run(_raws(statuses), gather=statuses, open_prs=prs)
    out = capsys.readouterr().out
    assert rc == 0
    mocks["gather_open_prs"].assert_called_once_with()
    section = _open_section(out)
    assert section[1].split() == [
        "PR",
        "BRANCH",
        "BASE",
        "TYPE",
        "AUTHOR",
        "CI",
        "OPENED",
        "TITLE",
    ]
    rows = [line.split()[:7] for line in section[2:5]]
    assert rows == [
        [
            "#875",
            "lifecycle/rust-add-1.99",
            "develop",
            "lifecycle",
            "vergil-project-automation",
            "pending",
            "2h",
        ],
        [
            "#880",
            "automation/sync/123",
            "develop",
            "automation",
            "vergil-project-automation",
            "failure",
            "2h",
        ],
        ["#881", "feature/9-cloud", "develop", "fix", "someone", "success", "2h"],
    ]
    assert section[4].endswith("[draft] fix: from a cloud VM")
    assert section[5] == (
        "3 open PRs with no local worktree: 1 automation PR, 1 lifecycle PR, "
        "0 dependency updates, 0 release PRs, 1 other PR."
    )
    assert len(section) == 6
    # After the worktree sections, before the summary lines.
    assert out.index("Development") < out.index(section[0]) < out.index("2 worktrees:")


def test_open_prs_section_precedes_pending_release(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([], publish=True, pending=_pending(), open_prs=(_open(3, "deps/update-1"),))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("Open PRs on origin") < out.index("Pending release (")


def test_open_pr_backed_by_worktree_branch_without_pr_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The worktree's own PR lookup failed (no number), but its branch matches.
    statuses = [_status("feature/1-a", WorktreeState.UNKNOWN, detail="lookup failed")]
    rc, _ = _run(_raws(statuses), gather=statuses, open_prs=(_open(5, "feature/1-a"),))
    assert rc == 0
    assert _open_section(capsys.readouterr().out)[1:] == [
        "None: 1 open PR on origin, each with a local worktree."
    ]


def test_fork_pr_is_never_backed_by_a_same_named_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    statuses = [_status("feature/1-a", WorktreeState.NO_PR)]
    fork = _open(6, "feature/1-a", head_owner="stranger", author="stranger")
    rc, _ = _run(_raws(statuses), gather=statuses, open_prs=(fork,))
    assert rc == 0
    section = _open_section(capsys.readouterr().out)
    assert section[2].split()[:2] == ["#6", "stranger:feature/1-a"]


def test_no_open_prs(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([])
    assert rc == 0
    assert _open_section(capsys.readouterr().out) == [
        "Open PRs on origin with no local worktree",
        "None: origin has no open PRs.",
    ]


def test_open_prs_error_is_shown_and_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/1-a", WorktreeState.OPEN_PR, pr=5, ahead=1)]
    rc, _ = _run(
        _raws(statuses),
        gather=statuses,
        publish=True,
        pending=_pending(),
        open_prs=OpenPRsError("HTTP 502"),
    )
    out = capsys.readouterr().out
    assert rc == 1
    # A failed lookup is an error, never "no open PRs".
    assert _open_section(out) == [
        "Open PRs on origin with no local worktree",
        "ERROR: open PRs: HTTP 502",
    ]
    assert "None:" not in out
    assert "open PRs with no local worktree:" not in out
    # The rest still prints.
    assert "Nothing pending release" in out
    assert "Development: 1 active" in out


# -- the Release lock section ------------------------------------------------------


def _lock_section(out: str) -> list[str]:
    lines = out.splitlines()
    start = lines.index("Release lock")
    end = next((i for i in range(start + 1, len(lines)) if not lines[i]), len(lines))
    return lines[start + 1 : end]


def test_repo_status_shows_lock_section(capsys: pytest.CaptureFixture[str]) -> None:
    rec = LockRecord(
        holder_identity="human",
        holder_host="mac.local",
        version="2.1.400",
        worktree="/w/.worktrees/release-2.1.400",
        tracking_issue=f"{_LOCK_REPO}#5300",
        stage="cut",
        health="running",
        broken_stage="",
        started_at="2026-11-27T11:00:00Z",
        heartbeat_at="2026-11-27T11:59:00Z",
        token="t",
    )
    held = LockStatus(
        repo=_LOCK_REPO,
        state=LockState(sha="c" * 40, record=rec, error=""),
        staleness=Staleness(
            stale=False,
            live=True,
            reason="running with a fresh heartbeat",
            tracking_issue=rec.tracking_issue,
        ),
        heartbeat_age_secs=60,
        held_prs=(HeldPR(number=12, url="u12"),),
    )
    rc, mocks = _run([], lock=held)
    assert rc == 0
    out = capsys.readouterr().out
    section = _lock_section(out)
    assert "release lock: held" in section
    assert "holder: human on mac.local" in section
    assert "version: 2.1.400" in section
    assert f"tracking issue: {_LOCK_REPO}#5300" in section
    assert "stage: cut" in section
    assert "health: running" in section
    assert "stale: no (running with a fresh heartbeat)" in section
    assert "held PRs: #12" in section
    assert any(line.startswith("heartbeat: ") and "(60s ago)" in line for line in section)
    mocks["lock_status.gather"].assert_called_once_with(_LOCK_REPO)
    # Beside everything shown today: after the open PRs, before the summary line.
    assert out.index("Open PRs on origin with no local worktree") < out.index("Release lock")
    assert out.index("Release lock") < out.rindex("1 worktree:")


def test_repo_status_no_lock(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([])
    assert rc == 0
    assert _lock_section(capsys.readouterr().out) == ["no release lock held"]


def test_repo_status_lock_read_failure_is_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([], lock=RefReadError("reading refs/vergil/release-lock failed: HTTP 502"))
    assert rc == 1
    section = _lock_section(capsys.readouterr().out)
    assert section == ["ERROR: release lock: reading refs/vergil/release-lock failed: HTTP 502"]


def test_repo_status_lock_non_github_origin_is_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mocks = _run([], lock_repo=None)
    assert rc == 1
    section = _lock_section(capsys.readouterr().out)
    assert section == ["ERROR: release lock: origin is not a github.com remote"]
    mocks["lock_status.gather"].assert_not_called()
