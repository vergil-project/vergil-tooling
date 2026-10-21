"""Tests for vergil_tooling.bin.vrg_worktree_status."""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_worktree_status import _format_age, _row, main
from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.worktrees import (
    MainCheckoutStatus,
    RawWorktree,
    RepoWorktrees,
    Worktree,
    WorktreeState,
    WorktreeStatus,
)

_MOD = "vergil_tooling.bin.vrg_worktree_status"
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
    gone: set[str] | Exception | None = None,
    still_present: bool | None = None,
    argv: list[str] | None = None,
) -> tuple[int, dict[str, MagicMock]]:
    """Run main() against a mocked repo: main checkout at /repo plus *linked*.

    ``gather`` / ``detached`` are ``side_effect`` values (a list of results or
    an exception); ``integration`` / ``gone`` are raised when they are
    exceptions and returned otherwise.
    """
    mocks: dict[str, MagicMock] = {}
    repo = RepoWorktrees(main=RawWorktree(path=_ROOT, branch="develop"), linked=tuple(linked))
    with contextlib.ExitStack() as stack:

        def _patch(name: str, **kwargs: Any) -> None:
            mocks[name] = stack.enter_context(patch(f"{_MOD}.{name}", **kwargs))

        _patch("worktrees.discover_repo_worktrees", return_value=repo)
        if isinstance(integration, Exception):
            _patch("config.integration_branch", side_effect=integration)
        else:
            _patch("config.integration_branch", return_value=integration)
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
    assert "0 PR prepared." in out


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


def test_main_omits_empty_sections_but_counts_them(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Main checkout" in out
    assert "Development\n" not in out
    assert "Release and dependency machinery\n" not in out
    assert "Other / unknown\n" not in out
    assert (
        "1 worktrees: main checkout (ok), 0 development, "
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
    mocks["config.integration_branch"].assert_called_once_with(_ROOT)
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
    assert "1 worktrees" in out
    assert "note: issue-9-gone ((detached)): removed by a concurrent operation" in out


def test_main_reraises_git_failure_on_present_worktree() -> None:
    """A git failure on a worktree that still exists is real — never swallowed."""
    raw = RawWorktree(path=_ROOT / ".worktrees" / "issue-5-x", branch="feature/5-x")
    with pytest.raises(subprocess.CalledProcessError):
        _run([raw], gather=_RACE_ERR, still_present=True)
