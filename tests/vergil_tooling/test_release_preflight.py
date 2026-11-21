from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.cd_runs import CDRunLookupError
from vergil_tooling.lib.checkout_lock import CheckoutLockError, checkout_lock
from vergil_tooling.lib.managed_worktree import ManagedWorktreeError
from vergil_tooling.lib.pending_release import PendingRelease, PendingReleaseError, ReleaseTags
from vergil_tooling.lib.release import checklist, state
from vergil_tooling.lib.release import preflight as preflight_module
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.preflight import (
    PendingDepsPr,
    _check_branch_and_tree,
    _check_no_open_release,
    _compute_release_version,
    _release_contents,
    check_develop_cd,
    fix_task_status,
    pending_deps_prs,
    preflight,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from vergil_tooling.lib.release.context import ReleaseContext

_MOD = "vergil_tooling.lib.release.preflight"


@pytest.fixture(autouse=True)
def develop_cd_gate() -> Iterator[MagicMock]:
    """Stub the develop-CD gate, which reads GitHub, out of every preflight run.

    The gate's own tests call :func:`check_develop_cd` directly.
    """
    with patch(_MOD + "._develop_cd_gate") as gate:
        yield gate


_CUT = "c" * 40
_PENDING = PendingRelease(
    tags=ReleaseTags(latest=None, rolling=None, rolling_target=None),
    main_moved_ts=0.0,
    changes=(),
)


@pytest.fixture(autouse=True)
def release_contents() -> Iterator[MagicMock]:
    """Stub the release-contents read, which reads git and GitHub, out of every preflight run.

    Its own tests call :func:`_release_contents` directly.
    """
    with patch(_MOD + "._release_contents", return_value=(_CUT, _PENDING)) as contents:
        yield contents


def test_preflight_fresh_start_records_the_cut_and_contents(
    release_contents: MagicMock,
) -> None:
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_main_not_ahead_of_develop"),
        patch(_MOD + "._check_no_open_release"),
        patch(_MOD + "._check_packages_unpublished"),
        patch(_MOD + ".pending_deps_prs", return_value=[]),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
    ):
        ctx = preflight(version_override=None, repo_root=root)
    release_contents.assert_called_once_with(wt)
    assert ctx.cut == _CUT
    assert ctx.pending_release is _PENDING


def test_preflight_resume_reads_no_contents(release_contents: MagicMock) -> None:
    root = Path("/tmp/repo")  # noqa: S108
    wt = Path("/tmp/repo/.worktrees/release-2.3.0")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
    ):
        ctx = preflight(version_override=None, repo_root=root, resume=True, resume_version="2.3.0")
    release_contents.assert_not_called()
    assert ctx.cut is None
    assert ctx.pending_release is None


def test_release_contents_pins_the_pending_release_to_the_cut() -> None:
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + ".git.read_output", return_value=_CUT) as m_git,
        patch(_MOD + ".gather_pending_release", return_value=_PENDING) as m_gather,
    ):
        assert _release_contents(wt) == (_CUT, _PENDING)
    m_git.assert_called_once_with("-C", str(wt), "rev-parse", "HEAD")
    m_gather.assert_called_once_with(integration="develop", head=_CUT)


def test_release_contents_incomplete_lookup_refuses() -> None:
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    with (
        patch(_MOD + ".git.read_output", return_value=_CUT),
        patch(
            _MOD + ".gather_pending_release",
            side_effect=PendingReleaseError("compare truncated"),
        ),
        pytest.raises(ReleaseError, match="Could not read what the release contains") as info,
    ):
        _release_contents(wt)
    assert "compare truncated" in str(info.value)
    assert info.value.phase == "preflight"


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
        patch(_MOD + "._check_main_not_ahead_of_develop"),
        patch(_MOD + "._check_no_open_release"),
        patch(_MOD + "._check_packages_unpublished"),
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
        patch(_MOD + "._check_main_not_ahead_of_develop") as m_diverged,
        patch(_MOD + "._check_no_open_release") as m_issue,
        patch(_MOD + "._check_packages_unpublished"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt) as m_wt,
        patch(_MOD + ".os.chdir"),
    ):
        preflight(version_override=None, repo_root=root, resume=True)
    m_tagged.assert_not_called()
    m_issue.assert_not_called()
    m_diverged.assert_not_called()
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
        patch(_MOD + "._check_main_not_ahead_of_develop"),
        patch(_MOD + "._check_no_open_release"),
        patch(_MOD + "._check_packages_unpublished"),
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
        patch(_MOD + "._check_main_not_ahead_of_develop"),
        patch(_MOD + "._check_no_open_release"),
        patch(_MOD + "._check_packages_unpublished"),
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
        pytest.raises(ReleaseError, match=r"No container runtime \(docker or nerdctl\)"),
    ):
        _check_host_prerequisites()


def testcheck_gh_auth_fails() -> None:
    from vergil_tooling.lib.release.preflight import check_gh_auth

    with (
        patch(_MOD + ".github.read_output", side_effect=Exception("auth failed")),
        pytest.raises(ReleaseError, match="authentication failed"),
    ):
        check_gh_auth()


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


_RESUME_READ = "vergil_tooling.lib.release.resume.github.read_json"


def _release_issue(ver: str, number: int, body: str) -> dict[str, object]:
    return {"number": number, "title": f"release: {ver}", "body": body}


def _state_body(ver: str) -> str:
    block = state.render_state(
        state.initial_state(version=ver, cut="abc1234", issues=(), without_deps=False)
    )
    return f"## Contents\n\n{block}\n"


def test_fresh_start_refuses_other_version_open() -> None:
    open_issues = [_release_issue("2.0.33", 99, _state_body("2.0.33"))]
    with (
        patch(_RESUME_READ, return_value=open_issues),
        pytest.raises(ReleaseError) as info,
    ):
        _check_no_open_release("owner/repo")
    message = str(info.value)
    assert info.value.phase == "preflight"
    assert "https://github.com/owner/repo/issues/99" in message
    assert "release 2.0.33" in message
    assert "`vrg-release --resume 2.0.33`" in message
    assert "by hand" not in message


def test_fresh_start_refusal_names_old_tooling_recovery() -> None:
    old_body = "## Release\n\n" + checklist.render(["audit", "prepare"], ())
    open_issues = [_release_issue("2.0.33", 98, old_body)]
    with (
        patch(_RESUME_READ, return_value=open_issues),
        pytest.raises(ReleaseError) as info,
    ):
        _check_no_open_release("owner/repo")
    message = str(info.value)
    assert "https://github.com/owner/repo/issues/98" in message
    assert "no release state block" in message
    assert "Finish that release by hand, then close the issue." in message
    assert "--resume 2.0.33" not in message


def test_fresh_start_refusal_names_unreadable_state_block() -> None:
    body = f"{state.BEGIN}\n```json\n{{}}\n```\n{state.END}\n"
    with (
        patch(_RESUME_READ, return_value=[_release_issue("2.0.33", 97, body)]),
        pytest.raises(ReleaseError, match="cannot be read") as info,
    ):
        _check_no_open_release("owner/repo")
    assert "`vrg-release --resume 2.0.33`" in str(info.value)


