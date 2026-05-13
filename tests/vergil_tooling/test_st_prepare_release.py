"""Tests for standard_tooling.bin.st_prepare_release."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from standard_tooling.bin.st_prepare_release import (
    RELEASE_NOTES_DIR,
    _detect_cargo,
    _detect_claude_plugin,
    _detect_go,
    _detect_maven,
    _detect_python,
    _detect_ruby,
    _detect_version_file,
    _ensure_clean_tree,
    _ensure_develop_up_to_date,
    _ensure_on_develop,
    _generate_release_notes,
    _normalize_trailing_newline,
    detect_ecosystem,
    main,
    parse_args,
)


def test_parse_args() -> None:
    args = parse_args(["--issue", "42"])
    assert args.issue == 42


# -- ecosystem detection tests ---


def test_detect_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "3.1.0"\n')
    assert _detect_python() == "3.1.0"


def test_detect_python_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_python() is None


def test_detect_python_no_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    assert _detect_python() is None


def test_detect_maven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pom.xml").write_text(
        "<project><artifactId>foo</artifactId><version>2.0.0</version></project>"
    )
    assert _detect_maven() == "2.0.0"


def test_detect_maven_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_maven() is None


def test_detect_maven_no_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pom.xml").write_text("<project><groupId>g</groupId></project>")
    assert _detect_maven() is None


def test_detect_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "go.mod").write_text("module example\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "version.go").write_text('package pkg\nconst Version = "1.5.0"\n')
    assert _detect_go() == "1.5.0"


def test_detect_go_no_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_go() is None


def test_detect_go_no_version_in_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "go.mod").write_text("module example\n")
    (tmp_path / "version.go").write_text("package main\n")
    assert _detect_go() is None


def test_detect_ruby(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\n')
    lib = tmp_path / "lib" / "mq" / "rest" / "admin"
    lib.mkdir(parents=True)
    (lib / "version.rb").write_text(
        'module MQ\n  module REST\n    module Admin\n      VERSION = "1.3.0"\n    end\n  end\nend\n'
    )
    assert _detect_ruby() == "1.3.0"


def test_detect_ruby_single_quotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\n')
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "version.rb").write_text("module Foo\n  VERSION = '2.0.0'\nend\n")
    assert _detect_ruby() == "2.0.0"


def test_detect_ruby_no_gemfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_ruby() is None


def test_detect_ruby_no_version_in_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\n')
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "version.rb").write_text("module Foo\nend\n")
    assert _detect_ruby() is None


def test_detect_cargo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "my-crate"\nversion = "1.2.0"\nedition = "2024"\n'
    )
    assert _detect_cargo() == "1.2.0"


def test_detect_cargo_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_cargo() is None


def test_detect_cargo_no_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "my-crate"\n')
    assert _detect_cargo() is None


def test_detect_ecosystem_cargo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "my-crate"\nversion = "1.2.0"\n')
    name, version = detect_ecosystem()
    assert name == "cargo"
    assert version == "1.2.0"


def test_detect_claude_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"name": "test", "version": "0.1.0"}')
    assert _detect_claude_plugin() == "0.1.0"


def test_detect_claude_plugin_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_claude_plugin() is None


def test_detect_claude_plugin_no_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"name": "test"}')
    assert _detect_claude_plugin() is None


def test_detect_ecosystem_claude_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{"name": "test", "version": "0.1.0"}')
    name, version = detect_ecosystem()
    assert name == "claude-plugin"
    assert version == "0.1.0"


def test_detect_version_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("2.3.4\n")
    assert _detect_version_file() == "2.3.4"


def test_detect_version_file_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _detect_version_file() is None


def test_detect_version_file_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("not-a-version\n")
    with pytest.raises(SystemExit, match="not valid semver"):
        _detect_version_file()


def test_detect_ecosystem_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')
    name, version = detect_ecosystem()
    assert name == "python"
    assert version == "1.0.0"


def test_detect_ecosystem_ruby(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\n')
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "version.rb").write_text('module Foo\n  VERSION = "1.0.0"\nend\n')
    name, version = detect_ecosystem()
    assert name == "ruby"
    assert version == "1.0.0"


def test_detect_ecosystem_version_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    name, version = detect_ecosystem()
    assert name == "version-file"
    assert version == "1.0.0"


def test_detect_ecosystem_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="Could not detect ecosystem"):
        detect_ecosystem()


# -- precondition check tests ---


def test_ensure_on_develop_ok() -> None:
    mock = "standard_tooling.bin.st_prepare_release.git.current_branch"
    with patch(mock, return_value="develop"):
        _ensure_on_develop()


def test_ensure_on_develop_wrong_branch() -> None:
    with (
        patch(
            "standard_tooling.bin.st_prepare_release.git.current_branch",
            return_value="feature/x",
        ),
        pytest.raises(SystemExit, match="Must be on develop"),
    ):
        _ensure_on_develop()


def test_ensure_clean_tree_ok() -> None:
    with patch("standard_tooling.bin.st_prepare_release.git.read_output", return_value=""):
        _ensure_clean_tree()


def test_ensure_clean_tree_dirty() -> None:
    with (
        patch("standard_tooling.bin.st_prepare_release.git.read_output", return_value="M file.py"),
        pytest.raises(SystemExit, match="Working tree is not clean"),
    ):
        _ensure_clean_tree()


def test_ensure_develop_up_to_date_ok() -> None:
    with (
        patch("standard_tooling.bin.st_prepare_release.git.run"),
        patch("standard_tooling.bin.st_prepare_release.git.read_output", return_value="abc123"),
    ):
        _ensure_develop_up_to_date()


def test_ensure_develop_up_to_date_diverged() -> None:
    call_count = 0

    def mock_read_output(*args: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "abc123"
        return "def456"

    with (
        patch("standard_tooling.bin.st_prepare_release.git.run"),
        patch(
            "standard_tooling.bin.st_prepare_release.git.read_output",
            side_effect=mock_read_output,
        ),
        pytest.raises(SystemExit, match="does not match"),
    ):
        _ensure_develop_up_to_date()


# -- main flow tests ---


def _smart_read_output(status_return: str = "M CHANGELOG.md") -> object:
    """Return a mock for git.read_output that handles all call patterns in main()."""

    def _mock(*args: str) -> str:
        if args[0] == "status":
            return status_return
        if args[0] == "rev-parse":
            return "abc123"
        return ""

    return _mock


def test_main_full_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')

    # _ensure_clean_tree reads status --porcelain, needs "" first time, then
    # _generate_changelog reads it again, needs non-empty second time
    status_calls = 0

    def mock_read_output(*args: str) -> str:
        nonlocal status_calls
        if args[0] == "status":
            status_calls += 1
            if status_calls == 1:
                return ""  # _ensure_clean_tree: clean
            return "M CHANGELOG.md"  # _generate_changelog: has changes
        if args[0] == "rev-parse":
            return "abc123"
        return ""

    def mock_subprocess_run(
        cmd: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "git-cliff" in cmd and "-o" in cmd:
            output_idx = list(cmd).index("-o") + 1
            output_path = Path(cmd[output_idx])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("# Content\n")
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")

    with (
        patch(
            "standard_tooling.bin.st_prepare_release.git.current_branch",
            return_value="develop",
        ),
        patch("standard_tooling.bin.st_prepare_release.git.run"),
        patch(
            "standard_tooling.bin.st_prepare_release.git.read_output",
            side_effect=mock_read_output,
        ),
        patch("standard_tooling.bin.st_prepare_release.git.ref_exists", return_value=False),
        patch(
            "standard_tooling.bin.st_prepare_release.subprocess.run",
            side_effect=mock_subprocess_run,
        ),
        patch(
            "standard_tooling.bin.st_prepare_release.github.create_pr",
            return_value="https://github.com/pr/1",
        ),
    ):
        result = main(["--issue", "42"])
    assert result == 0


def test_main_release_branch_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')

    def mock_read_output(*args: str) -> str:
        if args[0] == "status":
            return ""
        if args[0] == "rev-parse":
            return "abc123"
        return ""

    with (
        patch(
            "standard_tooling.bin.st_prepare_release.git.current_branch",
            return_value="develop",
        ),
        patch("standard_tooling.bin.st_prepare_release.git.run"),
        patch(
            "standard_tooling.bin.st_prepare_release.git.read_output",
            side_effect=mock_read_output,
        ),
        patch("standard_tooling.bin.st_prepare_release.git.ref_exists", return_value=True),
        pytest.raises(SystemExit, match="already exists"),
    ):
        main(["--issue", "42"])


def test_main_no_publishable_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')

    def mock_read_output(*args: str) -> str:
        if args[0] == "status":
            return ""  # both times: clean tree + no changes after changelog
        if args[0] == "rev-parse":
            return "abc123"
        return ""

    def mock_subprocess_run(
        cmd: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "git-cliff" in cmd and "-o" in cmd:
            output_idx = list(cmd).index("-o") + 1
            output_path = Path(cmd[output_idx])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("# Content\n")
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")

    with (
        patch(
            "standard_tooling.bin.st_prepare_release.git.current_branch",
            return_value="develop",
        ),
        patch("standard_tooling.bin.st_prepare_release.git.run"),
        patch(
            "standard_tooling.bin.st_prepare_release.git.read_output",
            side_effect=mock_read_output,
        ),
        patch("standard_tooling.bin.st_prepare_release.git.ref_exists", return_value=False),
        patch(
            "standard_tooling.bin.st_prepare_release.subprocess.run",
            side_effect=mock_subprocess_run,
        ),
        pytest.raises(SystemExit, match="No publishable changes"),
    ):
        main(["--issue", "42"])


# -- helper function tests ---


def test_normalize_trailing_newline(tmp_path: Path) -> None:
    p = tmp_path / "test.md"
    p.write_text("hello\n\n\n")
    _normalize_trailing_newline(p)
    assert p.read_text() == "hello\n"


def test_normalize_trailing_newline_no_newline(tmp_path: Path) -> None:
    p = tmp_path / "test.md"
    p.write_text("hello")
    _normalize_trailing_newline(p)
    assert p.read_text() == "hello\n"


# -- release notes tests ---


def test_generate_release_notes_creates_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    captured_cmds: list[tuple[str, ...]] = []

    def mock_subprocess_run(
        cmd: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured_cmds.append(tuple(cmd))
        if "git-cliff" in cmd:
            output_idx = list(cmd).index("-o") + 1
            Path(cmd[output_idx]).write_text("# Release 1.0.0\n")
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")

    with (
        patch(
            "standard_tooling.bin.st_prepare_release.subprocess.run",
            side_effect=mock_subprocess_run,
        ),
        patch("standard_tooling.bin.st_prepare_release.git.run"),
    ):
        _generate_release_notes("1.0.0", "develop-v1.0.0")

    assert (tmp_path / RELEASE_NOTES_DIR / "v1.0.0.md").is_file()
    assert (tmp_path / RELEASE_NOTES_DIR / "v1.0.0.md").read_text() == "# Release 1.0.0\n"

    cliff_cmd = next(c for c in captured_cmds if "git-cliff" in c)
    assert "--unreleased" in cliff_cmd
    assert "--latest" not in cliff_cmd
    assert "--config" in cliff_cmd


def test_main_full_flow_with_release_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('version = "1.0.0"\n')

    status_calls = 0

    def mock_read_output(*args: str) -> str:
        nonlocal status_calls
        if args[0] == "status":
            status_calls += 1
            if status_calls == 1:
                return ""
            return "M CHANGELOG.md"
        if args[0] == "rev-parse":
            return "abc123"
        return ""

    def mock_subprocess_run(
        cmd: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "git-cliff" in cmd and "-o" in cmd:
            output_idx = list(cmd).index("-o") + 1
            output_path = Path(cmd[output_idx])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("# Content\n")
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")

    with (
        patch(
            "standard_tooling.bin.st_prepare_release.git.current_branch",
            return_value="develop",
        ),
        patch("standard_tooling.bin.st_prepare_release.git.run"),
        patch(
            "standard_tooling.bin.st_prepare_release.git.read_output",
            side_effect=mock_read_output,
        ),
        patch("standard_tooling.bin.st_prepare_release.git.ref_exists", return_value=False),
        patch(
            "standard_tooling.bin.st_prepare_release.subprocess.run",
            side_effect=mock_subprocess_run,
        ),
        patch(
            "standard_tooling.bin.st_prepare_release.github.create_pr",
            return_value="https://github.com/pr/1",
        ),
    ):
        result = main(["--issue", "42"])
    assert result == 0
    assert (tmp_path / RELEASE_NOTES_DIR / "v1.0.0.md").is_file()
