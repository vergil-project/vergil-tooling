"""Tests for vergil_tooling.lib.version."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.version import bump, show, show_major_minor

if TYPE_CHECKING:
    from pathlib import Path


# -- Fixture helpers ----------------------------------------------------------


def _write_toml(tmp_path: Path, language: str) -> None:
    (tmp_path / "vergil.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        f'primary-language = "{language}"\n\n[dependencies]\nvergil = "v2.0"\n'
        f'\n[ci]\nversions = ["3.14"]\n'
    )


# -- show() tests ------------------------------------------------------------


def test_show_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.2.3"\n')
    assert show(tmp_path) == "1.2.3"


def test_show_generic_version_file(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("2.0.1\n")
    assert show(tmp_path) == "2.0.1"


def test_show_rust(tmp_path: Path) -> None:
    _write_toml(tmp_path, "rust")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "example"\nversion = "0.3.7"\n')
    assert show(tmp_path) == "0.3.7"


def test_show_ruby(tmp_path: Path) -> None:
    _write_toml(tmp_path, "ruby")
    version_dir = tmp_path / "lib" / "mq" / "rest" / "admin"
    version_dir.mkdir(parents=True)
    (version_dir / "version.rb").write_text("  VERSION = '4.1.0'\n")
    assert show(tmp_path) == "4.1.0"


def test_show_go(tmp_path: Path) -> None:
    _write_toml(tmp_path, "go")
    pkg_dir = tmp_path / "mqrestadmin"
    pkg_dir.mkdir()
    (pkg_dir / "version.go").write_text('package mqrestadmin\n\nVersion = "1.0.5"\n')
    assert show(tmp_path) == "1.0.5"


def test_show_java(tmp_path: Path) -> None:
    _write_toml(tmp_path, "java")
    (tmp_path / "pom.xml").write_text("<project>\n  <version>3.2.1</version>\n</project>\n")
    assert show(tmp_path) == "3.2.1"


def test_show_claude_plugin(tmp_path: Path) -> None:
    _write_toml(tmp_path, "claude-plugin")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{\n  "name": "example",\n  "version": "1.4.19"\n}\n')
    assert show(tmp_path) == "1.4.19"


# -- show_major_minor() tests ------------------------------------------------


def test_show_major_minor(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.5.2\n")
    assert show_major_minor(tmp_path) == "1.5"


# -- version_file override ---------------------------------------------------


def test_show_with_version_file_override(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        'primary-language = "shell"\nversion-file = "custom/VERSION"\n\n'
        '[dependencies]\nvergil = "v2.0"\n'
        '\n[ci]\nversions = ["3.14"]\n'
    )
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    (custom_dir / "VERSION").write_text("9.8.7\n")
    assert show(tmp_path) == "9.8.7"


# -- error cases --------------------------------------------------------------


def test_show_missing_version_file(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    with pytest.raises(FileNotFoundError):
        show(tmp_path)


# -- show_ref() tests --------------------------------------------------------


def test_show_ref_reads_via_git(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("3.0.0\n")

    with patch("vergil_tooling.lib.version._read_version_from_ref") as mock:
        mock.return_value = "2.9.0"
        result = show(tmp_path, ref="origin/main")
    assert result == "2.9.0"
    mock.assert_called_once()


# -- bump() tests ------------------------------------------------------------


def test_bump_generic(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    result = bump(tmp_path)
    assert result == "1.2.4"
    assert (tmp_path / "VERSION").read_text().strip() == "1.2.4"


def test_bump_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "2.0.0"\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "2.0.1"
    text = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "2.0.1"' in text


def test_bump_rust(tmp_path: Path) -> None:
    _write_toml(tmp_path, "rust")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "example"\nversion = "0.3.7"\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "0.3.8"
    text = (tmp_path / "Cargo.toml").read_text()
    assert 'version = "0.3.8"' in text


def test_bump_ruby(tmp_path: Path) -> None:
    _write_toml(tmp_path, "ruby")
    version_dir = tmp_path / "lib" / "mq"
    version_dir.mkdir(parents=True)
    (version_dir / "version.rb").write_text("  VERSION = '1.0.0'\n")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "1.0.1"
    text = (version_dir / "version.rb").read_text()
    assert "VERSION = '1.0.1'" in text


def test_bump_go(tmp_path: Path) -> None:
    _write_toml(tmp_path, "go")
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "version.go").write_text('package pkg\n\nVersion = "1.0.5"\n')
    result = bump(tmp_path)
    assert result == "1.0.6"
    text = (pkg_dir / "version.go").read_text()
    assert 'Version = "1.0.6"' in text


def test_bump_java(tmp_path: Path) -> None:
    _write_toml(tmp_path, "java")
    (tmp_path / "pom.xml").write_text("<project>\n  <version>3.2.1</version>\n</project>\n")
    result = bump(tmp_path)
    assert result == "3.2.2"
    text = (tmp_path / "pom.xml").read_text()
    assert "<version>3.2.2</version>" in text


def test_bump_claude_plugin(tmp_path: Path) -> None:
    _write_toml(tmp_path, "claude-plugin")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{\n  "name": "example",\n  "version": "1.4.19"\n}\n')
    result = bump(tmp_path)
    assert result == "1.4.20"
    text = (plugin_dir / "plugin.json").read_text()
    assert '"version": "1.4.20"' in text
    assert '"name": "example"' in text


# -- lockfile maintenance tests -----------------------------------------------


def test_bump_python_runs_uv_lock(tmp_path: Path) -> None:
    import subprocess as _sp

    _write_toml(tmp_path, "python")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.0.0"\n')
    cp = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("vergil_tooling.lib.version.subprocess.run", return_value=cp) as mock_run:
        bump(tmp_path)
        mock_run.assert_called_once_with(
            ["uv", "lock"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )


def test_bump_rust_runs_cargo_update(tmp_path: Path) -> None:
    _write_toml(tmp_path, "rust")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "example"\nversion = "0.1.0"\n')
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        bump(tmp_path)
        mock_run.assert_called_once_with(
            ["cargo", "update", "--workspace"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )


def test_bump_ruby_runs_bundle_install(tmp_path: Path) -> None:
    _write_toml(tmp_path, "ruby")
    version_dir = tmp_path / "lib" / "mq"
    version_dir.mkdir(parents=True)
    (version_dir / "version.rb").write_text("  VERSION = '1.0.0'\n")
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        bump(tmp_path)
        mock_run.assert_called_once_with(
            ["bundle", "install"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )


def test_bump_generic_skips_lockfile(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        bump(tmp_path)
        mock_run.assert_not_called()


def test_bump_claude_plugin_skips_lockfile(tmp_path: Path) -> None:
    _write_toml(tmp_path, "claude-plugin")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{\n  "name": "example",\n  "version": "1.0.0"\n}\n')
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        bump(tmp_path)
        mock_run.assert_not_called()


# -- _discover_version_file error paths ----------------------------------------


def test_discover_ruby_no_match(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _discover_version_file

    with pytest.raises(FileNotFoundError, match="No lib"):
        _discover_version_file(tmp_path, "ruby")


def test_discover_go_no_match(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _discover_version_file

    with pytest.raises(FileNotFoundError, match="No .*/version.go"):
        _discover_version_file(tmp_path, "go")


def test_discover_unsupported_language(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _discover_version_file

    with pytest.raises(ValueError, match="Unsupported language"):
        _discover_version_file(tmp_path, "fortran")


# -- _read_version error paths -------------------------------------------------


def test_read_version_ruby_bad_format() -> None:
    from vergil_tooling.lib.version import _read_version

    with pytest.raises(ValueError, match="No VERSION"):
        _read_version("no version here", "ruby")


def test_read_version_go_bad_format() -> None:
    from vergil_tooling.lib.version import _read_version

    with pytest.raises(ValueError, match="No Version"):
        _read_version("no version here", "go")


def test_read_version_java_bad_format() -> None:
    from vergil_tooling.lib.version import _read_version

    with pytest.raises(ValueError, match="No <version>"):
        _read_version("no version here", "java")


def test_read_version_claude_plugin_missing_key() -> None:
    from vergil_tooling.lib.version import _read_version

    with pytest.raises(ValueError, match="No 'version' key"):
        _read_version('{"name": "example"}', "claude-plugin")


# -- _read_version_from_ref body -----------------------------------------------


def test_read_version_from_ref_body(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _read_version_from_ref

    with patch(
        "vergil_tooling.lib.version.subprocess.run",
        return_value=__import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="1.2.3\n"
        ),
    ):
        result = _read_version_from_ref("origin/main", "VERSION", "shell")
    assert result == "1.2.3"