def test_fresh_start_refusal_names_every_open_release() -> None:
    open_issues = [
        _release_issue("2.0.33", 99, _state_body("2.0.33")),
        _release_issue("2.0.32", 90, "no block"),
    ]
    with (
        patch(_RESUME_READ, return_value=open_issues),
        pytest.raises(ReleaseError) as info,
    ):
        _check_no_open_release("owner/repo")
    lines = [line for line in str(info.value).splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    assert "release 2.0.32" in lines[0]
    assert "by hand" in lines[0]
    assert "`vrg-release --resume 2.0.33`" in lines[1]


def test_fresh_start_passes_with_none_open() -> None:
    not_a_release = {"number": 5, "title": "chore(release): bump to 2.0.34", "body": ""}
    with patch(_RESUME_READ, return_value=[not_a_release]):
        _check_no_open_release("owner/repo")


def test_fresh_start_open_release_read_failure_raises() -> None:
    with (
        patch(_RESUME_READ, return_value={"message": "Bad credentials"}),
        pytest.raises(ReleaseError, match="not a list") as info,
    ):
        _check_no_open_release("owner/repo")
    assert info.value.phase == "preflight"


@pytest.mark.parametrize(
    ("on_path", "runtime"),
    [({"nerdctl", "docker"}, "nerdctl"), ({"docker"}, "docker")],
)
def test_check_host_prerequisites_success(on_path: set[str], runtime: str) -> None:
    from vergil_tooling.lib.release.preflight import _check_host_prerequisites

    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch(
            "shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if name in on_path else None
        ),
        patch(_MOD + ".subprocess.run", return_value=ok) as mock_run,
    ):
        _check_host_prerequisites()
    assert mock_run.call_args[0][0] == (runtime, "version")


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, "docker version", stderr="Cannot connect"),
        subprocess.TimeoutExpired("docker version", 15),
    ],
)
def test_check_host_prerequisites_runtime_unreachable(failure: Exception) -> None:
    from vergil_tooling.lib.release.preflight import _check_host_prerequisites

    with (
        patch(
            "shutil.which", side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None
        ),
        patch(_MOD + ".subprocess.run", side_effect=failure),
        pytest.raises(ReleaseError, match="docker container runtime is not reachable"),
    ):
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


def testaudit_repo_config_success() -> None:
    from subprocess import CompletedProcess

    from vergil_tooling.lib.release.preflight import audit_repo_config

    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="", stderr=""),
    ) as m_run:
        audit_repo_config("owner/repo")
    assert m_run.call_args.args[0] == ("vrg-github-repo-config", "audit", "--repo", "owner/repo")


def test_audit_repo_config_in_tree_templates_passes_the_flag() -> None:
    from subprocess import CompletedProcess

    from vergil_tooling.lib.release.preflight import audit_repo_config

    with (
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=1, stdout="", stderr=""),
        ) as m_run,
        pytest.raises(ReleaseError, match="non-compliant") as exc_info,
    ):
        audit_repo_config("o/vergil-tooling", in_tree_templates=True)
    command = (
        "vrg-github-repo-config",
        "audit",
        "--repo",
        "o/vergil-tooling",
        "--in-tree-templates",
    )
    assert m_run.call_args.args[0] == command
    assert exc_info.value.command == " ".join(command)


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
        patch(_MOD + "._check_main_not_ahead_of_develop"),
        patch(_MOD + "._check_no_open_release"),
        patch(_MOD + "._check_packages_unpublished"),
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


def test_acquire_release_worktree_refuses_a_branch_only_on_origin() -> None:
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    with (
        patch(_MOD + ".git.ref_exists", side_effect=lambda ref: ref.startswith("origin/")),
        patch(_MOD + ".adopt_worktree") as m_adopt,
        pytest.raises(ReleaseError, match="already exists on origin"),
    ):
        _acquire_release_worktree(Path("/tmp/repo"), "release/2.1.0", resume=False)  # noqa: S108
    m_adopt.assert_not_called()


class _Leftover:
    """Git stand-ins for a local release branch an interrupted fresh run left (#4367).

    *pushed* is what ``ls-remote`` reports, *ahead* the commits develop lacks,
    and *head*/*status* what the leftover worktree, when present, reports.
    """

    def __init__(
        self,
        *,
        pushed: str = "",
        ahead: int = 0,
        head: str = "release/2.1.0",
        status: str = "",
        ls_remote_error: bool = False,
    ) -> None:
        self.pushed = pushed
        self.ahead = ahead
        self.head = head
        self.status = status
        self.ls_remote_error = ls_remote_error
        self.runs: list[tuple[str, ...]] = []

    def read_output(self, *args: str) -> str:
        if args[0] == "ls-remote":
            if self.ls_remote_error:
                raise subprocess.CalledProcessError(128, ("git", *args), stderr="unreachable")
            return self.pushed
        if args[2:] == ("rev-parse", "--abbrev-ref", "HEAD"):
            return self.head
        if args[2:] == ("status", "--porcelain"):
            return self.status
        msg = f"unexpected git read: {args}"
        raise AssertionError(msg)

    def run(self, *args: str) -> None:
        self.runs.append(args)


def _acquire_leftover(
    tmp_path: Path, leftover: _Leftover, *, worktree_present: bool = False
) -> tuple[Path, Path]:
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    wt = tmp_path / ".worktrees" / "release-2.1.0"
    if worktree_present:
        wt.mkdir(parents=True)
    with (
        patch(_MOD + ".git.ref_exists", side_effect=lambda ref: ref == "release/2.1.0"),
        patch(_MOD + ".git.read_output", side_effect=leftover.read_output),
        patch(_MOD + ".git.commits_ahead", return_value=leftover.ahead),
        patch(_MOD + ".git.run", side_effect=leftover.run),
        patch(_MOD + ".adopt_worktree", return_value=wt) as m_adopt,
        patch(_MOD + ".create_worktree") as m_create,
    ):
        result = _acquire_release_worktree(tmp_path, "release/2.1.0", resume=False)
    m_adopt.assert_called_once_with(tmp_path, branch="release/2.1.0")
    m_create.assert_not_called()
    return result, wt


def test_acquire_release_worktree_adopts_an_unrecorded_local_branch(tmp_path: Path) -> None:
    # The 2026-11-15 case: preflight made the branch and worktree, then the
    # run was interrupted before prepare opened the tracking issue.
    leftover = _Leftover()
    result, wt = _acquire_leftover(tmp_path, leftover, worktree_present=True)
    assert result == wt
    assert leftover.runs == [("-C", str(wt), "merge", "--ff-only", "develop")]


def test_acquire_release_worktree_reattaches_an_unrecorded_branch_with_no_worktree(
    tmp_path: Path,
) -> None:
    leftover = _Leftover(head="never-asked")
    result, wt = _acquire_leftover(tmp_path, leftover)
    assert result == wt
    assert leftover.runs == [("-C", str(wt), "merge", "--ff-only", "develop")]


