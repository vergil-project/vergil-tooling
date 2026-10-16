from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release import preflight as preflight_module
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.preflight import (
    PendingDepsPr,
    _compute_release_version,
    fix_task_status,
    pending_deps_prs,
    preflight,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

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
        patch(_MOD + ".pending_deps_prs", return_value=[]),
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
        patch(_MOD + ".pending_deps_prs", return_value=[]),
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
        patch(_MOD + ".pending_deps_prs", return_value=[]),
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
        patch(_MOD + ".pending_deps_prs", return_value=[]),
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


# --- dependency-update release gate (epic vergil-project/.github#381, spec §5.3) ---

_PRS = [
    {
        "url": "https://github.com/o/r/pull/9",
        "body": "Mechanized update.\n\nFix task: https://github.com/o/r/issues/10\n",
    },
    {"url": "https://github.com/o/r/pull/11", "body": "no fix task yet"},
    {"url": "https://github.com/o/r/pull/12", "body": None},
]


def test_pending_deps_prs_parses_fix_task() -> None:
    with patch(_MOD + ".github.read_json", return_value=_PRS) as m:
        prs = pending_deps_prs("o/r")
    assert prs == [
        PendingDepsPr(
            url="https://github.com/o/r/pull/9",
            fix_task="https://github.com/o/r/issues/10",
        ),
        PendingDepsPr(url="https://github.com/o/r/pull/11", fix_task=None),
        PendingDepsPr(url="https://github.com/o/r/pull/12", fix_task=None),
    ]
    args = list(m.call_args.args)
    assert args[:2] == ["pr", "list"]
    assert args[args.index("--repo") + 1] == "o/r"
    assert args[args.index("--state") + 1] == "open"
    assert args[args.index("--label") + 1] == "dependency-update"


def test_pending_deps_prs_fails_loud_on_gh_error() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 502")
    with (
        patch(_MOD + ".github.read_json", side_effect=err),
        pytest.raises(ReleaseError, match="Could not list open dependency-update PRs") as info,
    ):
        pending_deps_prs("o/r")
    assert info.value.phase == "preflight"


def test_pending_deps_prs_fails_loud_on_bad_json() -> None:
    with (
        patch(_MOD + ".github.read_json", side_effect=json.JSONDecodeError("x", "doc", 0)),
        pytest.raises(ReleaseError, match="Could not list open dependency-update PRs"),
    ):
        pending_deps_prs("o/r")


def test_pending_deps_prs_rejects_non_list() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value={"url": "x"}),
        pytest.raises(ReleaseError, match="expected a list"),
    ):
        pending_deps_prs("o/r")


def test_pending_deps_prs_rejects_entry_without_url() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[{"body": "x"}]),
        pytest.raises(ReleaseError, match="without a url"),
    ):
        pending_deps_prs("o/r")


def test_pending_deps_prs_rejects_non_object_entry() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=["https://github.com/o/r/pull/9"]),
        pytest.raises(ReleaseError, match="without a url"),
    ):
        pending_deps_prs("o/r")


def _fresh_patches(pending: list[PendingDepsPr]) -> list[contextlib.AbstractContextManager[object]]:
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    return [
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="o/r"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".pending_deps_prs", return_value=pending),
    ]


