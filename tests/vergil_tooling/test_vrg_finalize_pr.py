"""Tests for vergil_tooling.bin.vrg_finalize_pr."""

from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_finalize_pr import (
    FinalizeContext,
    FinalizeError,
    _check_cd_workflow_status,
    _parse_pr_list,
    _resolve_open_prs,
    _resolve_strategy,
    _run_finalize_batch,
    _stage_cd_check,
    _stage_merge,
    _stage_provenance,
    _stage_validation,
    _worktree_is_dirty,
    build_stages,
    main,
    parse_args,
)
from vergil_tooling.lib.pr_merge import MergeAbortError
from vergil_tooling.lib.pr_provenance import Action, ProvenanceResult, Role
from vergil_tooling.lib.worktrees import Worktree, WorktreeState, WorktreeStatus

_MOD = "vergil_tooling.bin.vrg_finalize_pr"

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _main_worktree() -> Iterator[None]:
    """Default every test to running in the main worktree.

    Individual tests can override by patching is_main_worktree directly —
    the innermost patch wins.
    """
    with patch(_MOD + ".git.is_main_worktree", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _interactive_defaults() -> Iterator[None]:
    """Default: TTY present, no worktree candidates, cleanup confirmed.

    Inference tests override by patching these targets directly — the
    innermost patch wins.
    """
    with (
        patch(_MOD + ".worktrees.require_tty"),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".confirm", return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def _clean_working_tree() -> Iterator[None]:
    """Default every test to a clean working tree.

    Individual tests can override by patching working_tree_status
    directly — the innermost patch wins.
    """
    with patch(_MOD + ".git.working_tree_status", return_value=""):
        yield


@pytest.fixture(autouse=True)
def _validation_passes() -> Iterator[None]:
    """Default: the post-finalization validation child succeeds.

    The validation stage streams through progress.run (issue #1479);
    tests that assert validation behavior re-patch it directly — the
    innermost patch wins.
    """
    with patch(_MOD + ".progress.run", return_value=0):
        yield


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.target_branch == "develop"
    assert args.dry_run is False
    assert args.strategy is None


def test_parse_args_custom() -> None:
    args = parse_args(["--target-branch", "main", "--dry-run"])
    assert args.target_branch == "main"
    assert args.dry_run is True


def test_main_rejects_secondary_worktree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_root = tmp_path / "main-wt"
    main_root.mkdir()

    with (
        patch(_MOD + ".git.is_main_worktree", return_value=False),
        patch(_MOD + ".git.main_worktree_root", return_value=main_root),
    ):
        result = main([])

    assert result == 1
    err = capsys.readouterr().err
    assert "must be run from the main worktree" in err
    assert str(main_root) in err


def _make_profile(tmp_path: Path, model: str) -> None:
    (tmp_path / "vergil.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "{model}"\nrelease-model = "tagged-release"\n'
        f'primary-language = "python"\n\n[dependencies]\nvergil = "v2.0"\n'
        f'\n[ci]\nversions = ["3.14"]\n'
    )


_PR_EVIDENCE = {"number": "9", "url": "https://github.com/o/r/pull/9", "title": "Done"}


@contextmanager
def _sweep_guards_pass() -> Iterator[None]:
    """Make the ancestry-sweep guards (issue #1445) pass.

    The candidate branch resolves to a different SHA than the target
    (it has commits of its own) and a closed PR exists as merge
    evidence, so the sweep proceeds to delete it.
    """
    with (
        patch(_MOD + ".git.commit_sha", side_effect=lambda ref: f"sha-of-{ref}"),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=_PR_EVIDENCE),
    ):
        yield


def test_main_library_release(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run") as mock_run,
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["feature/x", "develop"],
        ),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0
    mock_run.assert_any_call("checkout", "develop")
    mock_run.assert_any_call("branch", "-D", "feature/x")


def test_main_cleanup_syncs_target_without_pull(tmp_path: Path) -> None:
    """Cleanup must fetch then ff-merge the remote-tracking ref — never
    `pull`, whose FETCH_HEAD dependence races concurrent fetches
    (issue #1499)."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["feature/x", "develop"],
        ),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    fetch_idx = next(
        i
        for i, c in enumerate(git_run_calls)
        if c == ("fetch", "--tags", "--force", "origin", "develop")
    )
    merge_idx = next(
        i for i, c in enumerate(git_run_calls) if c == ("merge", "--ff-only", "origin/develop")
    )
    assert fetch_idx < merge_idx
    assert not any(c[0] == "pull" for c in git_run_calls)


def test_main_already_on_target(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0


def test_main_dry_run(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run") as mock_git_run,
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["feature/x"],
        ),
    ):
        result = main(["--dry-run"])
    assert result == 0
    mock_git_run.assert_not_called()


def test_main_no_profile(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0


def test_main_unrecognized_model(tmp_path: Path) -> None:
    _make_profile(tmp_path, "unknown-model")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
    ):
        result = main([])
    assert result == 1


def test_main_application_promotion(tmp_path: Path) -> None:
    _make_profile(tmp_path, "application-promotion")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["develop", "release", "main", "feature/y"],
        ),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0


def test_main_docs_single_branch(tmp_path: Path) -> None:
    _make_profile(tmp_path, "docs-single-branch")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0


def test_main_no_deleted_branches(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=["develop"]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0


def test_main_validation_fails(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(
            _MOD + ".progress.run",
            side_effect=subprocess.CalledProcessError(1, ("vrg-container-run",)),
        ),
        patch(_MOD + "._check_cd_workflow_status", return_value=None) as cd_check,
    ):
        result = main([])
    assert result == 1
    # fail_defer: a validation failure must not skip the CD check.
    cd_check.assert_called_once()


# -- _check_cd_workflow_status (issue #303) --------------------------------


def _gh_run_json(conclusion: str | None) -> str:
    """Build a single-element gh run list JSON response."""
    return json.dumps(
        [
            {
                "conclusion": conclusion,
                "databaseId": 12345,
                "headSha": "abc123def456",
                "createdAt": "2026-04-26T18:00:00Z",
                "url": "https://github.com/owner/repo/actions/runs/12345",
            }
        ]
    )


def test_check_cd_workflow_returns_none_when_gh_fails() -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=1, stdout="", stderr="oops"),
    ):
        assert _check_cd_workflow_status("develop") is None


def test_check_cd_workflow_returns_none_when_no_runs() -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="[]"),
    ):
        assert _check_cd_workflow_status("develop") is None


def test_check_cd_workflow_returns_none_on_success() -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout=_gh_run_json("success")),
    ):
        assert _check_cd_workflow_status("develop") is None


def test_check_cd_workflow_returns_none_on_in_progress() -> None:
    # gh reports null conclusion (in_progress / queued).
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout=_gh_run_json(None)),
    ):
        assert _check_cd_workflow_status("develop") is None


def test_check_cd_workflow_returns_message_on_failure() -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout=_gh_run_json("failure")),
    ):
        msg = _check_cd_workflow_status("develop")
    assert msg is not None
    assert "12345" in msg
    assert "develop" in msg
    assert "abc123d" in msg  # short sha
    assert "failure" in msg
    assert "actions/runs/12345" in msg


def test_check_cd_workflow_returns_none_on_malformed_json() -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="not json"),
    ):
        assert _check_cd_workflow_status("develop") is None


def test_check_cd_workflow_returns_none_on_empty_stdout() -> None:
    # Defensive: stdout missing entirely (None) shouldn't crash.
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout=None),
    ):
        assert _check_cd_workflow_status("develop") is None


def test_main_returns_one_on_docs_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(
            _MOD + "._check_cd_workflow_status",
            return_value=(
                "CD workflow run 999 on develop (deadbee) ended with conclusion 'failure'."
            ),
        ),
    ):
        result = main([])
    assert result == 1
    assert "CD workflow" in capsys.readouterr().out


def test_main_skips_docs_check_on_dry_run(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(
            _MOD + "._check_cd_workflow_status",
            return_value="should not appear",
        ) as mock_check,
    ):
        result = main(["--dry-run"])
    assert result == 0
    mock_check.assert_not_called()


# -- _worktree_is_dirty (issue #667) -------------------------------------------


def test_worktree_is_dirty_returns_true_when_status_output(tmp_path: Path) -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="M  foo.py\n"),
    ):
        assert _worktree_is_dirty(tmp_path) is True


def test_worktree_is_dirty_returns_false_when_clean(tmp_path: Path) -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout=""),
    ):
        assert _worktree_is_dirty(tmp_path) is False


def test_worktree_is_dirty_returns_true_on_git_failure(tmp_path: Path) -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=128, stdout=""),
    ):
        assert _worktree_is_dirty(tmp_path) is True


# -- worktree porcelain helper (lookup tests live in test_worktrees.py) ------


def _porcelain(*records: tuple[str, str | None]) -> str:
    """Build a `git worktree list --porcelain` output from (path, branch)
    tuples. branch=None means a detached worktree.
    """
    lines: list[str] = []
    for path, branch in records:
        lines.append(f"worktree {path}")
        lines.append("HEAD 0123456789abcdef0123456789abcdef01234567")
        if branch is None:
            lines.append("detached")
        else:
            lines.append(f"branch refs/heads/{branch}")
        lines.append("")
    return "\n".join(lines)


def test_main_removes_worktree_before_deleting_branch(tmp_path: Path) -> None:
    """Issue #315: when a merged branch is checked out in a `.worktrees/`
    worktree, finalize must `git worktree remove` it before
    `git branch -D` — otherwise -D refuses to delete a checked-out
    branch and the whole finalize crashes.
    """
    _make_profile(tmp_path, "library-release")
    wt_dir = tmp_path / ".worktrees" / "issue-99-x"
    wt_dir.mkdir(parents=True)

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/99-x"]),
        # Discovery itself is covered in test_worktrees.py; here the lookup
        # is patched directly (the autouse fixture stubs list_worktrees).
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=wt_dir.resolve()),
        patch(_MOD + "._worktree_is_dirty", return_value=False),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    remove_call = ("worktree", "remove", str(wt_dir.resolve()))
    delete_call = ("branch", "-D", "feature/99-x")
    assert remove_call in git_run_calls
    assert delete_call in git_run_calls
    assert git_run_calls.index(remove_call) < git_run_calls.index(delete_call)


def test_main_skips_worktree_remove_when_branch_not_in_worktree(tmp_path: Path) -> None:
    """If `_worktree_for_branch` returns None, no worktree-remove call
    fires — only `branch -D`. Pins the existing path for branches that
    aren't checked out anywhere.
    """
    _make_profile(tmp_path, "library-release")
    porcelain = _porcelain((str(tmp_path), "develop"))

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/99-x"]),
        patch(_MOD + ".git.read_output", return_value=porcelain),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    assert ("branch", "-D", "feature/99-x") in git_run_calls
    assert not any(c[:1] == ("worktree",) for c in git_run_calls)


def test_main_skips_dirty_worktree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #667: a worktree with uncommitted changes is skipped entirely
    — neither the worktree nor the branch is removed, and finalize
    continues with the remaining branches.
    """
    _make_profile(tmp_path, "library-release")
    wt_dir = tmp_path / ".worktrees" / "issue-42-wip"
    wt_dir.mkdir(parents=True)

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/42-wip"]),
        # Discovery itself is covered in test_worktrees.py; here the lookup
        # is patched directly (the autouse fixture stubs list_worktrees).
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=wt_dir.resolve()),
        patch(_MOD + "._worktree_is_dirty", return_value=True),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    assert not any(c[0] == "worktree" for c in git_run_calls)
    assert not any(c == ("branch", "-D", "feature/42-wip") for c in git_run_calls)
    out = capsys.readouterr().out
    assert "Skipping feature/42-wip" in out
    assert "uncommitted changes" in out