@pytest.mark.parametrize(
    ("leftover", "match"),
    [
        (_Leftover(pushed="abc123\trefs/heads/release/2.1.0"), "was pushed to origin"),
        (_Leftover(ls_remote_error=True), "could not be asked"),
        (_Leftover(ahead=2), "2 commit\\(s\\) develop lacks"),
    ],
)
def test_acquire_release_worktree_refuses_a_branch_that_may_hold_work(
    tmp_path: Path, leftover: _Leftover, match: str
) -> None:
    with pytest.raises(ReleaseError, match=match):
        _acquire_leftover(tmp_path, leftover, worktree_present=True)
    assert leftover.runs == []


@pytest.mark.parametrize(
    ("leftover", "match"),
    [
        (_Leftover(head="develop"), "is on 'develop', not on it"),
        (_Leftover(status=" M CHANGELOG.md"), "has uncommitted changes"),
    ],
)
def test_acquire_release_worktree_refuses_a_leftover_worktree_that_may_hold_work(
    tmp_path: Path, leftover: _Leftover, match: str
) -> None:
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    (tmp_path / ".worktrees" / "release-2.1.0").mkdir(parents=True)
    with (
        patch(_MOD + ".git.ref_exists", side_effect=lambda ref: ref == "release/2.1.0"),
        patch(_MOD + ".git.read_output", side_effect=leftover.read_output),
        patch(_MOD + ".git.commits_ahead", return_value=0),
        patch(_MOD + ".git.run", side_effect=leftover.run),
        patch(_MOD + ".adopt_worktree") as m_adopt,
        pytest.raises(ReleaseError, match=match),
    ):
        _acquire_release_worktree(tmp_path, "release/2.1.0", resume=False)
    m_adopt.assert_not_called()
    assert leftover.runs == []


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


_ELSEWHERE = ManagedWorktreeError(
    "Could not attach the worktree /tmp/repo/.worktrees/release-2.1.0 to branch "
    "'release/2.1.0'. If the branch is checked out in another worktree (see `git "
    "worktree list`), remove that worktree or switch it to another branch, then re-run.",
    detail="stderr: fatal: 'release/2.1.0' is already used by worktree at '/tmp/other'",
)


def test_acquire_release_worktree_names_a_branch_checked_out_elsewhere_on_resume() -> None:
    # #4442: the resume path's adopt fails because the branch is checked out
    # in another worktree; the human gets a ReleaseError, not a traceback.
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    with (
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".adopt_worktree", side_effect=_ELSEWHERE),
        pytest.raises(ReleaseError, match="checked out in another worktree") as info,
    ):
        _acquire_release_worktree(Path("/tmp/repo"), "release/2.1.0", resume=True)  # noqa: S108
    assert info.value.phase == "preflight"
    assert info.value.detail == _ELSEWHERE.detail


def test_acquire_release_worktree_names_a_branch_checked_out_elsewhere_on_fresh_run(
    tmp_path: Path,
) -> None:
    # #4442: the fresh path adopting an unrecorded branch hits the same refusal.
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    leftover = _Leftover()
    with (
        patch(_MOD + ".git.ref_exists", side_effect=lambda ref: ref == "release/2.1.0"),
        patch(_MOD + ".git.read_output", side_effect=leftover.read_output),
        patch(_MOD + ".git.commits_ahead", return_value=0),
        patch(_MOD + ".git.run", side_effect=leftover.run),
        patch(_MOD + ".adopt_worktree", side_effect=_ELSEWHERE),
        pytest.raises(ReleaseError, match="checked out in another worktree") as info,
    ):
        _acquire_release_worktree(tmp_path, "release/2.1.0", resume=False)
    assert info.value.detail == _ELSEWHERE.detail
    assert leftover.runs == []


@pytest.mark.parametrize(
    ("resume", "failing", "rerun"),
    [
        (True, ("branch", "release/2.1.0", "origin/release/2.1.0"), "vrg-release --resume"),
        (False, ("merge", "--ff-only", "develop"), "vrg-release`"),
    ],
)
def test_acquire_release_worktree_wraps_a_raw_git_failure(
    tmp_path: Path, resume: bool, failing: tuple[str, ...], rerun: str
) -> None:
    # #4442: a git call outside the managed-worktree helpers (the resume
    # path's local branch, the fresh path's fast-forward) fails: the
    # CalledProcessError is translated, with the branch, worktree and recovery.
    from vergil_tooling.lib.release.preflight import _acquire_release_worktree

    def _run(*args: str) -> None:
        raise subprocess.CalledProcessError(128, ("git", *args), stderr="fatal: boom")

    wt = tmp_path / ".worktrees" / "release-2.1.0"
    leftover = _Leftover()
    with (
        patch(
            _MOD + ".git.ref_exists",
            side_effect=lambda ref: ref.startswith("origin/") if resume else ref == "release/2.1.0",
        ),
        patch(_MOD + ".git.read_output", side_effect=leftover.read_output),
        patch(_MOD + ".git.commits_ahead", return_value=0),
        patch(_MOD + ".git.run", side_effect=_run),
        patch(_MOD + ".adopt_worktree", return_value=wt),
        pytest.raises(ReleaseError, match="release branch 'release/2.1.0'") as info,
    ):
        _acquire_release_worktree(tmp_path, "release/2.1.0", resume=resume)
    message = str(info.value)
    assert str(wt) in message
    assert rerun in message
    assert info.value.command.endswith(" ".join(failing))
    assert info.value.detail is not None
    assert "stderr: fatal: boom" in info.value.detail


def test_run_audit_resolves_repo_and_audits(tmp_path: Path) -> None:
    # Any repo but vergil-tooling: the installed templates, as before (#4414).
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-app"\n')
    with (
        patch(_MOD + ".check_gh_auth", return_value="owner/repo") as m_auth,
        patch(_MOD + ".audit_repo_config") as m_audit,
    ):
        preflight_module.run_audit(tmp_path)
    m_auth.assert_called_once_with()
    m_audit.assert_called_once_with("owner/repo", in_tree_templates=False)


def test_run_audit_self_repo_uses_in_tree_templates(tmp_path: Path) -> None:
    # Releasing vergil-tooling itself audits against the templates being
    # released, not the installed release's (#4414).
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    with (
        patch(_MOD + ".check_gh_auth", return_value="vergil-project/vergil-tooling"),
        patch(_MOD + ".audit_repo_config") as m_audit,
    ):
        preflight_module.run_audit(tmp_path)
    m_audit.assert_called_once_with("vergil-project/vergil-tooling", in_tree_templates=True)


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


def test_preflight_wraps_cargo_version_error() -> None:
    from vergil_tooling.lib.version import CargoVersionError

    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + ".check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + ".audit_repo_config"),
        patch(
            _MOD + ".version.show",
            side_effect=CargoVersionError("Cargo.toml holds no version: [workspace.package]"),
        ),
        pytest.raises(ReleaseError, match=r"\[workspace\.package\]"),
    ):
        preflight(version_override=None, repo_root=root)


# --- dependency-update release gate (epic vergil-project/.github#381, spec §5.3) ---

_HEAD = "deps/update-2.1.0"

_PRS = [
    {
        "url": "https://github.com/o/r/pull/9",
        "body": "Mechanized update.\n\nFix task: https://github.com/o/r/issues/10\n",
        "headRefName": _HEAD,
    },
    {"url": "https://github.com/o/r/pull/11", "body": "no fix task yet", "headRefName": _HEAD},
    {"url": "https://github.com/o/r/pull/12", "body": None, "headRefName": _HEAD},
]


