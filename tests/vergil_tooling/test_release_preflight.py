from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.preflight import (
    _apply_version_override,
    _audit_repo_config,
    _check_branch_and_tree,
    _detect_cargo,
    _detect_claude_plugin,
    _detect_go,
    _detect_maven,
    _detect_python,
    _detect_ruby,
    _detect_version,
    _detect_version_file,
    preflight,
)

if TYPE_CHECKING:
    from pathlib import Path

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


def test_check_branch_and_tree_fails_if_not_develop() -> None:
    with (
        patch(_MOD + ".git.current_branch", return_value="main"),
        pytest.raises(ReleaseError, match="develop"),
    ):
        _check_branch_and_tree()


def test_check_branch_and_tree_fails_if_dirty() -> None:
    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", return_value="M file.py"),
        pytest.raises(ReleaseError, match="not clean"),
    ):
        _check_branch_and_tree()


def test_check_branch_and_tree_fails_if_behind() -> None:
    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", side_effect=["", "abc1234", "def5678"]),
        patch(_MOD + ".git.run"),
        pytest.raises(ReleaseError, match="does not match"),
    ):
        _check_branch_and_tree()


def test_check_branch_and_tree_passes() -> None:
    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", side_effect=["", "abc1234", "abc1234"]),
        patch(_MOD + ".git.run"),
    ):
        _check_branch_and_tree()


def test_audit_repo_config_fails() -> None:
    from subprocess import CompletedProcess

    with (
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=1, stdout="fail", stderr="err"),
        ),
        pytest.raises(ReleaseError, match="non-compliant"),
    ):
        _audit_repo_config("owner/repo")


def test_audit_repo_config_passes() -> None:
    from subprocess import CompletedProcess

    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0),
    ):
        _audit_repo_config("owner/repo")


def test_detect_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.2.3"\n')
    assert _detect_python() == "1.2.3"


def test_detect_python_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_python() is None


def test_detect_maven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pom.xml").write_text(
        "<project><artifactId>foo</artifactId><version>3.0.0</version></project>"
    )
    assert _detect_maven() == "3.0.0"


def test_detect_maven_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_maven() is None


def test_detect_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    (tmp_path / "version.go").write_text('const Version = "4.5.6"\n')
    assert _detect_go() == "4.5.6"


def test_detect_go_no_version_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    (tmp_path / "version.go").write_text("package main\n")
    assert _detect_go() is None


def test_detect_go_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_go() is None


def test_detect_ruby(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "version.rb").write_text("VERSION = '7.8.9'\n")
    assert _detect_ruby() == "7.8.9"


def test_detect_ruby_no_version_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "version.rb").write_text("module Foo\nend\n")
    assert _detect_ruby() is None


def test_detect_ruby_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_ruby() is None


def test_detect_cargo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[package]\nversion = "0.1.0"\n')
    assert _detect_cargo() == "0.1.0"


def test_detect_cargo_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_cargo() is None


def test_detect_claude_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"version": "1.0.0"}')
    assert _detect_claude_plugin() == "1.0.0"


def test_detect_claude_plugin_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_claude_plugin() is None


def test_detect_version_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("5.6.7\n")
    assert _detect_version_file() == "5.6.7"


def test_detect_version_file_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("not-semver\n")
    with pytest.raises(ReleaseError, match="not valid semver"):
        _detect_version_file()


def test_detect_version_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_version_file() is None


def test_detect_version_none_found(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="Could not detect"):
        _detect_version(tmp_path)


def test_preflight_with_version_override(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="owner/repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".git.read_output", return_value="v2.0.0"),
        patch(_MOD + ".find_existing_tracking_issue", return_value=None),
        patch(_MOD + "._apply_version_override", return_value="2.2.0"),
    ):
        ctx = preflight(version_override="minor", repo_root=_repo)
    assert ctx.version == "2.2.0"


def test_preflight_version_override_minor(_repo: Path) -> None:
    from vergil_tooling.lib import config

    cfg = config.read_config(_repo)
    with (
        patch(_MOD + ".subprocess.run"),
        patch(_MOD + ".git.run"),
    ):
        result = _apply_version_override(_repo, "2.1.0", "minor", cfg)
    assert result == "2.2.0"


def test_preflight_version_override_major(_repo: Path) -> None:
    from vergil_tooling.lib import config

    cfg = config.read_config(_repo)
    with (
        patch(_MOD + ".subprocess.run"),
        patch(_MOD + ".git.run"),
    ):
        result = _apply_version_override(_repo, "2.1.0", "major", cfg)
    assert result == "3.0.0"


def test_preflight_version_override_bad_semver(_repo: Path) -> None:
    from vergil_tooling.lib import config

    cfg = config.read_config(_repo)
    with pytest.raises(ReleaseError, match="not valid semver"):
        _apply_version_override(_repo, "bad", "minor", cfg)


def test_preflight_version_override_unsupported_language(_repo: Path) -> None:
    from vergil_tooling.lib import config

    toml = _valid_toml().replace('primary-language = "python"', 'primary-language = "go"')
    (_repo / "vergil.toml").write_text(toml)
    cfg = config.read_config(_repo)
    with pytest.raises(ReleaseError, match="not yet implemented"):
        _apply_version_override(_repo, "2.1.0", "minor", cfg)