def test_main_cleans_docker_cache_on_branch_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=["feature/x"]),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=2) as mock_clean,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0
    mock_clean.assert_called_once_with("feature/x")
    assert "Cleaned 2 cached container image(s)" in capsys.readouterr().out


# -- working-tree cleanliness gate (issue #472) -------------------------------


def test_main_fails_on_dirty_working_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #472: after cleanup, develop must be clean — any dirty state
    is a hard error so orphaned files don't silently accumulate.
    """
    _make_profile(tmp_path, "library-release")
    dirty_status = "?? orphan-spec.md\n?? stale-plan.md"
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.working_tree_status", return_value=dirty_status),
    ):
        result = main([])

    assert result == 1
    out = capsys.readouterr().out
    assert "working tree is not clean" in out
    assert "orphan-spec.md" in out
    assert "stale-plan.md" in out


def test_main_skips_dirty_check_on_dry_run(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.working_tree_status", return_value="?? should-not-fail.md"),
    ):
        result = main(["--dry-run"])
    assert result == 0


# -- merge + provenance (issue #1289) -----------------------------------------


def _clean() -> ProvenanceResult:
    return ProvenanceResult()


def _with_violation() -> ProvenanceResult:
    return ProvenanceResult(violations=[Action("a-vergil-audit", Role.AUDIT, "closed")])


def _with_advisory() -> ProvenanceResult:
    return ProvenanceResult(advisories=[Action("a-vergil-audit", Role.AUDIT, "approved")])


def test_pr_arg_runs_provenance_then_merges(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()) as mock_check,
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.pr_merge.wait_and_merge") as mock_merge,
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value=""),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._check_cd_workflow_status", return_value=None),
    ):
        result = main(["42"])
    assert result == 0
    mock_check.assert_called_once_with("42")
    mock_merge.assert_called_once()


def test_provenance_violation_aborts_without_merge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_with_violation()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
    ):
        result = main(["42"])
    assert result == 1
    mock_merge.assert_not_called()
    out = capsys.readouterr().out
    assert "provenance violation" in out.lower()
    assert "closed" in out


def test_provenance_violation_override_merges(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_with_violation()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.pr_merge.wait_and_merge") as mock_merge,
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value=""),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._check_cd_workflow_status", return_value=None),
    ):
        result = main(["42", "--allow-provenance-violation"])
    assert result == 0
    mock_merge.assert_called_once()


def test_advisory_surfaced_and_merge_proceeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_with_advisory()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.pr_merge.wait_and_merge") as mock_merge,
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value=""),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._check_cd_workflow_status", return_value=None),
    ):
        result = main(["42"])
    assert result == 0
    mock_merge.assert_called_once()
    assert "advisory" in capsys.readouterr().out.lower()


def test_already_merged_skips_merge(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()),
        patch(f"{_MOD}.github.pr_state", return_value="MERGED"),
        patch(f"{_MOD}.pr_merge.wait_and_merge") as mock_merge,
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value=""),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._check_cd_workflow_status", return_value=None),
    ):
        result = main(["42"])
    assert result == 0
    mock_merge.assert_not_called()


def test_pr_dry_run_skips_merge(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.pr_merge.wait_and_merge") as mock_merge,
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
    ):
        result = main(["42", "--dry-run"])
    assert result == 0
    mock_merge.assert_not_called()


def test_merge_abort_returns_one_from_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.pr_merge.wait_and_merge", side_effect=MergeAbortError("is a draft")),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.git.current_branch") as branch,
    ):
        result = main(["42"])
    assert result == 1
    branch.assert_not_called()  # fail_fast: cleanup never starts
    assert "is a draft" in capsys.readouterr().out


def test_no_pr_arg_is_cleanup_only(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr") as mock_check,
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0
    mock_check.assert_not_called()
    mock_merge.assert_not_called()


# -- PR inference and always-confirm (issue #1423) ----------------------------

_PR7 = {"number": "7", "url": "https://github.com/o/r/pull/7", "title": "Foo"}
_PR8 = {"number": "8", "url": "https://github.com/o/r/pull/8", "title": "Bar"}
_WT7 = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
_WT8 = Worktree(path=Path("/repo/.worktrees/issue-8-bar"), branch="feature/8-bar")


@contextmanager
def _cleanup_path_mocks(root: Path) -> Iterator[None]:
    """Neutralize the post-merge cleanup path for inference-focused tests.

    Keeps main() off real git/config/gh calls after the part under test.
    The worktree-arm sweep (issue #1552) classifies each inferred worktree,
    so gather_worktree_status is stubbed to a non-removable verdict here —
    these tests cover inference, not cleanup deletion.
    """
    with (
        patch(_MOD + ".git.repo_root", return_value=root),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(
            _MOD + ".worktrees.gather_worktree_status",
            return_value=WorktreeStatus(
                worktree=Worktree(path=root, branch="x"),
                state=WorktreeState.OPEN_PR,
                pr_number=None,
                ahead=0,
                dirty=False,
            ),
        ),
    ):
        yield


def test_no_arg_single_candidate_confirms_and_finalizes(tmp_path: Path) -> None:
    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + ".confirm", return_value=True) as confirm,
        patch(_MOD + "._stage_provenance") as prov,
        patch(_MOD + "._stage_merge"),
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    assert "PR #7" in confirm.call_args[0][0]
    assert prov.call_args[0][0].args.pr == "https://github.com/o/r/pull/7"


def test_yes_single_candidate_skips_tty_guard_and_finalizes(tmp_path: Path) -> None:
    """--yes pre-answers the single-PR confirm, so no interactive prompt is
    shown and the TTY guard is not required (issue #1644)."""
    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.require_tty") as guard,
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + "._stage_provenance") as prov,
        patch(_MOD + "._stage_merge"),
    ):
        rc = main(["--yes", "--dry-run"])
    assert rc == 0
    guard.assert_not_called()
    assert prov.call_args[0][0].args.pr == "https://github.com/o/r/pull/7"


