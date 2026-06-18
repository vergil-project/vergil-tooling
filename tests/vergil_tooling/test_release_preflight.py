from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release import preflight as preflight_module
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.preflight import (
    _compute_release_version,
    preflight,
)

_MOD = "vergil_tooling.lib.release.preflight"


def test_preflight_success() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt) as m_wt,
        patch(_MOD + ".os.chdir") as m_chdir,
    ):
        ctx = preflight(version_override=None, repo_root=root)
    assert ctx.repo == "owner/repo"
    assert ctx.version == "2.1.0"
    assert ctx.version_override is None
    assert ctx.release_branch == "release/2.1.0"
    assert ctx.worktree_path == wt
    m_wt.assert_called_once_with(root, "release/2.1.0", resume=False)
    m_chdir.assert_called_once_with(wt)


def test_preflight_resume_skips_fresh_checks_and_adopts() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged") as m_tagged,
        patch(_MOD + "._check_no_existing_tracking_issue") as m_issue,
        patch(_MOD + "._acquire_release_worktree", return_value=wt) as m_wt,
        patch(_MOD + ".os.chdir"),
    ):
        preflight(version_override=None, repo_root=root, resume=True)
    m_tagged.assert_not_called()
    m_issue.assert_not_called()
    m_wt.assert_called_once_with(root, "release/2.1.0", resume=True)


def test_preflight_resume_uses_resume_version_and_sets_issue() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-2.3.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".version.show") as m_show,
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
    ):
        ctx = preflight(
            version_override=None,
            repo_root=root,
            resume=True,
            resume_version="2.3.0",
            resume_issue_number=42,
        )
    assert ctx.version == "2.3.0"
    m_show.assert_not_called()  # resume_version bypasses the develop VERSION file
    assert ctx.issue_number == 42
    assert ctx.issue_url == "https://github.com/owner/repo/issues/42"


def test_preflight_with_minor_override() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.0.34"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
    ):
        ctx = preflight(version_override="minor", repo_root=root)
    assert ctx.version == "2.1.0"
    assert ctx.version_override == "minor"
    assert ctx.release_branch == "release/2.1.0"


def test_preflight_with_major_override() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-3.0.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.0.34"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
    ):
        ctx = preflight(version_override="major", repo_root=root)
    assert ctx.version == "3.0.0"
    assert ctx.version_override == "major"
    assert ctx.release_branch == "release/3.0.0"


def test_preflight_wraps_version_error() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(
            _MOD + ".version.show",
            side_effect=FileNotFoundError("VERSION file not found"),
        ),
        pytest.raises(ReleaseError, match="VERSION file not found"),
    ):
        preflight(version_override=None, repo_root=root)


def test_compute_release_version_minor() -> None:
    assert _compute_release_version("2.0.34", "minor") == "2.1.0"


def test_compute_release_version_major() -> None:
    assert _compute_release_version("2.0.34", "major") == "3.0.0"


def test_check_host_prerequisites_fails() -> None:
    from vergil_tooling.lib.release.preflight import _check_host_prerequisites

    with (
        patch("shutil.which", return_value=None),
        pytest.raises(ReleaseError, match="git-cliff"),
    ):
        _check_host_prerequisites()


def testcheck_gh_auth_fails() -> None:
    from vergil_tooling.lib.release.preflight import check_gh_auth

    with (
        patch(_MOD + ".github.read_output", side_effect=Exception("auth failed")),
        pytest.raises(ReleaseError, match="authentication failed"),
    ):
        check_gh_auth()


def test_check_branch_wrong_branch() -> None:
    from vergil_tooling.lib.release.preflight import _check_branch_and_tree

    with (
        patch(_MOD + ".git.current_branch", return_value="main"),
        pytest.raises(ReleaseError, match="Must be on develop"),
    ):
        _check_branch_and_tree()


def test_check_branch_dirty_tree() -> None:
    from vergil_tooling.lib.release.preflight import _check_branch_and_tree

    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", return_value="M file.py"),
        pytest.raises(ReleaseError, match="not clean"),
    ):
        _check_branch_and_tree()


