"""Tests for vergil_tooling.bin.vrg_finalize_pr."""

from __future__ import annotations

import json
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from vergil_tooling.bin.vrg_finalize_pr import (
    _check_cd_workflow_status,
    _worktree_is_dirty,
    main,
    parse_args,
)
from vergil_tooling.lib.pr_provenance import Action, ProvenanceResult, Role

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


def test_main_library_release(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
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
    porcelain = _porcelain(
        (str(tmp_path), "develop"),
        (str(wt_dir), "feature/99-x"),
    )

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/99-x"]),
        patch(_MOD + ".git.read_output", return_value=porcelain),
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
    porcelain = _porcelain(
        (str(tmp_path), "develop"),
        (str(wt_dir), "feature/42-wip"),
    )

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/42-wip"]),
        patch(_MOD + ".git.read_output", return_value=porcelain),
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
        result = main(["42", "--allow-provenance-violation"])
    assert result == 0
    mock_merge.assert_called_once()


def test_advisory_surfaced_and_merge_proceeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_with_advisory()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
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
        result = main(["42"])
    assert result == 0
    mock_merge.assert_called_once()
    assert "advisory" in capsys.readouterr().err.lower()


def test_already_merged_skips_merge(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()),
        patch(f"{_MOD}.github.pr_state", return_value="MERGED"),
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
        result = main(["42"])
    assert result == 0
    mock_merge.assert_not_called()


def test_pr_dry_run_skips_merge(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
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
