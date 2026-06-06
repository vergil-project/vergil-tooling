"""Tests for vergil_tooling.bin.vrg_finalize_pr."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_finalize_pr import (
    _check_cd_workflow_status,
    _finalize_specific_pr,
    _worktree_is_dirty,
    main,
    parse_args,
)
from vergil_tooling.lib.pr_merge import MergeAbortError
from vergil_tooling.lib.pr_provenance import Action, ProvenanceResult, Role
from vergil_tooling.lib.worktrees import Worktree

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
        patch(_MOD + ".prompt_yes_no", return_value=True),
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


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.target_branch == "develop"
    assert args.dry_run is False


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


def _validation_ok() -> CompletedProcess[bytes]:
    return CompletedProcess(args=("vrg-validate",), returncode=0)


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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0
    mock_run.assert_any_call("checkout", "develop")
    mock_run.assert_any_call("branch", "-D", "feature/x")


def test_main_already_on_target(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
            "vergil_tooling.bin.vrg_finalize_pr.subprocess.run",
            return_value=CompletedProcess(args=("vrg-validate",), returncode=1),
        ),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 1


def test_main_calls_docker_run(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()) as mock_sub,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0
    cmd = mock_sub.call_args[0][0]
    assert cmd[0] == "vrg-container-run"
    assert cmd[1:] == ("--", "vrg-validate")


def test_main_container_run_uses_uv_for_python(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()) as mock_sub,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])
    assert result == 0
    cmd = mock_sub.call_args[0][0]
    assert cmd == ("vrg-container-run", "--", "uv", "run", "vrg-validate")


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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
        patch(
            _MOD + "._check_cd_workflow_status",
            return_value=(
                "CD workflow run 999 on develop (deadbee) ended with conclusion 'failure'."
            ),
        ),
    ):
        result = main([])
    assert result == 1
    stderr = capsys.readouterr().err
    assert "CD workflow" in stderr


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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
    stderr = capsys.readouterr().err
    assert "working tree is not clean" in stderr
    assert "orphan-spec.md" in stderr
    assert "stale-plan.md" in stderr


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
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
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
    err = capsys.readouterr().err
    assert "provenance violation" in err.lower()
    assert "closed" in err


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
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
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
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
        result = main(["42"])
    assert result == 0
    mock_merge.assert_called_once()
    assert "advisory" in capsys.readouterr().err.lower()


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
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
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
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
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
def _cleanup_path_mocks() -> Iterator[None]:
    """Neutralize the post-merge cleanup path for inference-focused tests.

    Keeps main() off real git/config/gh calls after the part under test.
    """
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        yield


def test_no_arg_single_candidate_confirms_and_finalizes() -> None:
    with (
        _cleanup_path_mocks(),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + ".prompt_yes_no", return_value=True) as confirm,
        patch(_MOD + "._finalize_specific_pr", return_value=0) as fin,
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    assert "PR #7" in confirm.call_args[0][0]
    assert fin.call_args[0][0].pr == "https://github.com/o/r/pull/7"


def test_no_arg_single_candidate_decline_exits_zero_without_action() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + ".prompt_yes_no", return_value=False),
        patch(_MOD + "._finalize_specific_pr") as fin,
        patch(_MOD + ".git.current_branch") as branch,
    ):
        rc = main([])
    assert rc == 0
    fin.assert_not_called()
    branch.assert_not_called()  # cleanup never started


def test_no_arg_multiple_candidates_menu_then_confirm() -> None:
    def _pr_for(branch: str) -> dict[str, str]:
        return _PR7 if branch == "feature/7-foo" else _PR8

    with (
        _cleanup_path_mocks(),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".github.pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".prompt_choice", return_value="PR #8 (feature/8-bar): Bar") as menu,
        patch(_MOD + ".prompt_yes_no", return_value=True),
        patch(_MOD + "._finalize_specific_pr", return_value=0) as fin,
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    menu.assert_called_once()
    assert fin.call_args[0][0].pr == "https://github.com/o/r/pull/8"


def test_no_arg_no_candidates_confirms_cleanup_only() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".prompt_yes_no", return_value=False) as confirm,
    ):
        rc = main([])
    assert rc == 0
    assert "cleanup only" in confirm.call_args[0][0].lower()


def test_no_arg_excluded_worktree_prints_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No silent exclusions: a worktree without an open PR says why it
    was skipped while another worktree proceeds."""

    def _pr_for(branch: str) -> dict[str, str] | None:
        return _PR7 if branch == "feature/7-foo" else None

    with (
        _cleanup_path_mocks(),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".github.pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".prompt_yes_no", return_value=True),
        patch(_MOD + "._finalize_specific_pr", return_value=0),
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
        patch(_MOD + ".prompt_yes_no") as confirm,
        patch(_MOD + "._finalize_specific_pr") as fin,
        pytest.raises(SystemExit, match="interactive terminal"),
    ):
        main([])
    confirm.assert_not_called()
    fin.assert_not_called()