def test_preflight_refuses_on_pending_deps_pr() -> None:
    pending = [
        PendingDepsPr(
            url="https://github.com/o/r/pull/9",
            fix_task="https://github.com/o/r/issues/10",
        ),
        PendingDepsPr(url="https://github.com/o/r/pull/11", fix_task=None),
    ]
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches(pending):
            stack.enter_context(p)
        stack.enter_context(patch(_MOD + ".fix_task_status", return_value=("OPEN", None)))
        m_run = stack.enter_context(patch(_MOD + ".github.run"))
        m_wt = stack.enter_context(patch(_MOD + "._acquire_release_worktree"))
        with pytest.raises(ReleaseError, match="dependency-update") as info:
            preflight(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    msg = str(info.value)
    assert "https://github.com/o/r/pull/9 (fix task https://github.com/o/r/issues/10 is open" in msg
    assert "https://github.com/o/r/pull/11 (no fix task linked" in msg
    assert "--allow-pending-deps" in msg
    assert info.value.phase == "preflight"
    # The gate refuses before the release branch/worktree (the release lock) exists.
    m_wt.assert_not_called()
    # An open fix task and a missing fix-task line both keep blocking: nothing closed.
    m_run.assert_not_called()


def test_preflight_allow_pending_deps_records_bypass(capsys: pytest.CaptureFixture[str]) -> None:
    pending = [PendingDepsPr(url="https://github.com/o/r/pull/9", fix_task=None)]
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches(pending):
            stack.enter_context(p)
        ctx = preflight(
            version_override=None,
            repo_root=Path("/tmp/repo"),  # noqa: S108
            allow_pending_deps=True,
        )
    assert ctx.bypassed_deps_prs == ["https://github.com/o/r/pull/9"]
    out = capsys.readouterr().out
    assert "--allow-pending-deps" in out
    assert "https://github.com/o/r/pull/9" in out


def test_preflight_no_pending_deps_passes() -> None:
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches([]):
            stack.enter_context(p)
        ctx = preflight(
            version_override=None,
            repo_root=Path("/tmp/repo"),  # noqa: S108
            allow_pending_deps=True,
        )
    assert ctx.bypassed_deps_prs == []


def test_preflight_resume_skips_pending_deps_gate() -> None:
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="o/r"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".pending_deps_prs") as m_pending,
    ):
        ctx = preflight(
            version_override=None,
            repo_root=Path("/tmp/repo"),  # noqa: S108
            resume=True,
            resume_version="2.1.0",
            resume_issue_number=7,
        )
    m_pending.assert_not_called()
    assert ctx.bypassed_deps_prs == []
    assert ctx.cleared_deps_prs == []


# --- auto-clear a dependency-update PR whose fix task completed (#3286) ---

_PR9 = "https://github.com/o/r/pull/9"
_PR11 = "https://github.com/o/r/pull/11"
_TASK10 = "https://github.com/o/r/issues/10"
_TASK12 = "https://github.com/o/r/issues/12"


def test_fix_task_status_reads_state_and_reason() -> None:
    raw = {"state": "CLOSED", "stateReason": "COMPLETED"}
    with patch(_MOD + ".github.read_json", return_value=raw) as m:
        assert fix_task_status("https://github.com/other/repo/issues/3282") == (
            "CLOSED",
            "COMPLETED",
        )
    args = list(m.call_args.args)
    assert args[:3] == ["issue", "view", "3282"]
    assert args[args.index("--repo") + 1] == "other/repo"
    assert args[args.index("--json") + 1] == "state,stateReason"


def test_fix_task_status_open_issue_has_no_reason() -> None:
    raw = {"state": "OPEN", "stateReason": ""}
    with patch(_MOD + ".github.read_json", return_value=raw):
        assert fix_task_status(_TASK10) == ("OPEN", None)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/pull/10",
        "https://example.com/o/r/issues/10",
        "o/r#10",
        "https://github.com/o/r/issues/ten",
    ],
)
def test_fix_task_status_rejects_unparseable_url(url: str) -> None:
    with (
        patch(_MOD + ".github.read_json") as m,
        pytest.raises(ReleaseError, match="Cannot parse fix task URL") as info,
    ):
        fix_task_status(url)
    m.assert_not_called()
    assert info.value.phase == "preflight"


def test_fix_task_status_fails_loud_on_gh_error() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 502")
    with (
        patch(_MOD + ".github.read_json", side_effect=err),
        pytest.raises(ReleaseError, match="Could not look up fix task") as info,
    ):
        fix_task_status(_TASK10)
    assert info.value.detail is not None
    assert "502" in info.value.detail


def test_fix_task_status_fails_loud_on_bad_json() -> None:
    with (
        patch(_MOD + ".github.read_json", side_effect=json.JSONDecodeError("x", "doc", 0)),
        pytest.raises(ReleaseError, match="Could not look up fix task"),
    ):
        fix_task_status(_TASK10)


@pytest.mark.parametrize("raw", [[], {"stateReason": "COMPLETED"}, {"state": ""}])
def test_fix_task_status_rejects_malformed_answer(raw: object) -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=raw),
        pytest.raises(ReleaseError, match="no state"),
    ):
        fix_task_status(_TASK10)