def test_pending_deps_prs_parses_fix_task() -> None:
    with patch(_MOD + ".github.read_json", return_value=_PRS) as m:
        prs = pending_deps_prs("o/r")
    assert prs == [
        PendingDepsPr(
            head=_HEAD,
            url="https://github.com/o/r/pull/9",
            fix_task="https://github.com/o/r/issues/10",
        ),
        PendingDepsPr(head=_HEAD, url="https://github.com/o/r/pull/11", fix_task=None),
        PendingDepsPr(head=_HEAD, url="https://github.com/o/r/pull/12", fix_task=None),
    ]
    args = list(m.call_args.args)
    assert args[:2] == ["pr", "list"]
    assert args[args.index("--repo") + 1] == "o/r"
    assert args[args.index("--state") + 1] == "open"
    assert args[args.index("--label") + 1] == "dependency-update"
    assert "headRefName" in args[args.index("--json") + 1].split(",")


@pytest.mark.parametrize("head", [None, ""])
def test_pending_deps_prs_rejects_entry_without_head(head: str | None) -> None:
    entry = {"url": "https://github.com/o/r/pull/9", "body": "", "headRefName": head}
    with (
        patch(_MOD + ".github.read_json", return_value=[entry]),
        pytest.raises(ReleaseError, match="without a head branch"),
    ):
        pending_deps_prs("o/r")


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


def _deps_prs(count: int) -> list[dict[str, object]]:
    return [
        {"url": f"https://github.com/o/r/pull/{i}", "body": "", "headRefName": _HEAD}
        for i in range(1, count + 1)
    ]


def test_pending_deps_prs_accepts_a_list_one_short_of_the_limit() -> None:
    count = preflight_module._PENDING_DEPS_LIMIT - 1
    with patch(_MOD + ".github.read_json", return_value=_deps_prs(count)) as m:
        assert len(pending_deps_prs("o/r")) == count
    args = list(m.call_args.args)
    assert args[args.index("--limit") + 1] == str(preflight_module._PENDING_DEPS_LIMIT)


def test_pending_deps_prs_refuses_a_list_that_may_be_truncated() -> None:
    # A pending PR past the limit would go unseen, so a full page refuses (#4038).
    rows = _deps_prs(preflight_module._PENDING_DEPS_LIMIT)
    with (
        patch(_MOD + ".github.read_json", return_value=rows),
        pytest.raises(ReleaseError, match="may be truncated; refusing to release blind"),
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
        patch(_MOD + "._check_main_not_ahead_of_develop"),
        patch(_MOD + "._check_no_open_release"),
        patch(_MOD + "._check_packages_unpublished"),
        patch(_MOD + "._acquire_release_worktree", return_value=wt),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".pending_deps_prs", return_value=pending),
    ]


def test_preflight_refuses_on_pending_deps_pr() -> None:
    pending = [
        PendingDepsPr(
            head=_HEAD,
            url="https://github.com/o/r/pull/9",
            fix_task="https://github.com/o/r/issues/10",
        ),
        PendingDepsPr(head=_HEAD, url="https://github.com/o/r/pull/11", fix_task=None),
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
    pending = [PendingDepsPr(head=_HEAD, url="https://github.com/o/r/pull/9", fix_task=None)]
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
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]
    ctx, calls = _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "COMPLETED")})
    assert ctx.cleared_deps_prs == [_PR9]
    assert ctx.bypassed_deps_prs == []
    # Comment first (naming the fix task and the preflight), close, then delete
    # the head branch (#3332): delete-on-merge never covers a closed PR.
    assert calls == [
        calls[0],
        ["pr", "close", _PR9],
        ["api", "-X", "DELETE", f"repos/o/r/git/refs/heads/{_HEAD}"],
    ]
    assert calls[0][:3] == ["pr", "comment", _PR9]
    assert ctx.undeleted_deps_branches == []
    comment = calls[0][calls[0].index("--body") + 1]
    assert _TASK10 in comment
    assert "vrg-release" in comment
    assert "preflight" in comment
    out = capsys.readouterr().out
    assert _PR9 in out
    assert _TASK10 in out
    assert f"Deleted branch {_HEAD}" in out