def test_yes_multiple_candidates_still_disambiguates(tmp_path: Path) -> None:
    """--yes never auto-picks among PRs: the disambiguation menu is still
    shown, and only the post-choice confirm is pre-answered (issue #1644)."""

    def _pr_for(branch: str) -> dict[str, str]:
        return _PR7 if branch == "feature/7-foo" else _PR8

    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".github.pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".prompt_choice", return_value="PR #8 (feature/8-bar): Bar") as menu,
        patch(_MOD + "._stage_provenance") as prov,
        patch(_MOD + "._stage_merge"),
    ):
        rc = main(["--yes", "--dry-run"])
    assert rc == 0
    menu.assert_called_once()
    assert prov.call_args[0][0].args.pr == "https://github.com/o/r/pull/8"


def test_no_arg_single_candidate_decline_exits_zero_without_action() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + ".confirm", return_value=False),
        patch(_MOD + "._stage_provenance") as fin,
        patch(_MOD + ".git.current_branch") as branch,
    ):
        rc = main([])
    assert rc == 0
    fin.assert_not_called()
    branch.assert_not_called()  # cleanup never started


def test_no_arg_multiple_candidates_menu_then_confirm(tmp_path: Path) -> None:
    def _pr_for(branch: str) -> dict[str, str]:
        return _PR7 if branch == "feature/7-foo" else _PR8

    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".github.pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".prompt_choice", return_value="PR #8 (feature/8-bar): Bar") as menu,
        patch(_MOD + ".confirm", return_value=True),
        patch(_MOD + "._stage_provenance") as prov,
        patch(_MOD + "._stage_merge"),
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    menu.assert_called_once()
    assert prov.call_args[0][0].args.pr == "https://github.com/o/r/pull/8"


