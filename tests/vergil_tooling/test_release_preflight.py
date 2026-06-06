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
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
    ):
        ctx = preflight(version_override=None, repo_root=root)
    assert ctx.repo == "owner/repo"
    assert ctx.version == "2.1.0"
    assert ctx.version_override is None


def test_preflight_with_minor_override() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.0.34"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
    ):
        ctx = preflight(version_override="minor", repo_root=root)
    assert ctx.version == "2.1.0"
    assert ctx.version_override == "minor"


def test_preflight_with_major_override() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.0.34"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
    ):
        ctx = preflight(version_override="major", repo_root=root)
    assert ctx.version == "3.0.0"
    assert ctx.version_override == "major"


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
                stdout="non-compliant",
                stderr="",
            ),
        ),
        pytest.raises(ReleaseError, match="non-compliant"),
    ):
        audit_repo_config("owner/repo")


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
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config") as mock_audit,
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
    ):
        ctx = preflight(version_override=None, repo_root=root)
    assert ctx.repo == "owner/repo"
    mock_audit.assert_not_called()


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