def test_check_branch_not_synced() -> None:
    from vergil_tooling.lib.release.preflight import _check_branch_and_tree

    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", side_effect=["", "abc1234", "def5678"]),
        patch(_MOD + ".git.run"),
        pytest.raises(ReleaseError, match="does not match"),
    ):
        _check_branch_and_tree()


def testaudit_repo_config_fails() -> None:
    from subprocess import CompletedProcess

    from vergil_tooling.lib.release.preflight import audit_repo_config

    with (
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(
                args=(),
                returncode=1,
                stdout="  o/r: NON-COMPLIANT (1 issues)",
                stderr="",
            ),
        ),
        pytest.raises(ReleaseError, match="non-compliant") as exc_info,
    ):
        audit_repo_config("owner/repo")
    # The captured audit output is carried as detail so it can be surfaced.
    assert "NON-COMPLIANT" in (exc_info.value.detail or "")


def testaudit_repo_config_distinguishes_crash_from_noncompliance() -> None:
    """A non-1 exit means the audit could not complete (crash / auth / API),
    not that the repo is non-compliant — the headline must say so, and the
    captured output must ride along as detail (issue #1691)."""
    from subprocess import CompletedProcess

    from vergil_tooling.lib.release.preflight import audit_repo_config

    with (
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(
                args=(),
                returncode=2,
                stdout="",
                stderr="gh: Resource not accessible by integration (HTTP 403)",
            ),
        ),
        pytest.raises(ReleaseError, match="could not complete") as exc_info,
    ):
        audit_repo_config("owner/repo")
    assert "403" in (exc_info.value.detail or "")


def test_check_version_not_tagged_passes() -> None:
    from vergil_tooling.lib.release.preflight import _check_version_not_tagged

    with patch(_MOD + ".git.read_output", return_value="v2.0.33"):
        _check_version_not_tagged("2.0.34")


def test_check_version_not_tagged_fails() -> None:
    from vergil_tooling.lib.release.preflight import _check_version_not_tagged

    with (
        patch(_MOD + ".git.read_output", return_value="v2.0.34"),
        pytest.raises(ReleaseError, match="already tagged"),
    ):
        _check_version_not_tagged("2.0.34")


def test_check_version_not_tagged_no_tags() -> None:
    from vergil_tooling.lib.release.preflight import _check_version_not_tagged

    with patch(
        _MOD + ".git.read_output",
        side_effect=subprocess.CalledProcessError(128, "git describe"),
    ):
        _check_version_not_tagged("2.0.34")


def test_check_no_existing_tracking_issue_passes() -> None:
    from vergil_tooling.lib.release.preflight import _check_no_existing_tracking_issue

    with patch(_MOD + ".find_existing_tracking_issue", return_value=None):
        _check_no_existing_tracking_issue("owner/repo", "2.0.34")


def test_check_no_existing_tracking_issue_fails() -> None:
    from vergil_tooling.lib.release.preflight import _check_no_existing_tracking_issue

    with (
        patch(
            _MOD + ".find_existing_tracking_issue",
            return_value="https://github.com/owner/repo/issues/99",
        ),
        pytest.raises(ReleaseError, match="already exists"),
    ):
        _check_no_existing_tracking_issue("owner/repo", "2.0.34")


def test_check_host_prerequisites_success() -> None:
    from vergil_tooling.lib.release.preflight import _check_host_prerequisites

    with patch("shutil.which", return_value="/usr/bin/git-cliff"):
        _check_host_prerequisites()


def testcheck_gh_auth_success() -> None:
    from vergil_tooling.lib.release.preflight import check_gh_auth

    with patch(_MOD + ".github.read_output", return_value="owner/repo"):
        assert check_gh_auth() == "owner/repo"


def test_read_and_validate_config(tmp_path: Path) -> None:
    from vergil_tooling.lib.release.preflight import _read_and_validate_config

    (tmp_path / "vergil.toml").write_text(
        "[project]\n"
        'repository-type = "library"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        'primary-language = "python"\n'
        "[publish]\n"
        "release = true\n"
        "docs = true\n"
        "[ci]\n"
        'versions = ["3.12"]\n'
        "[dependencies]\n"
        'vergil = "v2.0"\n'
    )
    _read_and_validate_config(tmp_path)