def test_no_arg_no_candidates_confirms_cleanup_only() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".confirm", return_value=False) as confirm,
    ):
        rc = main([])
    assert rc == 0
    assert "cleanup only" in confirm.call_args[0][0].lower()


def test_no_arg_excluded_worktree_prints_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No silent exclusions: a worktree without an open PR says why it
    was skipped while another worktree proceeds."""

    def _pr_for(branch: str) -> dict[str, str] | None:
        return _PR7 if branch == "feature/7-foo" else None

    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".github.pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".confirm", return_value=True),
        patch(_MOD + "._stage_provenance"),
        patch(_MOD + "._stage_merge"),
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "issue-8-bar: no open PR for feature/8-bar" in out


def test_no_arg_non_tty_fails_fast_before_prompting() -> None:
    """The TTY guard fires before any prompt — interactivity is a
    requirement of inference mode, not an accident of menu count."""
    with (
        patch(
            _MOD + ".worktrees.require_tty",
            side_effect=SystemExit("requires an interactive terminal"),
        ),
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + "._stage_provenance") as fin,
        pytest.raises(SystemExit, match="interactive terminal"),
    ):
        main([])
    confirm.assert_not_called()
    fin.assert_not_called()


def test_explicit_pr_skips_inference_and_prompts(tmp_path: Path) -> None:
    # list_worktrees is now also called by the cleanup sweep (issue #1552),
    # so it is no longer an inference-only signal; the no-prompt + provenance
    # assertions prove inference was skipped. Empty list → worktree arm no-op.
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + "._stage_provenance") as fin,
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        rc = main(["123", "--dry-run"])
    assert rc == 0
    confirm.assert_not_called()
    fin.assert_called_once()


# -- --cleanup-only: non-interactive release path (issue #1448) ----------------


def test_parse_args_cleanup_only_defaults_false() -> None:
    assert parse_args([]).cleanup_only is False


def test_parse_args_cleanup_only_flag() -> None:
    assert parse_args(["--cleanup-only"]).cleanup_only is True


def test_cleanup_only_rejects_pr_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PR argument and --cleanup-only contradict each other: the flag
    promises no merge and no prompts, the argument requests a merge."""
    with pytest.raises(SystemExit):
        parse_args(["123", "--cleanup-only"])
    err = capsys.readouterr().err
    assert "--cleanup-only" in err
    assert "not allowed with" in err