def _run_preflight_with_tasks(
    pending: list[PendingDepsPr],
    statuses: dict[str, tuple[str, str | None]],
    *,
    allow: bool = False,
    run_side_effect: object = None,
) -> tuple[ReleaseContext, list[list[str]]]:
    calls: list[list[str]] = []

    def fake_run(*args: str) -> None:
        calls.append(list(args))
        if callable(run_side_effect):
            run_side_effect(list(args))

    with contextlib.ExitStack() as stack:
        for p in _fresh_patches(pending):
            stack.enter_context(p)
        stack.enter_context(patch(_MOD + ".fix_task_status", side_effect=statuses.__getitem__))
        stack.enter_context(patch(_MOD + ".github.run", side_effect=fake_run))
        ctx = preflight(
            version_override=None,
            repo_root=Path("/tmp/repo"),  # noqa: S108
            allow_pending_deps=allow,
        )
    return ctx, calls


def test_preflight_clears_pr_whose_fix_task_completed(capsys: pytest.CaptureFixture[str]) -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]
    ctx, calls = _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "COMPLETED")})
    assert ctx.cleared_deps_prs == [_PR9]
    assert ctx.bypassed_deps_prs == []
    # Comment first (naming the fix task and the preflight), then close.
    assert [c[:3] for c in calls] == [["pr", "comment", _PR9], ["pr", "close", _PR9]]
    comment = calls[0][calls[0].index("--body") + 1]
    assert _TASK10 in comment
    assert "vrg-release" in comment
    assert "preflight" in comment
    out = capsys.readouterr().out
    assert _PR9 in out
    assert _TASK10 in out


def test_preflight_not_planned_fix_task_keeps_blocking() -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError, match="dependency-update") as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "NOT_PLANNED")})
    msg = str(info.value)
    assert _PR9 in msg
    assert f"fix task {_TASK10} closed without completing (NOT_PLANNED)" in msg
    assert "human" in msg


def test_preflight_refusal_advice_no_fix_task_linked() -> None:
    pending = [PendingDepsPr(url=_PR11, fix_task=None)]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {})
    assert (
        f"{_PR11} (no fix task linked — the update is still pending or was never "
        "linked to a fix task; merge this PR if it is green, or close it deliberately)"
    ) in str(info.value)


def test_preflight_refusal_advice_fix_task_open() -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("OPEN", None)})
    assert (
        f"{_PR9} (fix task {_TASK10} is open — finish the fix: merging its PR "
        "closes the fix task as completed, and the next release then closes this "
        "PR itself)"
    ) in str(info.value)


def test_preflight_refusal_advice_fix_task_closed_not_completed() -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "NOT_PLANNED")})
    assert (
        f"{_PR9} (fix task {_TASK10} closed without completing (NOT_PLANNED) — the "
        "update did not land; deciding what to do with this PR is a human call: "
        "close it deliberately or reopen the fix task)"
    ) in str(info.value)


def test_preflight_refusal_trailer_keeps_only_bypass_advice() -> None:
    pending = [
        PendingDepsPr(url=_PR9, fix_task=_TASK10),
        PendingDepsPr(url=_PR11, fix_task=None),
    ]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("OPEN", None)})
    msg = str(info.value)
    # The per-PR advice is followed directly by the shared bypass-only trailer.
    assert (
        "or close it deliberately). For an urgent release, pass --allow-pending-deps "
        "(the bypass is logged on the tracking issue)."
    ) in msg
    assert "Clear the gate" not in msg
    assert "merging the PR" not in msg
    assert "landing the fix task" not in msg


def test_preflight_closed_fix_task_without_reason_keeps_blocking() -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError, match="closed without completing") as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", None)})
    assert "(no state reason)" in str(info.value)