def test_check_branch_and_tree_success() -> None:
    from vergil_tooling.lib.release.preflight import _check_branch_and_tree

    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", side_effect=["", "abc1234", "abc1234"]),
        patch(_MOD + ".git.run"),
    ):
        _check_branch_and_tree()


def testaudit_repo_config_success() -> None:
    from subprocess import CompletedProcess

    from vergil_tooling.lib.release.preflight import audit_repo_config

    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="", stderr=""),
    ):
        audit_repo_config("owner/repo")


def test_preflight_never_audits() -> None:
    """The audit is its own pipeline stage now — preflight never runs it."""
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config") as mock_audit,
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
    ):
        ctx = preflight(version_override=None, repo_root=root)
    assert ctx.repo == "owner/repo"
    mock_audit.assert_not_called()


def test_acquire_release_worktree_creates_off_develop() -> None:
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    root = Path("/tmp/repo")  # noqa: S108
    wt = root / ".worktrees" / "release-2.1.0"
    with (
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".create_worktree", return_value=wt) as m_create,
    ):
        result = _acquire_release_worktree(root, "release/2.1.0", resume=False)
    assert result == wt
    m_create.assert_called_once_with(root, branch="release/2.1.0", base="develop")


def test_acquire_release_worktree_adopts_existing_local_branch_on_resume() -> None:
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    root = Path("/tmp/repo")  # noqa: S108
    wt = root / ".worktrees" / "release-2.1.0"
    with (
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".adopt_worktree", return_value=wt) as m_adopt,
        patch(_MOD + ".create_worktree") as m_create,
    ):
        result = _acquire_release_worktree(root, "release/2.1.0", resume=True)
    assert result == wt
    m_adopt.assert_called_once_with(root, branch="release/2.1.0")
    m_create.assert_not_called()
    m_run.assert_not_called()


def test_acquire_release_worktree_creates_local_branch_from_origin_on_resume() -> None:
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    root = Path("/tmp/repo")  # noqa: S108
    wt = root / ".worktrees" / "release-2.1.0"
    with (
        patch(_MOD + ".git.ref_exists", side_effect=[False, True, False]),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".adopt_worktree", return_value=wt),
    ):
        result = _acquire_release_worktree(root, "release/2.1.0", resume=True)
    assert result == wt
    m_run.assert_called_once_with("branch", "release/2.1.0", "origin/release/2.1.0")


def test_acquire_release_worktree_fails_if_branch_exists() -> None:
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    with (
        patch(_MOD + ".git.ref_exists", return_value=True),
        pytest.raises(ReleaseError, match="already exists"),
    ):
        _acquire_release_worktree(Path("/tmp/repo"), "release/2.1.0", resume=False)  # noqa: S108


def test_acquire_release_worktree_wraps_worktree_error() -> None:
    from vergil_tooling.lib.managed_worktree import ManagedWorktreeError
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    with (
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(
            _MOD + ".create_worktree",
            side_effect=ManagedWorktreeError("path already exists"),
        ),
        pytest.raises(ReleaseError, match="path already exists"),
    ):
        _acquire_release_worktree(Path("/tmp/repo"), "release/2.1.0", resume=False)  # noqa: S108


def test_run_audit_resolves_repo_and_audits() -> None:
    with (
        patch(_MOD + ".check_gh_auth", return_value="owner/repo") as m_auth,
        patch(_MOD + ".audit_repo_config") as m_audit,
    ):
        preflight_module.run_audit()
    m_auth.assert_called_once_with()
    m_audit.assert_called_once_with("owner/repo")


def test_preflight_wraps_version_sync_error() -> None:
    from vergil_tooling.lib.version import VersionSyncError

    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(
            _MOD + ".version.show",
            side_effect=VersionSyncError("VERSION", "1.0.0", {"pyproject.toml": "1.0.1"}),
        ),
        pytest.raises(ReleaseError, match="VERSION"),
    ):
        preflight(version_override=None, repo_root=root)