def test_cleanup_only_skips_inference_and_prompts(tmp_path: Path) -> None:
    """--cleanup-only is the scriptable release path: no TTY guard, no
    prompts, no merge — straight to cleanup. (The cleanup sweep itself
    enumerates worktrees; inference does not run.)"""
    with (
        patch(_MOD + ".worktrees.require_tty") as guard,
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + "._stage_provenance") as fin,
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
    ):
        rc = main(["--cleanup-only", "--dry-run"])
    assert rc == 0
    guard.assert_not_called()
    confirm.assert_not_called()
    fin.assert_not_called()


# -- engine swap + explicit-target cleanup (issue #1423) -----------------------

_CLEAN_PROVENANCE = ProvenanceResult(violations=[], advisories=[])


def test_explicit_target_cleanup_deletes_merged_pr_branch(tmp_path: Path) -> None:
    """After a squash merge, the just-merged branch is cleaned even though
    `git branch --merged` cannot see it."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),  # squash: sweep is blind
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        rc = main(["123"])
    assert rc == 0
    confirm.assert_not_called()  # explicit arg: no prompt
    assert ("branch", "-D", "feature/7-foo") in git_run_calls


def test_explicit_target_cleanup_respects_eternal_branches(tmp_path: Path) -> None:
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="develop"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls), (
        "must never delete an eternal branch"
    )


def test_explicit_target_cleanup_skips_missing_local_branch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A merged PR whose branch has no local counterpart is skipped loudly."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),  # no local branch
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert "no local branch" in capsys.readouterr().out


def test_explicit_target_cleanup_skips_dirty_worktree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dirty worktree blocks the explicit-target deletion, loudly."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(
            _MOD + ".worktrees.worktree_for_branch",
            return_value=Path("/repo/.worktrees/issue-7-foo"),
        ),
        patch(_MOD + "._worktree_is_dirty", return_value=True),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert "uncommitted changes" in capsys.readouterr().out


# -- ancestry-sweep guards (issue #1445) ----------------------------------------


def test_sweep_skips_zero_commit_branch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #1445: a branch whose tip equals the target's tip is the
    starting state of a freshly created issue worktree — the sweep must
    not delete the branch or remove its worktree, and must not even need
    a gh lookup to decide."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/1439-fresh"]),
        patch(_MOD + ".git.commit_sha", return_value="same-sha"),
        patch(_MOD + ".github.closed_pr_for_branch") as evidence,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert not any(c[:1] == ("worktree",) for c in git_run_calls)
    evidence.assert_not_called()  # guard 1 decides before the gh lookup
    out = capsys.readouterr().out
    assert "Skipping feature/1439-fresh" in out
    assert "zero-commit" in out


def test_sweep_skips_branch_without_merge_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #1445: an ancestor branch with no closed/merged PR may be a
    fresh branch created off an older target tip — without merge
    evidence the sweep leaves the branch and its worktree alone."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/77-stale-tip"]),
        patch(_MOD + ".git.commit_sha", side_effect=lambda ref: f"sha-of-{ref}"),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=None),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert not any(c[:1] == ("worktree",) for c in git_run_calls)
    out = capsys.readouterr().out
    assert "Skipping feature/77-stale-tip" in out
    assert "no closed or merged PR" in out


def test_sweep_deletes_straggler_with_commits_and_merge_evidence(tmp_path: Path) -> None:
    """A genuine straggler — own commits plus a closed/merged PR — is
    still swept; the guards relax the cleanup, they don't disable it."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/55-straggler"]),
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    assert ("branch", "-D", "feature/55-straggler") in git_run_calls