def test_preflight_not_planned_does_not_close_pr() -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches(pending):
            stack.enter_context(p)
        stack.enter_context(
            patch(_MOD + ".fix_task_status", return_value=("CLOSED", "NOT_PLANNED"))
        )
        m_run = stack.enter_context(patch(_MOD + ".github.run"))
        with pytest.raises(ReleaseError):
            preflight(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    m_run.assert_not_called()


def test_preflight_allow_pending_deps_applies_after_clear_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pending = [
        PendingDepsPr(url=_PR9, fix_task=_TASK10),
        PendingDepsPr(url=_PR11, fix_task=_TASK12),
    ]
    ctx, calls = _run_preflight_with_tasks(
        pending,
        {_TASK10: ("CLOSED", "COMPLETED"), _TASK12: ("CLOSED", "NOT_PLANNED")},
        allow=True,
    )
    assert ctx.cleared_deps_prs == [_PR9]
    assert ctx.bypassed_deps_prs == [_PR11]
    assert [c[2] for c in calls] == [_PR9, _PR9]
    warning = next(line for line in capsys.readouterr().out.splitlines() if "WARNING" in line)
    assert _PR11 in warning
    assert _PR9 not in warning


def test_preflight_refusal_after_clear_names_only_blocking_prs() -> None:
    pending = [
        PendingDepsPr(url=_PR9, fix_task=_TASK10),
        PendingDepsPr(url=_PR11, fix_task=None),
    ]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "COMPLETED")})
    msg = str(info.value)
    assert _PR11 in msg
    assert _PR9 not in msg


def test_preflight_looks_up_every_fix_task_before_closing_any() -> None:
    pending = [
        PendingDepsPr(url=_PR9, fix_task=_TASK10),
        PendingDepsPr(url=_PR11, fix_task=_TASK12),
    ]
    boom = ReleaseError(phase="preflight", command="gh issue view", message="lookup failed")

    def status(url: str) -> tuple[str, str | None]:
        if url == _TASK12:
            raise boom
        return ("CLOSED", "COMPLETED")

    with contextlib.ExitStack() as stack:
        for p in _fresh_patches(pending):
            stack.enter_context(p)
        stack.enter_context(patch(_MOD + ".fix_task_status", side_effect=status))
        m_run = stack.enter_context(patch(_MOD + ".github.run"))
        with pytest.raises(ReleaseError, match="lookup failed"):
            preflight(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    m_run.assert_not_called()


def test_preflight_comment_failure_raises_and_leaves_pr_untouched() -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]

    def fail_comment(args: list[str]) -> None:
        if args[1] == "comment":
            raise subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 403")

    with pytest.raises(ReleaseError, match="Could not comment on") as info:
        _run_preflight_with_tasks(
            pending, {_TASK10: ("CLOSED", "COMPLETED")}, run_side_effect=fail_comment
        )
    msg = str(info.value)
    assert _PR9 in msg
    assert "still open" in msg
    assert info.value.detail is not None
    assert "403" in info.value.detail


def test_preflight_close_failure_after_comment_raises_with_pr_state() -> None:
    pending = [PendingDepsPr(url=_PR9, fix_task=_TASK10)]

    def fail_close(args: list[str]) -> None:
        if args[1] == "close":
            raise subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 502")

    with pytest.raises(ReleaseError, match="Could not close") as info:
        _run_preflight_with_tasks(
            pending, {_TASK10: ("CLOSED", "COMPLETED")}, run_side_effect=fail_close
        )
    msg = str(info.value)
    assert _PR9 in msg
    assert "commented" in msg
    assert "still open" in msg
    assert info.value.command == f"gh pr close {_PR9}"


def test_preflight_clear_step_end_to_end_with_gh_mocked() -> None:
    """The real fix_task_status path, gh mocked: #3281 / #3282 shaped."""
    pending = [
        PendingDepsPr(
            url="https://github.com/o/r/pull/3281",
            fix_task="https://github.com/o/r/issues/3282",
        )
    ]
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches(pending):
            stack.enter_context(p)
        m_json = stack.enter_context(
            patch(
                _MOD + ".github.read_json",
                return_value={"state": "CLOSED", "stateReason": "COMPLETED"},
            )
        )
        m_run = stack.enter_context(patch(_MOD + ".github.run"))
        ctx = preflight(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.cleared_deps_prs == ["https://github.com/o/r/pull/3281"]
    assert m_json.call_args.args[:3] == ("issue", "view", "3282")
    assert m_run.call_args.args[:3] == ("pr", "close", "https://github.com/o/r/pull/3281")