def test_preflight_branch_delete_failure_records_and_still_clears(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The PR is closed, so the gate clears; the leftover branch is recorded, not silent."""
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]

    def fail_delete(args: list[str]) -> None:
        if args[0] == "api":
            raise subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 403")

    ctx, _ = _run_preflight_with_tasks(
        pending, {_TASK10: ("CLOSED", "COMPLETED")}, run_side_effect=fail_delete
    )
    assert ctx.cleared_deps_prs == [_PR9]
    assert ctx.undeleted_deps_branches == [_HEAD]
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "403" in out
    assert f"git push origin --delete {_HEAD}" in out


def test_preflight_branch_already_deleted_counts_as_deleted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]

    def gone(args: list[str]) -> None:
        if args[0] == "api":
            raise subprocess.CalledProcessError(
                1, ["gh"], output="", stderr="gh: Reference does not exist (HTTP 422)"
            )

    ctx, _ = _run_preflight_with_tasks(
        pending, {_TASK10: ("CLOSED", "COMPLETED")}, run_side_effect=gone
    )
    assert ctx.undeleted_deps_branches == []
    assert "already deleted" in capsys.readouterr().out


def test_every_pr_close_in_src_deletes_its_branch() -> None:
    """A PR closed without merging keeps its branch unless the closer deletes it (#3332).

    Every `gh pr close` in src/ must pass `--delete-branch`, or carry a comment
    naming `--delete-branch` in the lines just above it saying why not.
    """
    src = Path(preflight_module.__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if '"pr", "close"' not in line:
                continue
            window = "\n".join(lines[max(0, i - 5) : i + 1])
            if "--delete-branch" not in window:
                offenders.append(f"{path.relative_to(src)}:{i + 1}")
    assert offenders == []


def test_preflight_not_planned_fix_task_keeps_blocking() -> None:
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError, match="dependency-update") as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "NOT_PLANNED")})
    msg = str(info.value)
    assert _PR9 in msg
    assert f"fix task {_TASK10} closed without completing (NOT_PLANNED)" in msg
    assert "human" in msg


def test_preflight_refusal_advice_no_fix_task_linked() -> None:
    pending = [PendingDepsPr(head=_HEAD, url=_PR11, fix_task=None)]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {})
    assert (
        f"{_PR11} (no fix task linked — the update is still pending or was never "
        "linked to a fix task; merge this PR if it is green, or close it deliberately)"
    ) in str(info.value)


def test_preflight_refusal_advice_fix_task_open() -> None:
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("OPEN", None)})
    assert (
        f"{_PR9} (fix task {_TASK10} is open — finish the fix: merging its PR "
        "closes the fix task as completed, and the next release then closes this "
        "PR itself)"
    ) in str(info.value)


def test_preflight_refusal_advice_fix_task_closed_not_completed() -> None:
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "NOT_PLANNED")})
    assert (
        f"{_PR9} (fix task {_TASK10} closed without completing (NOT_PLANNED) — the "
        "update did not land; deciding what to do with this PR is a human call: "
        "close it deliberately or reopen the fix task)"
    ) in str(info.value)


def test_preflight_refusal_trailer_keeps_only_bypass_advice() -> None:
    pending = [
        PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10),
        PendingDepsPr(head=_HEAD, url=_PR11, fix_task=None),
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
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]
    with pytest.raises(ReleaseError, match="closed without completing") as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", None)})
    assert "(no state reason)" in str(info.value)


def test_preflight_not_planned_does_not_close_pr() -> None:
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]
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
        PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10),
        PendingDepsPr(head=_HEAD, url=_PR11, fix_task=_TASK12),
    ]
    ctx, calls = _run_preflight_with_tasks(
        pending,
        {_TASK10: ("CLOSED", "COMPLETED"), _TASK12: ("CLOSED", "NOT_PLANNED")},
        allow=True,
    )
    assert ctx.cleared_deps_prs == [_PR9]
    assert ctx.bypassed_deps_prs == [_PR11]
    assert [c[:3] for c in calls[1:]] == [["pr", "close", _PR9], ["api", "-X", "DELETE"]]
    warning = next(line for line in capsys.readouterr().out.splitlines() if "WARNING" in line)
    assert _PR11 in warning
    assert _PR9 not in warning


def test_preflight_refusal_after_clear_names_only_blocking_prs() -> None:
    pending = [
        PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10),
        PendingDepsPr(head=_HEAD, url=_PR11, fix_task=None),
    ]
    with pytest.raises(ReleaseError) as info:
        _run_preflight_with_tasks(pending, {_TASK10: ("CLOSED", "COMPLETED")})
    msg = str(info.value)
    assert _PR11 in msg
    assert _PR9 not in msg


def test_preflight_looks_up_every_fix_task_before_closing_any() -> None:
    pending = [
        PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10),
        PendingDepsPr(head=_HEAD, url=_PR11, fix_task=_TASK12),
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
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]

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
    pending = [PendingDepsPr(head=_HEAD, url=_PR9, fix_task=_TASK10)]

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
            head=_HEAD,
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
    assert [c.args[:3] for c in m_run.call_args_list[1:]] == [
        ("pr", "close", "https://github.com/o/r/pull/3281"),
        ("api", "-X", "DELETE"),
    ]


# --- package re-publish check (#3309) ----------------------------------------

_VP_TOML = """\
[project]
repository-type = "infrastructure"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[dependencies]
vergil = "v2.1"

[ci]
versions = ["latest"]

[package]
builder = "staged"
vendor = "vergil"
name = "vergil-python3.14.8"
version = "3.14.8+20261003"
summary = "Pinned CPython"
smoke = "true"

[package.staged]
build-command = "packaging/build.sh"
"""


def test_check_packages_unpublished_passes_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VP_TOML)
    with patch(_MOD + ".published.published", return_value={}) as m_pub:
        preflight_module._check_packages_unpublished("o/r", tmp_path, "1.0.1")
    m_pub.assert_called_once()
    assert m_pub.call_args.args[:2] == ("o/r", "v1.0.1")
    assert "Package check passed: 4 package(s) not yet published by o/r" in capsys.readouterr().out


def test_check_packages_unpublished_refuses_the_incident_shape(tmp_path: Path) -> None:
    """vergil-python v1.0.1 rebuilding v1.0.0's 3.14.8+20261003-1 is refused in preflight."""
    from vergil_tooling.lib.package.index.collect import Identity

    (tmp_path / "vergil.toml").write_text(_VP_TOML)
    prior = {Identity("deb", "vergil-python3.14.8", "3.14.8+20261003-1", "amd64"): "v1.0.0"}
    with (
        patch(_MOD + ".published.published", return_value=prior),
        pytest.raises(ReleaseError) as info,
    ):
        preflight_module._check_packages_unpublished("o/r", tmp_path, "1.0.1")
    assert info.value.phase == "preflight"
    assert info.value.command == "vrg-package check-unpublished --repo o/r --version 1.0.1"
    assert "already published by o/r@v1.0.0" in str(info.value)


def test_check_packages_unpublished_without_package_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VP_TOML.split("[package]")[0])
    with patch(_MOD + ".published.published") as m_pub:
        preflight_module._check_packages_unpublished("o/r", tmp_path, "1.0.1")
    m_pub.assert_not_called()
    assert capsys.readouterr().out == ""


def test_check_packages_unpublished_refuses_when_state_is_unreadable(tmp_path: Path) -> None:
    from vergil_tooling.lib.package import PackageError

    (tmp_path / "vergil.toml").write_text(_VP_TOML)
    with (
        patch(_MOD + ".published.published", side_effect=PackageError("listing failed: 502")),
        pytest.raises(ReleaseError, match="listing failed: 502"),
    ):
        preflight_module._check_packages_unpublished("o/r", tmp_path, "1.0.1")


def test_check_packages_unpublished_wraps_config_error(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        _VP_TOML.replace("[package]\n", '[package]\nexclude = ["*/*/*"]\n')
    )
    with pytest.raises(ReleaseError, match="selects no targets"):
        preflight_module._check_packages_unpublished("o/r", tmp_path, "1.0.1")


def test_preflight_package_refusal_precedes_deps_gate_and_worktree() -> None:
    refusal = ReleaseError(phase="preflight", command="c", message="would re-publish")
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches([]):
            stack.enter_context(p)
        stack.enter_context(patch(_MOD + "._check_packages_unpublished", side_effect=refusal))
        m_deps = stack.enter_context(patch(_MOD + ".pending_deps_prs"))
        m_wt = stack.enter_context(patch(_MOD + "._acquire_release_worktree"))
        with pytest.raises(ReleaseError, match="would re-publish"):
            preflight(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    m_deps.assert_not_called()
    m_wt.assert_not_called()


def test_preflight_runs_package_check_with_release_version() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches([]):
            stack.enter_context(p)
        m_check = stack.enter_context(patch(_MOD + "._check_packages_unpublished"))
        preflight(version_override="minor", repo_root=root)
    m_check.assert_called_once_with("o/r", root, "2.2.0")


# --- main/develop divergence (#3527) ---


def test_check_main_not_ahead_of_develop_passes_when_main_is_merged() -> None:
    with (
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".git.read_output", return_value="") as m_read,
    ):
        preflight_module._check_main_not_ahead_of_develop()
    m_run.assert_called_once_with("fetch", "origin", "main")
    assert m_read.call_args.args[-1] == "origin/develop..origin/main"


def test_check_main_not_ahead_of_develop_refuses_with_recovery_path() -> None:
    ahead = "abc1234 Merge pull request #7 from o/release/2.1.6\ndef5678 fix: on main"
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=ahead),
        pytest.raises(ReleaseError, match="2 commit") as info,
    ):
        preflight_module._check_main_not_ahead_of_develop()
    msg = str(info.value)
    assert info.value.phase == "preflight"
    assert "vrg-release --resume" in msg
    assert "git switch -c release/back-merge-main origin/main" in msg
    assert "merge commit" in msg
    assert info.value.detail == ahead