def test_explicit_target_cleanup_bypasses_sweep_guards(tmp_path: Path) -> None:
    """Issue #1445: the guards gate the ancestry sweep only — the
    just-merged PR branch keeps its existing explicit-target cleanup,
    which already has PR evidence by construction."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(_MOD + ".git.commit_sha") as sha,
        patch(_MOD + ".github.closed_pr_for_branch") as evidence,
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        rc = main(["123"])
    assert rc == 0
    assert ("branch", "-D", "feature/7-foo") in git_run_calls
    sha.assert_not_called()
    evidence.assert_not_called()


# -- stage functions (issue #1479) ---------------------------------------------


def _stage_ctx(argv: list[str], root: Path | None = None) -> FinalizeContext:
    return FinalizeContext(args=parse_args(argv), root=root or Path("/repo"))


def test_stage_provenance_clean_passes() -> None:
    ctx = _stage_ctx(["123"])
    with patch(_MOD + ".pr_provenance.check_pr", return_value=_clean()) as mock_check:
        _stage_provenance(ctx)
    mock_check.assert_called_once_with("123")


def test_stage_provenance_violation_raises(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_with_violation()),
        pytest.raises(FinalizeError, match="provenance"),
    ):
        _stage_provenance(ctx)
    err = capsys.readouterr().err
    assert "provenance violation" in err.lower()
    assert "closed" in err


def test_stage_provenance_override_passes() -> None:
    ctx = _stage_ctx(["123", "--allow-provenance-violation"])
    with patch(_MOD + ".pr_provenance.check_pr", return_value=_with_violation()):
        _stage_provenance(ctx)  # must not raise


def test_stage_provenance_advisory_surfaced(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _stage_ctx(["123"])
    with patch(_MOD + ".pr_provenance.check_pr", return_value=_with_advisory()):
        _stage_provenance(ctx)
    assert "advisory" in capsys.readouterr().err.lower()


def test_stage_merge_uses_wait_and_merge() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="feature/42-x"),
    ):
        _stage_merge(ctx)
    engine.assert_called_once_with("123", strategy="squash")
    assert ctx.merged_branch == "feature/42-x"


def test_stage_merge_abort_raises() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".github.head_ref", return_value="feature/42-x"),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbortError("is a draft")),
        pytest.raises(FinalizeError, match="is a draft"),
    ):
        _stage_merge(ctx)


def test_resolve_strategy_release_branch_merges() -> None:
    assert _resolve_strategy("release/2.1.30", None) == "merge"
    assert _resolve_strategy("release/post-2.1.30", None) == "merge"


def test_resolve_strategy_feature_branch_squashes() -> None:
    assert _resolve_strategy("feature/42-x", None) == "squash"
    assert _resolve_strategy("develop", None) == "squash"


def test_resolve_strategy_explicit_override_wins() -> None:
    assert _resolve_strategy("release/2.1.30", "squash") == "squash"
    assert _resolve_strategy("feature/42-x", "merge") == "merge"


def test_stage_merge_release_branch_uses_merge_commit() -> None:
    """A release/* branch with no explicit --strategy merges with a merge
    commit, preserving develop<->main ancestry (issue #1620)."""
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="release/post-2.1.30"),
    ):
        _stage_merge(ctx)
    engine.assert_called_once_with("123", strategy="merge")


def test_stage_merge_explicit_strategy_overrides_prefix() -> None:
    ctx = _stage_ctx(["123", "--strategy", "squash"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="release/2.1.30"),
    ):
        _stage_merge(ctx)
    engine.assert_called_once_with("123", strategy="squash")


def test_stage_merge_already_merged_skips_engine() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="feature/42-x"),
    ):
        _stage_merge(ctx)
    engine.assert_not_called()
    assert ctx.merged_branch == "feature/42-x"


def test_stage_merge_dry_run_skips_engine() -> None:
    ctx = _stage_ctx(["123", "--dry-run"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="feature/42-x"),
    ):
        _stage_merge(ctx)
    engine.assert_not_called()
    assert ctx.merged_branch == "feature/42-x"


def test_build_stages_with_pr() -> None:
    names = [s.name for s in build_stages(include_pr=True)]
    assert names == ["provenance", "merge", "cleanup", "validation", "cd-check"]


def test_build_stages_without_pr() -> None:
    names = [s.name for s in build_stages(include_pr=False)]
    assert names == ["cleanup", "validation", "cd-check"]


def test_build_stages_failure_modes() -> None:
    modes = {s.name: s.mode for s in build_stages(include_pr=True)}
    assert modes["provenance"] == "fail_fast"
    assert modes["merge"] == "fail_fast"
    assert modes["cleanup"] == "fail_fast"
    # fail_defer preserves current semantics: a validation failure still
    # runs the cd-check, and either failure exits 1.
    assert modes["validation"] == "fail_defer"
    assert modes["cd-check"] == "fail_defer"


def test_stage_validation_streams_through_progress(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + ".progress.run", return_value=0) as run:
        _stage_validation(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-container-run", "--", "vrg-validate")


def test_stage_validation_uses_uv_for_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + ".progress.run", return_value=0) as run:
        _stage_validation(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-container-run", "--", "uv", "run", "vrg-validate")


def test_stage_validation_failure_raises(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    err = subprocess.CalledProcessError(1, ("vrg-container-run",))
    with (
        patch(_MOD + ".progress.run", side_effect=err),
        pytest.raises(FinalizeError, match="validation failed"),
    ):
        _stage_validation(ctx)


def test_stage_validation_dry_run_skips(tmp_path: Path) -> None:
    ctx = _stage_ctx(["--dry-run"], root=tmp_path)
    with patch(_MOD + ".progress.run") as run:
        _stage_validation(ctx)
    run.assert_not_called()


def test_stage_cd_check_passes_when_clean(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + "._check_cd_workflow_status", return_value=None):
        _stage_cd_check(ctx)  # must not raise


def test_stage_cd_check_raises_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(
            _MOD + "._check_cd_workflow_status",
            return_value="CD workflow run 999 on develop (deadbee) ended with 'failure'.",
        ),
        pytest.raises(FinalizeError, match="CD workflow"),
    ):
        _stage_cd_check(ctx)
    assert "CD workflow" in capsys.readouterr().err


def test_stage_cd_check_dry_run_skips(tmp_path: Path) -> None:
    ctx = _stage_ctx(["--dry-run"], root=tmp_path)
    with patch(_MOD + "._check_cd_workflow_status") as check:
        _stage_cd_check(ctx)
    check.assert_not_called()


# -- squash-merge-aware straggler sweep (issue #1552) ------------------------


def test_sweep_removes_squash_merged_worktree_branch(tmp_path: Path) -> None:
    """A squash-merged branch is invisible to `git branch --merged`, so the
    worktree arm must sweep it via its classify_worktree removable verdict."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.MERGED, pr_number=1, ahead=2, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=removable),
        patch(_MOD + "._delete_branch_and_worktree", return_value=True) as deleter,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_any_call("feature/1-x", tmp_path, dry_run=False)


def test_sweep_keeps_open_pr_worktree(tmp_path: Path) -> None:
    """Race-safety (issue #1445): a worktree whose PR is still open is not
    removable, so the worktree arm never deletes live work. The same branch
    also appears in `git branch --merged`, exercising the ancestry arm's
    dedup against worktree branches (issue #1552)."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-2-y", branch="feature/2-y")
    not_removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.OPEN_PR, pr_number=2, ahead=2, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=["feature/2-y"]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=not_removable),
        patch(_MOD + ".github.closed_pr_for_branch") as ancestry_evidence,
        patch(_MOD + "._delete_branch_and_worktree") as deleter,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_not_called()
    # The ancestry arm skipped feature/2-y as a worktree branch before
    # reaching the merge-evidence guard.
    ancestry_evidence.assert_not_called()


def test_sweep_worktree_delete_decline_does_not_record(tmp_path: Path) -> None:
    """If _delete_branch_and_worktree declines (e.g. the tree went dirty
    between classification and deletion), the branch is not recorded as
    deleted and the sweep continues without error."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-3-z", branch="feature/3-z")
    removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.MERGED, pr_number=3, ahead=1, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=removable),
        patch(_MOD + "._delete_branch_and_worktree", return_value=False) as deleter,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_called_once_with("feature/3-z", tmp_path, dry_run=False)


def test_sweep_skips_eternal_branch_worktree(tmp_path: Path) -> None:
    """A worktree checked out on an eternal branch is never classified or
    swept — the eternal guard short-circuits before gather_worktree_status."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "eternal", branch="develop")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status") as gather,
        patch(_MOD + "._delete_branch_and_worktree") as deleter,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    gather.assert_not_called()
    deleter.assert_not_called()


# -- --release: chain into vrg-release after a clean finalize (issue #1634) ----


def test_parse_args_release_defaults_false() -> None:
    assert parse_args([]).release is False


def test_parse_args_release_flag() -> None:
    assert parse_args(["--release"]).release is True


def test_release_chains_into_vrg_release_on_success(tmp_path: Path) -> None:
    """A clean finalize hands off to vrg-release from the repo root."""
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        result = main(["--release"])
    assert result == 0
    mock_run.assert_called_once()
    call = mock_run.call_args
    assert call.args[0] == ("vrg-release",)
    assert call.kwargs["cwd"] == tmp_path


def test_release_skipped_when_finalize_fails(tmp_path: Path) -> None:
    """A non-zero pipeline must never trigger the release."""
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(
            _MOD + ".progress.run",
            side_effect=subprocess.CalledProcessError(1, ("vrg-container-run",)),
        ),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        result = main(["--release"])
    assert result == 1
    mock_run.assert_not_called()


def test_release_dry_run_notes_without_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        result = main(["--release", "--dry-run"])
    assert result == 0
    mock_run.assert_not_called()
    assert "vrg-release" in capsys.readouterr().out


def test_release_failure_reports_pr_unaffected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A release failure propagates its exit code and points the human at a
    re-run; the merge already happened and is unaffected."""
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 2
        result = main(["--release"])
    assert result == 2
    err = capsys.readouterr().err
    assert "release failed" in err
    assert "vrg-release" in err


def test_no_release_flag_does_not_invoke_release(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        result = main([])
    assert result == 0
    mock_run.assert_not_called()


# -- --install: extend the cascade through vrg-release --install (issue #1643) --


def test_parse_args_install_defaults_false() -> None:
    assert parse_args([]).install is False


def test_parse_args_install_flag() -> None:
    assert parse_args(["--install"]).install is True


def test_install_chains_into_vrg_release_install_on_success(tmp_path: Path) -> None:
    """--install implies --release and hands off to `vrg-release --install`."""
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        result = main(["--install"])
    assert result == 0
    mock_run.assert_called_once()
    call = mock_run.call_args
    assert call.args[0] == ("vrg-release", "--install")
    assert call.kwargs["cwd"] == tmp_path


def test_install_dry_run_notes_release_install_without_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        result = main(["--install", "--dry-run"])
    assert result == 0
    mock_run.assert_not_called()
    assert "vrg-release --install" in capsys.readouterr().out


def test_install_failure_points_at_release_install_rerun(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 2
        result = main(["--install"])
    assert result == 2
    assert "vrg-release --install" in capsys.readouterr().err


def test_skip_post_checks_drops_validation_and_cd() -> None:
    names = {s.name for s in build_stages(include_pr=True, include_post_checks=False)}
    assert "validation" not in names
    assert "cd-check" not in names
    assert {"provenance", "merge", "cleanup"} <= names


def test_default_keeps_post_checks() -> None:
    names = {s.name for s in build_stages(include_pr=True, include_post_checks=True)}
    assert {"validation", "cd-check"} <= names


def test_skip_post_checks_flag_parses() -> None:
    assert parse_args(["123", "--skip-post-checks"]).skip_post_checks is True


_FIN_MOD = "vergil_tooling.bin.vrg_finalize_pr"


def test_parse_pr_list_splits_and_trims() -> None:
    assert _parse_pr_list("123, 124 ,125") == ["123", "124", "125"]
    assert _parse_pr_list("123") == ["123"]


def test_finalize_batch_runs_each_item_then_release_once() -> None:
    runs: list[tuple[str, ...]] = []

    def fake_run(cmd, **_kwargs):
        runs.append(tuple(cmd))
        return MagicMock(returncode=0)

    with (
        patch(_FIN_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_FIN_MOD + ".confirm", return_value=True),
    ):
        rc = _run_finalize_batch(
            ["123", "124"],
            root=Path("/repo"),
            release=True,
            install=False,
            assume_yes=True,
        )
    assert rc == 0
    assert ("vrg-finalize-pr", "123", "--skip-post-checks") in runs
    assert ("vrg-finalize-pr", "124", "--skip-post-checks") in runs
    assert ("vrg-finalize-pr", "--cleanup-only") in runs
    assert ("vrg-release",) in runs


def test_finalize_batch_stops_on_item_failure_no_release() -> None:
    def fake_run(cmd, **_kwargs):
        rc = 1 if "123" in cmd else 0
        return MagicMock(returncode=rc)

    with (
        patch(_FIN_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_FIN_MOD + ".confirm", return_value=True),
    ):
        rc = _run_finalize_batch(
            ["123", "124"],
            root=Path("/repo"),
            release=True,
            install=False,
            assume_yes=True,
        )
    assert rc == 1


def test_resolve_open_prs_skips_worktrees_without_pr() -> None:
    wts = [
        Worktree(path=Path("/r/.worktrees/issue-2-b"), branch="feature/2-b"),
        Worktree(path=Path("/r/.worktrees/issue-1-a"), branch="feature/1-a"),
    ]

    def pr_for(branch: str):
        return {"url": f"https://x/{branch}"} if branch == "feature/1-a" else None

    with (
        patch(_FIN_MOD + ".worktrees.list_worktrees", return_value=wts),
        patch(_FIN_MOD + ".github.pr_for_branch", side_effect=pr_for),
    ):
        # branch-sorted: feature/1-a precedes feature/2-b; only 1-a has a PR
        assert _resolve_open_prs(Path("/r")) == ["https://x/feature/1-a"]


def test_finalize_batch_validation_failure_reported() -> None:
    def fake_run(cmd, **_kwargs):
        rc = 1 if tuple(cmd)[:2] == ("vrg-finalize-pr", "--cleanup-only") else 0
        return MagicMock(returncode=rc)

    with (
        patch(_FIN_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_FIN_MOD + ".confirm", return_value=True),
    ):
        rc = _run_finalize_batch(
            ["123"], root=Path("/repo"), release=False, install=False, assume_yes=True
        )
    assert rc == 1


def test_finalize_batch_release_failure_reported() -> None:
    def fake_run(cmd, **_kwargs):
        rc = 1 if tuple(cmd)[0] == "vrg-release" else 0
        return MagicMock(returncode=rc)

    with (
        patch(_FIN_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_FIN_MOD + ".confirm", return_value=True),
    ):
        rc = _run_finalize_batch(
            ["123"], root=Path("/repo"), release=True, install=False, assume_yes=True
        )
    assert rc == 1


def test_main_all_routes_to_batch() -> None:
    with (
        patch(_FIN_MOD + ".git.is_main_worktree", return_value=True),
        patch(_FIN_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_FIN_MOD + "._resolve_open_prs", return_value=["u1", "u2"]),
        patch(_FIN_MOD + "._run_finalize_batch", return_value=0) as run_batch,
    ):
        rc = main(["--all"])
    assert rc == 0
    run_batch.assert_called_once()


def test_main_all_no_open_prs_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(_FIN_MOD + ".git.is_main_worktree", return_value=True),
        patch(_FIN_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_FIN_MOD + "._resolve_open_prs", return_value=[]),
    ):
        rc = main(["--all"])
    assert rc == 0
    assert "no open PRs" in capsys.readouterr().out


def test_main_comma_list_routes_to_batch() -> None:
    with (
        patch(_FIN_MOD + ".git.is_main_worktree", return_value=True),
        patch(_FIN_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_FIN_MOD + "._run_finalize_batch", return_value=0) as run_batch,
    ):
        rc = main(["123,124"])
    assert rc == 0
    run_batch.assert_called_once()
