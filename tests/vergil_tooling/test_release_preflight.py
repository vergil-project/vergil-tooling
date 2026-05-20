from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.preflight import preflight

_MOD = "vergil_tooling.lib.release.preflight"


def _valid_toml() -> str:
    return (
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


@pytest.fixture()
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vergil.toml").write_text(_valid_toml())
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "2.1.0"\n')
    return tmp_path


def test_preflight_fails_if_git_cliff_missing(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value=None),
        pytest.raises(ReleaseError, match="git-cliff"),
    ):
        preflight(
            version_override=None,
            repo_root=_repo,
        )


def test_preflight_fails_if_gh_auth_fails(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(
            _MOD + ".github.read_output",
            side_effect=Exception("not authenticated"),
        ),
        pytest.raises(ReleaseError, match="GitHub CLI"),
    ):
        preflight(
            version_override=None,
            repo_root=_repo,
        )


def test_preflight_fails_if_not_library_or_tooling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    toml = _valid_toml().replace(
        'repository-type = "library"',
        'repository-type = "documentation"',
    )
    (tmp_path / "vergil.toml").write_text(toml)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "1.0.0"\n')
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="test-repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        pytest.raises(ReleaseError, match="repository_type"),
    ):
        preflight(version_override=None, repo_root=tmp_path)


def test_preflight_fails_if_version_matches_tag(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="test-repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(
            _MOD + ".git.read_output",
            return_value="v2.1.0",
        ),
        patch(_MOD + ".find_existing_tracking_issue", return_value=None),
        pytest.raises(ReleaseError, match="already tagged"),
    ):
        preflight(version_override=None, repo_root=_repo)


def test_preflight_returns_context(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="owner/repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".git.read_output", return_value="v2.0.0"),
        patch(_MOD + ".find_existing_tracking_issue", return_value=None),
    ):
        ctx = preflight(version_override=None, repo_root=_repo)
    assert ctx.repo == "owner/repo"
    assert ctx.version == "2.1.0"
    assert ctx.repo_root == _repo


def test_preflight_fails_if_tracking_issue_exists(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="owner/repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".git.read_output", return_value="v2.0.0"),
        patch(
            _MOD + ".find_existing_tracking_issue",
            return_value="https://github.com/owner/repo/issues/50",
        ),
        pytest.raises(ReleaseError, match="tracking issue already exists"),
    ):
        preflight(version_override=None, repo_root=_repo)