def test_check_main_not_ahead_of_develop_refuses_when_compare_fails() -> None:
    with (
        patch(
            _MOD + ".git.run",
            side_effect=subprocess.CalledProcessError(
                128, "git fetch", stderr="fatal: couldn't find remote ref main"
            ),
        ),
        pytest.raises(ReleaseError, match="refusing to release blind") as info,
    ):
        preflight_module._check_main_not_ahead_of_develop()
    assert "couldn't find remote ref main" in (info.value.detail or "")


def test_preflight_divergence_refusal_precedes_every_other_fresh_check() -> None:
    refusal = ReleaseError(phase="preflight", command="c", message="main is ahead")
    with contextlib.ExitStack() as stack:
        for p in _fresh_patches([]):
            stack.enter_context(p)
        stack.enter_context(patch(_MOD + "._check_main_not_ahead_of_develop", side_effect=refusal))
        m_tagged = stack.enter_context(patch(_MOD + "._check_version_not_tagged"))
        m_pkgs = stack.enter_context(patch(_MOD + "._check_packages_unpublished"))
        m_deps = stack.enter_context(patch(_MOD + ".pending_deps_prs"))
        m_wt = stack.enter_context(patch(_MOD + "._acquire_release_worktree"))
        with pytest.raises(ReleaseError, match="main is ahead"):
            preflight(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    m_tagged.assert_not_called()
    m_pkgs.assert_not_called()
    m_deps.assert_not_called()
    m_wt.assert_not_called()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(clone: Path, name: str) -> None:
    (clone / name).write_text(f"{name}\n")
    _git(clone, "add", name)
    _git(clone, "commit", "-qm", name)


def _merge_into(clone: Path, target: str, source: str) -> None:
    """Merge *source* into *target* on origin with a merge commit, as a PR merge does."""
    _git(clone, "checkout", "-q", target)
    _git(clone, "merge", "-q", "--no-ff", "--no-edit", source)
    _git(clone, "push", "-q", "origin", target)


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """An origin one release in: the release merged to main, not yet back-merged.

    Returns ``(root, other)``: the release's root checkout on develop, which
    has not fetched the release yet, and a second clone that plays GitHub.
    """
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(other, "checkout", "-q", "-b", "main")
    _commit(other, "base")
    _git(other, "push", "-q", "origin", "main")
    _git(other, "checkout", "-q", "-b", "develop")
    _commit(other, "feature")
    _git(other, "push", "-q", "-u", "origin", "develop")
    root = tmp_path / "root"
    _git(tmp_path, "clone", "-q", "-b", "develop", str(origin), str(root))
    _git(other, "checkout", "-q", "-b", "release/1.0.0")
    _commit(other, "changelog")
    _git(other, "push", "-q", "origin", "release/1.0.0")
    _merge_into(other, "main", "release/1.0.0")
    monkeypatch.chdir(root)
    return root, other


def _check(root: Path) -> None:
    # The real fetch and log run against the local origin; no GitHub token.
    with patch("vergil_tooling.lib.git.github.get_installation_token", return_value=None):
        _git(root, "fetch", "-q", "origin", "develop")
        preflight_module._check_main_not_ahead_of_develop()


def test_divergence_check_real_git_refuses_a_release_never_back_merged(
    released: tuple[Path, Path],
) -> None:
    root, _other = released
    with pytest.raises(ReleaseError, match="not on origin/develop") as info:
        _check(root)
    assert "Merge branch 'release/1.0.0'" in (info.value.detail or "")


def test_divergence_check_real_git_passes_after_the_back_merge(
    released: tuple[Path, Path],
) -> None:
    """The normal post-release state: main's release merge reached develop."""
    root, other = released
    _git(other, "checkout", "-q", "-b", "release/post-1.0.0", "origin/main")
    _commit(other, "bump")
    _git(other, "push", "-q", "origin", "release/post-1.0.0")
    _merge_into(other, "develop", "release/post-1.0.0")
    # Develop moving on past the release is normal too.
    _commit(other, "next-feature")
    _git(other, "push", "-q", "origin", "develop")
    _check(root)


# -- the develop-CD gate (epic vergil-project/.github#1218, spec §6.6) ---------

_CD = _MOD + ".cd_runs"
_CD_HEAD = "abc123def4567890"
_REPO = "owner/repo"
# The real gate wrapper, kept before the autouse fixture stubs it.
_real_develop_cd_gate = preflight_module._develop_cd_gate


def _cd_run(run_id: int, *, sha: str = _CD_HEAD, created: str = "2026-11-17T10:00:00Z") -> dict:
    return {
        "databaseId": run_id,
        "headSha": sha,
        "status": "completed",
        "conclusion": None,
        "createdAt": created,
        "url": f"https://github.com/{_REPO}/actions/runs/{run_id}",
    }


def _jobs(*failed: str) -> dict:
    jobs = [{"name": "lint / lint", "status": "completed", "conclusion": "success"}]
    jobs += [{"name": name, "status": "completed", "conclusion": "failure"} for name in failed]
    return {"jobs": jobs}


@contextlib.contextmanager
def _gate(
    *,
    run: dict | None,
    states: list[str] | None = None,
    runs: list[dict] | None = None,
    jobs: dict | None = None,
) -> Generator[MagicMock]:
    """Fake the CD lookups the gate makes; yields the ``watch_run`` mock."""
    with (
        patch(_CD + ".find_run", return_value=run) as find,
        patch(_CD + ".watch_run", side_effect=states or []) as watch,
        patch(_CD + "._list_cd_runs", return_value=runs or []),
        patch(_MOD + ".github.read_json", return_value=jobs),
    ):
        yield watch
    if find.called:
        find.assert_called_once_with(_REPO, "develop", _CD_HEAD, dispatch_timeout=120)


def test_develop_cd_gate_green_run_passes() -> None:
    with _gate(run=_cd_run(1), states=["success"]) as watch:
        check_develop_cd(_REPO, _CD_HEAD)
    watch.assert_called_once()
    assert watch.call_args.args == ("1",)


def test_develop_cd_gate_failed_run_refuses() -> None:
    with (
        _gate(run=_cd_run(7), states=["failed"], jobs=_jobs("docker-publish / publish: dev-x")),
        pytest.raises(ReleaseError) as exc,
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    assert exc.value.phase == "preflight"
    assert "https://github.com/owner/repo/actions/runs/7" in str(exc.value)
    assert "docker-publish" in str(exc.value)


def test_develop_cd_gate_queued_run_waits() -> None:
    # The real watch: queued, then pending, then in progress, then green.
    views = [
        {"status": "queued", "conclusion": None, "url": ""},
        {"status": "pending", "conclusion": None, "url": ""},
        {"status": "in_progress", "conclusion": None, "url": ""},
        {"status": "completed", "conclusion": "success", "url": ""},
    ]
    with (
        patch(_CD + ".find_run", return_value=_cd_run(3)),
        patch(_CD + "._view_cd_run", side_effect=views) as view,
        patch(_CD + ".time.sleep") as sleep,
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    assert view.call_count == 4
    assert sleep.call_count == 3


def test_develop_cd_gate_timed_out_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_CD_TIMEOUT", "60")
    with _gate(run=_cd_run(3), states=["timed_out"]), pytest.raises(ReleaseError) as exc:
        check_develop_cd(_REPO, _CD_HEAD)
    assert (
        "The develop CD run is still not complete after 60s; result unknown. "
        "Re-run vrg-release once it finishes, or raise VRG_CD_TIMEOUT. "
        "Run: https://github.com/owner/repo/actions/runs/3"
    ) in str(exc.value)


def test_develop_cd_gate_no_run_uses_green_ancestor() -> None:
    runs = [
        {**_cd_run(9, sha="f" * 40), "status": "in_progress"},
        _cd_run(8, sha="e" * 40),
        _cd_run(5, sha="d" * 40),
    ]
    with (
        _gate(run=None, states=["success"], runs=runs) as watch,
        patch(_MOD + ".git.ref_exists", side_effect=lambda ref: not ref.startswith("e")),
        patch(_MOD + ".git.is_ancestor", return_value=True) as ancestor,
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    # The in-progress run is skipped and the run whose commit is not local is
    # not an ancestor; the next completed run is judged, with no wait.
    ancestor.assert_called_once_with("d" * 40, _CD_HEAD)
    watch.assert_called_once_with("5", timeout=0)


def test_develop_cd_gate_no_run_red_ancestor_refuses() -> None:
    with (
        _gate(
            run=None,
            states=["failed"],
            runs=[_cd_run(5, sha="d" * 40)],
            jobs=_jobs("build / build"),
        ),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.is_ancestor", return_value=True),
        pytest.raises(ReleaseError) as exc,
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    assert "build" in str(exc.value)
    assert "actions/runs/5" in str(exc.value)


def test_develop_cd_gate_no_run_unfinished_ancestor_refuses_without_a_wait() -> None:
    # The ancestor run is judged with no wait, so an unfinished one says so
    # rather than reporting a timeout that never ran.
    with (
        _gate(run=None, states=["timed_out"], runs=[_cd_run(5, sha="d" * 40)]) as watch,
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.is_ancestor", return_value=True),
        pytest.raises(ReleaseError) as exc,
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    watch.assert_called_once_with("5", timeout=0)
    message = str(exc.value)
    assert (
        f"The develop CD run on ancestor {'d' * 12} of {_CD_HEAD[:12]} is not finished; "
        "result unknown. Re-run vrg-release once it finishes. "
        "Run: https://github.com/owner/repo/actions/runs/5"
    ) in message
    assert "after" not in message
    assert "VRG_CD_TIMEOUT" not in message


def test_develop_cd_gate_no_run_and_no_ancestor_refuses() -> None:
    with (
        _gate(run=None, runs=[_cd_run(5, sha="d" * 40)]),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.is_ancestor", return_value=False),
        pytest.raises(ReleaseError, match="none of the recent"),
    ):
        check_develop_cd(_REPO, _CD_HEAD)


def test_develop_cd_gate_deferred_failure_warns(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        _gate(run=_cd_run(4), states=["failed"], jobs=_jobs("docs / docs")),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "docs" in err
    assert "actions/runs/4" in err
    mark_warn.assert_called_once()


def test_develop_cd_gate_superseded_uses_newer() -> None:
    mine = _cd_run(1, created="2026-11-17T10:00:00Z")
    runs = [
        _cd_run(3, sha="c" * 40, created="2026-11-17T10:20:00Z"),
        _cd_run(2, sha="b" * 40, created="2026-11-17T10:10:00Z"),
        mine,
    ]
    with _gate(run=mine, states=["superseded", "success"], runs=runs) as watch:
        check_develop_cd(_REPO, _CD_HEAD)
    assert [call.args for call in watch.call_args_list] == [("1",), ("2",)]


def test_develop_cd_gate_superseded_with_no_newer_run_refuses() -> None:
    mine = _cd_run(1)
    with (
        _gate(run=mine, states=["superseded"], runs=[mine]),
        pytest.raises(ReleaseError, match="no newer develop CD run"),
    ):
        check_develop_cd(_REPO, _CD_HEAD)


def test_develop_cd_gate_other_cancel_refuses() -> None:
    with _gate(run=_cd_run(6), states=["cancelled"]), pytest.raises(ReleaseError) as exc:
        check_develop_cd(_REPO, _CD_HEAD)
    assert "cancelled (not by a concurrency supersede)" in str(exc.value)
    assert "actions/runs/6" in str(exc.value)


@pytest.mark.parametrize(
    "error",
    [
        CDRunLookupError("gh run list failed: HTTP 502"),
        subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 502"),
    ],
)
def test_develop_cd_gate_lookup_failure_refuses(error: Exception) -> None:
    with (
        patch(_CD + ".find_run", side_effect=error),
        pytest.raises(ReleaseError, match="Could not read develop's CD runs") as exc,
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    assert "HTTP 502" in (exc.value.detail or "")


def test_develop_cd_gate_malformed_timeout_refuses_before_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VRG_CD_TIMEOUT", "soon")
    with (
        patch(_CD + ".find_run") as find,
        pytest.raises(ReleaseError, match="VRG_CD_TIMEOUT"),
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    find.assert_not_called()


def _preflight_patches(stack: contextlib.ExitStack, *, real_sync: bool = False) -> None:
    """Stub every preflight check; with *real_sync*, keep the develop sync real."""
    wt = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    for name in (
        "_check_host_prerequisites",
        "_read_and_validate_config",
        *(() if real_sync else ("_check_branch_and_tree",)),
        "_check_version_not_tagged",
        "_check_main_not_ahead_of_develop",
        "_check_no_open_release",
        "_check_packages_unpublished",
        "os.chdir",
    ):
        stack.enter_context(patch(f"{_MOD}.{name}"))
    stack.enter_context(patch(_MOD + ".check_gh_auth", return_value=_REPO))
    stack.enter_context(patch(_MOD + ".version.show", return_value="2.1.0"))
    stack.enter_context(patch(_MOD + ".pending_deps_prs", return_value=[]))
    stack.enter_context(patch(_MOD + "._acquire_release_worktree", return_value=wt))


def test_develop_cd_gate_runs_on_a_fresh_start(develop_cd_gate: MagicMock) -> None:
    with contextlib.ExitStack() as stack:
        _preflight_patches(stack)
        preflight(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    develop_cd_gate.assert_called_once_with(_REPO)


def test_develop_cd_gate_skipped_on_resume(develop_cd_gate: MagicMock) -> None:
    with contextlib.ExitStack() as stack:
        _preflight_patches(stack)
        preflight(version_override=None, repo_root=Path("/tmp/repo"), resume=True)  # noqa: S108
    develop_cd_gate.assert_not_called()


def test_develop_cd_gate_checks_the_local_head() -> None:
    with (
        patch(_MOD + ".git.read_output", return_value=_CD_HEAD) as read,
        patch(_MOD + ".check_develop_cd") as check,
    ):
        _real_develop_cd_gate(_REPO)
    read.assert_called_once_with("rev-parse", "HEAD")
    check.assert_called_once_with(_REPO, _CD_HEAD)


def test_develop_cd_gate_superseded_ancestor_refuses() -> None:
    with (
        _gate(run=None, states=["superseded"], runs=[_cd_run(5, sha="d" * 40)]),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.is_ancestor", return_value=True),
        pytest.raises(ReleaseError, match="superseded by a concurrency group"),
    ):
        check_develop_cd(_REPO, _CD_HEAD)


def test_develop_cd_gate_gives_up_after_repeated_supersedes() -> None:
    runs = [_cd_run(i, created=f"2026-11-17T10:{i:02d}:00Z") for i in range(20)]
    with (
        _gate(run=runs[0], states=["superseded"] * 11, runs=runs) as watch,
        pytest.raises(ReleaseError, match="superseded 11 times in a row"),
    ):
        check_develop_cd(_REPO, _CD_HEAD)
    assert watch.call_count == 11


def test_develop_cd_gate_unreadable_jobs_refuse() -> None:
    with (
        _gate(run=_cd_run(7), states=["failed"], jobs={"jobs": "nope"}),
        pytest.raises(ReleaseError, match="jobs could not be read"),
    ):
        check_develop_cd(_REPO, _CD_HEAD)


# --- The develop sync (_check_branch_and_tree, spec §6.6 of epic #1218) ---

_OLD = "1111111111111111111111111111111111111111"
_NEW = "2222222222222222222222222222222222222222"


class _FakeGit:
    """The root checkout's git, as the develop sync sees it.

    ``head`` is local develop and ``origin`` the fetched origin/develop. A
    ``merge --ff-only`` moves ``head`` to ``origin``. ``locked`` records
    whether the checkout lock of *root* was held at each git call.
    """

    def __init__(
        self,
        root: Path,
        *,
        head: str = _OLD,
        origin: str = _NEW,
        branch: str = "develop",
        status: str = "",
        ancestor: bool = True,
    ) -> None:
        self.root = root
        self.head = head
        self.origin = origin
        self.branch = branch
        self.status = status
        self.ancestor = ancestor
        self.runs: list[tuple[str, ...]] = []
        self.locked: list[bool] = []

    def _record_lock(self) -> None:
        try:
            with checkout_lock(self.root, purpose="probe"):
                self.locked.append(False)
        except CheckoutLockError:
            self.locked.append(True)

    def current_branch(self) -> str:
        self._record_lock()
        return self.branch

    def read_output(self, *args: str) -> str:
        self._record_lock()
        if args == ("status", "--porcelain"):
            return self.status
        if args == ("rev-parse", "HEAD"):
            return self.head
        if args == ("rev-parse", "origin/develop"):
            return self.origin
        msg = f"unexpected git read {args}"
        raise AssertionError(msg)

    def run(self, *args: str) -> None:
        self._record_lock()
        self.runs.append(args)
        if args == ("merge", "--ff-only", "origin/develop"):
            self.head = self.origin

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        self._record_lock()
        assert (ancestor, descendant) == (self.head, self.origin)
        return self.ancestor

    @contextlib.contextmanager
    def patched(self) -> Generator[None]:
        with (
            patch(_MOD + ".git.current_branch", self.current_branch),
            patch(_MOD + ".git.read_output", self.read_output),
            patch(_MOD + ".git.run", self.run),
            patch(_MOD + ".git.is_ancestor", self.is_ancestor),
        ):
            yield


_FETCH = ("fetch", "--tags", "--force", "origin", "develop")
_MERGE = ("merge", "--ff-only", "origin/develop")


def test_preflight_fast_forwards(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = _FakeGit(tmp_path)
    with fake.patched():
        _check_branch_and_tree(tmp_path)
    assert fake.runs == [_FETCH, _MERGE]
    assert fake.head == _NEW
    assert "Fast-forwarded develop 11111111..22222222" in capsys.readouterr().out
    # Every step of the sync ran under the checkout lock.
    assert fake.locked
    assert all(fake.locked)


def test_preflight_up_to_date_no_change(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = _FakeGit(tmp_path, head=_NEW, origin=_NEW)
    with fake.patched():
        _check_branch_and_tree(tmp_path)
    assert fake.runs == [_FETCH]
    assert "Fast-forwarded" not in capsys.readouterr().out


def test_preflight_diverged_refuses(tmp_path: Path) -> None:
    fake = _FakeGit(tmp_path, ancestor=False)
    with (
        fake.patched(),
        pytest.raises(ReleaseError) as excinfo,
    ):
        _check_branch_and_tree(tmp_path)
    assert str(excinfo.value) == (
        "Local develop (11111111) has diverged from origin/develop (22222222); resolve it first."
    )
    assert fake.runs == [_FETCH]
    assert fake.head == _OLD
    # The refusal released the lock.
    with checkout_lock(tmp_path, purpose="after"):
        pass


def test_preflight_dirty_refuses(tmp_path: Path) -> None:
    fake = _FakeGit(tmp_path, status=" M file.py")
    with (
        fake.patched(),
        pytest.raises(ReleaseError, match="not clean") as excinfo,
    ):
        _check_branch_and_tree(tmp_path)
    assert excinfo.value.detail == " M file.py"
    assert fake.runs == []


def test_preflight_wrong_branch_refuses(tmp_path: Path) -> None:
    fake = _FakeGit(tmp_path, branch="main")
    with (
        fake.patched(),
        pytest.raises(ReleaseError, match="Must be on develop"),
    ):
        _check_branch_and_tree(tmp_path)
    assert fake.runs == []


def test_preflight_fetch_keeps_tags(tmp_path: Path) -> None:
    """The fetch still brings remote tags, so the already-tagged check sees them."""
    fake = _FakeGit(tmp_path, head=_NEW, origin=_NEW)
    with fake.patched():
        _check_branch_and_tree(tmp_path)
    assert fake.runs[0] == _FETCH


def test_preflight_holds_lock_only_for_sync(tmp_path: Path, develop_cd_gate: MagicMock) -> None:
    """The lock is free when the develop-CD gate, which may wait, runs."""
    fake = _FakeGit(tmp_path)
    gate_saw_lock: list[bool] = []

    def gate(_repo: str) -> None:
        try:
            with checkout_lock(tmp_path, purpose="gate probe"):
                gate_saw_lock.append(False)
        except CheckoutLockError:
            gate_saw_lock.append(True)

    develop_cd_gate.side_effect = gate
    with contextlib.ExitStack() as stack:
        _preflight_patches(stack, real_sync=True)
        stack.enter_context(fake.patched())
        preflight(version_override=None, repo_root=tmp_path)
    assert all(fake.locked)
    assert gate_saw_lock == [False]


def test_cd_gate_checks_new_head(tmp_path: Path, develop_cd_gate: MagicMock) -> None:
    """The develop-CD gate judges the head preflight fast-forwarded to."""
    fake = _FakeGit(tmp_path)
    develop_cd_gate.side_effect = _real_develop_cd_gate
    with contextlib.ExitStack() as stack:
        _preflight_patches(stack, real_sync=True)
        stack.enter_context(fake.patched())
        checked = stack.enter_context(patch(_MOD + ".check_develop_cd"))
        preflight(version_override=None, repo_root=tmp_path)
    checked.assert_called_once_with(_REPO, _NEW)