def test_explicit_pr_skips_inference_and_prompts() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees") as listing,
        patch(_MOD + ".prompt_yes_no") as confirm,
        patch(_MOD + "._finalize_specific_pr", return_value=0) as fin,
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        rc = main(["123", "--dry-run"])
    assert rc == 0
    listing.assert_not_called()
    confirm.assert_not_called()
    fin.assert_called_once()


# -- engine swap + explicit-target cleanup (issue #1423) -----------------------

_CLEAN_PROVENANCE = ProvenanceResult(violations=[], advisories=[])


def test_finalize_specific_pr_uses_wait_and_merge() -> None:
    args = parse_args(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
    ):
        rc = _finalize_specific_pr(args)
    assert rc == 0
    engine.assert_called_once_with("123", strategy="squash")


def test_finalize_specific_pr_merge_abort_returns_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = parse_args(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbortError("is a draft")),
    ):
        rc = _finalize_specific_pr(args)
    assert rc == 1
    assert "is a draft" in capsys.readouterr().err


def test_finalize_specific_pr_already_merged_skips_engine() -> None:
    args = parse_args(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
    ):
        rc = _finalize_specific_pr(args)
    assert rc == 0
    engine.assert_not_called()


def test_explicit_target_cleanup_deletes_merged_pr_branch() -> None:
    """After a squash merge, the just-merged branch is cleaned even though
    `git branch --merged` cannot see it."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".prompt_yes_no") as confirm,
        patch(_MOD + "._finalize_specific_pr", return_value=0),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),  # squash: sweep is blind
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
    ):
        rc = main(["123"])
    assert rc == 0
    confirm.assert_not_called()  # explicit arg: no prompt
    assert ("branch", "-D", "feature/7-foo") in git_run_calls


def test_explicit_target_cleanup_respects_eternal_branches() -> None:
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + "._finalize_specific_pr", return_value=0),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".github.head_ref", return_value="develop"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls), (
        "must never delete an eternal branch"
    )


def test_explicit_target_cleanup_skips_missing_local_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A merged PR whose branch has no local counterpart is skipped loudly."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + "._finalize_specific_pr", return_value=0),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),  # no local branch
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert "no local branch" in capsys.readouterr().out


def test_explicit_target_cleanup_skips_dirty_worktree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dirty worktree blocks the explicit-target deletion, loudly."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + "._finalize_specific_pr", return_value=0),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main([])

    assert result == 0
    assert ("branch", "-D", "feature/55-straggler") in git_run_calls


def test_explicit_target_cleanup_bypasses_sweep_guards() -> None:
    """Issue #1445: the guards gate the ancestry sweep only — the
    just-merged PR branch keeps its existing explicit-target cleanup,
    which already has PR evidence by construction."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + "._finalize_specific_pr", return_value=0),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
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
        patch(_MOD + ".subprocess.run", return_value=_validation_ok()),
    ):
        rc = main(["123"])
    assert rc == 0
    assert ("branch", "-D", "feature/7-foo") in git_run_calls
    sha.assert_not_called()
    evidence.assert_not_called()
