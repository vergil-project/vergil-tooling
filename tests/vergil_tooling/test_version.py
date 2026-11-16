"""Tests for vergil_tooling.lib.version."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.version import (
    CargoVersionError,
    VersionSyncError,
    bump,
    bump_paths,
    cargo_version_table,
    next_version,
    show,
    show_major_minor,
)

if TYPE_CHECKING:
    from pathlib import Path


# -- Fixture helpers ----------------------------------------------------------


def _write_toml(tmp_path: Path, language: str | None = None) -> None:
    lang_line = f'primary-language = "{language}"\n' if language else ""
    (tmp_path / "vergil.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        f'{lang_line}\n[dependencies]\nvergil = "v2.0"\n'
        f'\n[ci]\nversions = ["3.14"]\n'
    )


# -- show() tests ------------------------------------------------------------


def test_show_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.2.3"\n')
    assert show(tmp_path) == "1.2.3"


def test_show_generic_version_file(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("2.0.1\n")
    assert show(tmp_path) == "2.0.1"


def test_show_rust(tmp_path: Path) -> None:
    _write_toml(tmp_path, "rust")
    (tmp_path / "VERSION").write_text("0.3.7\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "example"\nversion = "0.3.7"\n')
    assert show(tmp_path) == "0.3.7"


def test_show_go(tmp_path: Path) -> None:
    _write_toml(tmp_path, "go")
    (tmp_path / "VERSION").write_text("1.0.5\n")
    pkg_dir = tmp_path / "mqrestadmin"
    pkg_dir.mkdir()
    (pkg_dir / "version.go").write_text('package mqrestadmin\n\nVersion = "1.0.5"\n')
    assert show(tmp_path) == "1.0.5"


def test_show_claude_plugin(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.19\n")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{\n  "name": "example",\n  "version": "1.4.19"\n}\n')
    assert show(tmp_path) == "1.4.19"


# -- show_major_minor() tests ------------------------------------------------


def test_show_major_minor(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.5.2\n")
    assert show_major_minor(tmp_path) == "1.5"


# -- cross-check tests -------------------------------------------------------


def test_show_cross_checks_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.2.3"\n')
    assert show(tmp_path) == "1.2.3"


def test_show_mismatch_raises(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "9.9.9"\n')
    with pytest.raises(
        VersionSyncError,
        match="VERSION contains 1.2.3 but pyproject.toml contains 9.9.9",
    ):
        show(tmp_path)


def test_show_secondary_file_mismatch_raises(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{\n  "name": "example",\n  "version": "9.9.9"\n}\n')
    with pytest.raises(
        VersionSyncError,
        match="VERSION contains 1.0.0 but .claude-plugin/plugin.json contains 9.9.9",
    ):
        show(tmp_path)


def test_show_missing_language_file_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    result = show(tmp_path)
    assert result == "0.1.0"
    err = capsys.readouterr().err
    assert "not found" in err


def test_show_no_language_skips_cross_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("2.0.1\n")
    assert show(tmp_path) == "2.0.1"
    err = capsys.readouterr().err
    assert err == ""


def test_show_missing_version_file_raises(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.0.0"\n')
    with pytest.raises(FileNotFoundError, match="VERSION"):
        show(tmp_path)


def test_show_ref_reads_version_file_no_cross_check(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.0.0\n")
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="1.9.0\n"
        )
        result = show(tmp_path, ref="origin/main")
    assert result == "1.9.0"
    mock_run.assert_called_once_with(
        ["git", "show", "origin/main:VERSION"],
        capture_output=True,
        text=True,
        check=True,
    )


def test_show_cross_check_warns_when_discover_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_toml(tmp_path, "go")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    result = show(tmp_path)
    assert result == "1.0.0"
    err = capsys.readouterr().err
    assert "not found" in err


# -- bump() tests ------------------------------------------------------------


def test_bump_missing_version_file_raises(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    with pytest.raises(FileNotFoundError, match="VERSION"):
        bump(tmp_path)


def test_bump_generic(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    result = bump(tmp_path)
    assert result == "1.2.4"
    assert (tmp_path / "VERSION").read_text().strip() == "1.2.4"


def test_bump_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.0.0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "2.0.0"\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "2.0.1"
    assert (tmp_path / "VERSION").read_text().strip() == "2.0.1"
    text = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "2.0.1"' in text


def test_bump_rust(tmp_path: Path) -> None:
    _write_toml(tmp_path, "rust")
    (tmp_path / "VERSION").write_text("0.3.7\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "example"\nversion = "0.3.7"\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "0.3.8"
    assert (tmp_path / "VERSION").read_text().strip() == "0.3.8"
    text = (tmp_path / "Cargo.toml").read_text()
    assert 'version = "0.3.8"' in text


def test_bump_go(tmp_path: Path) -> None:
    _write_toml(tmp_path, "go")
    (tmp_path / "VERSION").write_text("1.0.5\n")
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "version.go").write_text('package pkg\n\nVersion = "1.0.5"\n')
    result = bump(tmp_path)
    assert result == "1.0.6"
    assert (tmp_path / "VERSION").read_text().strip() == "1.0.6"
    text = (pkg_dir / "version.go").read_text()
    assert 'Version = "1.0.6"' in text


def test_bump_claude_plugin(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.19\n")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{\n  "name": "example",\n  "version": "1.4.19"\n}\n')
    result = bump(tmp_path)
    assert result == "1.4.20"
    assert (tmp_path / "VERSION").read_text().strip() == "1.4.20"
    text = (plugin_dir / "plugin.json").read_text()
    assert '"version": "1.4.20"' in text
    assert '"name": "example"' in text


# -- lockfile maintenance tests -----------------------------------------------


def test_bump_python_runs_uv_lock_in_container(tmp_path: Path) -> None:
    import subprocess as _sp

    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.0.0"\n')
    cp = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("vergil_tooling.lib.version.subprocess.run", return_value=cp) as mock_run:
        bump(tmp_path)
        mock_run.assert_called_once_with(
            ["vrg-container-run", "--", "uv", "lock"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )


def test_bump_rust_runs_cargo_update_in_container(tmp_path: Path) -> None:
    """The Rust lock refresh needs no host cargo: it runs in the dev container (#4407)."""
    _write_toml(tmp_path, "rust")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "example"\nversion = "0.1.0"\n')
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        bump(tmp_path)
        mock_run.assert_called_once_with(
            ["vrg-container-run", "--", "cargo", "update", "--workspace"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )


def test_bump_rust_lockfile_refresh_runs_after_cargo_toml_bump(tmp_path: Path) -> None:
    """The container sees the bumped Cargo.toml, so cargo writes the new version."""
    _write_toml(tmp_path, "rust")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    cargo_toml = tmp_path / "Cargo.toml"
    cargo_toml.write_text('[package]\nname = "example"\nversion = "0.1.0"\n')
    seen: list[str] = []

    def _record(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(cargo_toml.read_text())
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

    with patch("vergil_tooling.lib.version.subprocess.run", side_effect=_record):
        bump(tmp_path)
    assert len(seen) == 1
    assert 'version = "0.1.1"' in seen[0]


def test_lockfile_refresh_no_container_runtime_is_loud(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """vrg-container-run's refusal with no runtime stops the bump and is shown."""
    import subprocess as _sp

    from vergil_tooling.lib.version import _run_lockfile_maintenance

    err = _sp.CalledProcessError(1, ["vrg-container-run", "--", "cargo", "update", "--workspace"])
    err.stdout = ""
    err.stderr = "ERROR: no container runtime found (need docker or nerdctl)\n"
    with (
        patch("vergil_tooling.lib.version.subprocess.run", side_effect=err),
        pytest.raises(_sp.CalledProcessError) as excinfo,
    ):
        _run_lockfile_maintenance(tmp_path, "rust")
    assert excinfo.value.cmd[:2] == ["vrg-container-run", "--"]
    assert "no container runtime found" in capsys.readouterr().err


def test_lockfile_refresh_failure_shows_container_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """vrg-container-run's header (image, command) is on stdout: show it on failure."""
    import subprocess as _sp

    from vergil_tooling.lib.version import _run_lockfile_maintenance

    err = _sp.CalledProcessError(101, ["vrg-container-run", "--", "cargo", "update"])
    err.stdout = "Image:    ghcr.io/vergil-project/prod-rust:1.90\n"
    err.stderr = "error: failed to load manifest\n"
    with (
        patch("vergil_tooling.lib.version.subprocess.run", side_effect=err),
        pytest.raises(_sp.CalledProcessError),
    ):
        _run_lockfile_maintenance(tmp_path, "rust")
    captured = capsys.readouterr()
    assert "prod-rust" in captured.out
    assert "failed to load manifest" in captured.err


def test_lockfile_refresh_success_relays_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess as _sp

    from vergil_tooling.lib.version import _run_lockfile_maintenance

    cp = _sp.CompletedProcess(
        args=[], returncode=0, stdout="Language: rust\n", stderr="Updating example\n"
    )
    with patch("vergil_tooling.lib.version.subprocess.run", return_value=cp):
        _run_lockfile_maintenance(tmp_path, "rust")
    captured = capsys.readouterr()
    assert captured.out == "Language: rust\n"
    assert captured.err == "Updating example\n"


def test_bump_generic_skips_lockfile(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        bump(tmp_path)
        mock_run.assert_not_called()


def test_bump_skips_missing_language_file_discover_raises(tmp_path: Path) -> None:
    _write_toml(tmp_path, "go")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "1.0.1"
    assert (tmp_path / "VERSION").read_text().strip() == "1.0.1"


def test_bump_skips_nonexistent_language_file(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "1.0.1"
    assert (tmp_path / "VERSION").read_text().strip() == "1.0.1"
    assert not (tmp_path / "pyproject.toml").exists()


def test_bump_claude_plugin_skips_lockfile(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text('{\n  "name": "example",\n  "version": "1.0.0"\n}\n')
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        bump(tmp_path)
        mock_run.assert_not_called()


# -- _discover_version_file error paths ----------------------------------------


def test_discover_go_no_match(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _discover_version_file

    with pytest.raises(FileNotFoundError, match="No .*/version.go"):
        _discover_version_file(tmp_path, "go")


def test_discover_unsupported_language(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _discover_version_file

    with pytest.raises(ValueError, match="Unsupported language"):
        _discover_version_file(tmp_path, "fortran")


# -- _read_version error paths -------------------------------------------------


def test_read_version_go_bad_format() -> None:
    from vergil_tooling.lib.version import _read_version

    with pytest.raises(ValueError, match="No Version"):
        _read_version("no version here", "go")


def test_read_version_claude_plugin_missing_key() -> None:
    from vergil_tooling.lib.version import _read_version

    with pytest.raises(ValueError, match="No 'version' key"):
        _read_version('{"name": "example"}', "claude-plugin")


# -- _write_version fallback ---------------------------------------------------


def test_write_version_fallback(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _write_version

    f = tmp_path / "VERSION"
    f.write_text("1.0.0\n")
    _write_version(f, "plaintext", "1.0.0", "1.0.1")
    assert f.read_text() == "1.0.1\n"


# -- _read_version_from_ref body -----------------------------------------------


def test_read_version_from_ref_body(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _read_version_from_ref

    with patch(
        "vergil_tooling.lib.version.subprocess.run",
        return_value=__import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="1.2.3\n"
        ),
    ):
        result = _read_version_from_ref("origin/main", "VERSION", "plaintext")
    assert result == "1.2.3"


# -- bump() part parameter tests ----------------------------------------------


def test_bump_minor_generic(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    result = bump(tmp_path, "minor")
    assert result == "1.3.0"
    assert (tmp_path / "VERSION").read_text().strip() == "1.3.0"


def test_bump_major_generic(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    result = bump(tmp_path, "major")
    assert result == "2.0.0"
    assert (tmp_path / "VERSION").read_text().strip() == "2.0.0"


def test_bump_minor_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.0.5\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "2.0.5"\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path, "minor")
    assert result == "2.1.0"
    assert (tmp_path / "VERSION").read_text().strip() == "2.1.0"
    text = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "2.1.0"' in text


def test_bump_major_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.5.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "2.5.3"\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path, "major")
    assert result == "3.0.0"
    assert (tmp_path / "VERSION").read_text().strip() == "3.0.0"
    text = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "3.0.0"' in text


def test_bump_patch_default(tmp_path: Path) -> None:
    """Calling bump() with no part argument still increments patch."""
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    result = bump(tmp_path)
    assert result == "1.0.1"


def test_read_version_from_ref_error_no_stderr() -> None:
    import subprocess as _sp

    from vergil_tooling.lib.version import _read_version_from_ref

    err = _sp.CalledProcessError(1, "git show")
    err.stderr = ""
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.version.subprocess.run", side_effect=err),
        pytest.raises(_sp.CalledProcessError),
    ):
        _read_version_from_ref("nonexistent", "VERSION", "plaintext")


def test_run_lockfile_maintenance_error_no_stderr(tmp_path: Path) -> None:
    import subprocess as _sp

    from vergil_tooling.lib.version import _run_lockfile_maintenance

    err = _sp.CalledProcessError(1, "uv lock")
    err.stderr = ""
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.version.subprocess.run", side_effect=err),
        pytest.raises(_sp.CalledProcessError),
    ):
        _run_lockfile_maintenance(tmp_path, "python")


def test_read_version_from_ref_prints_stderr_on_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess as _sp

    from vergil_tooling.lib.version import _read_version_from_ref

    err = _sp.CalledProcessError(1, "git show")
    err.stderr = "fatal: bad revision\n"
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.version.subprocess.run", side_effect=err),
        pytest.raises(_sp.CalledProcessError),
    ):
        _read_version_from_ref("nonexistent", "VERSION", "plaintext")
    captured = capsys.readouterr()
    assert "bad revision" in captured.err


def test_run_lockfile_maintenance_prints_stderr_on_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess as _sp

    from vergil_tooling.lib.version import _run_lockfile_maintenance

    err = _sp.CalledProcessError(1, "uv lock")
    err.stderr = "error: could not resolve\n"
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.version.subprocess.run", side_effect=err),
        pytest.raises(_sp.CalledProcessError),
    ):
        _run_lockfile_maintenance(tmp_path, "python")
    captured = capsys.readouterr()
    assert "could not resolve" in captured.err


def test_bump_invalid_part_raises(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with pytest.raises(ValueError, match="part must be"):
        bump(tmp_path, "rc")


# -- next_version() and bump_paths() tests (#4415) ----------------------------


@pytest.mark.parametrize(
    ("part", "expected"), [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")]
)
def test_next_version_matches_bump(tmp_path: Path, part: str, expected: str) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    assert next_version("1.2.3", part) == expected
    assert bump(tmp_path, part) == expected


def test_next_version_invalid_part_raises() -> None:
    with pytest.raises(ValueError, match="part must be"):
        next_version("1.0.0", "rc")


def test_bump_paths_rust(tmp_path: Path) -> None:
    _write_toml(tmp_path, "rust")
    assert bump_paths(tmp_path) == {
        "VERSION",
        "Cargo.toml",
        "Cargo.lock",
        ".claude-plugin/plugin.json",
    }


def test_bump_paths_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    assert bump_paths(tmp_path) == {
        "VERSION",
        "pyproject.toml",
        "uv.lock",
        ".claude-plugin/plugin.json",
    }


def test_bump_paths_go(tmp_path: Path) -> None:
    _write_toml(tmp_path, "go")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "version.go").write_text('package pkg\n\nVersion = "1.0.5"\n')
    assert bump_paths(tmp_path) == {"VERSION", "pkg/version.go", ".claude-plugin/plugin.json"}


def test_bump_paths_go_without_version_file(tmp_path: Path) -> None:
    """With no version.go, bump writes none, so none is listed."""
    _write_toml(tmp_path, "go")
    assert bump_paths(tmp_path) == {"VERSION", ".claude-plugin/plugin.json"}


def test_bump_paths_generic(tmp_path: Path) -> None:
    _write_toml(tmp_path)
    assert bump_paths(tmp_path) == {"VERSION", ".claude-plugin/plugin.json"}


# -- Rust workspace versions (epic vergil-project/.github#1165, spec §6.4) -----


def _rust_repo(tmp_path: Path, cargo_toml: str, version: str) -> Path:
    _write_toml(tmp_path, "rust")
    (tmp_path / "VERSION").write_text(f"{version}\n")
    (tmp_path / "Cargo.toml").write_text(cargo_toml)
    return tmp_path


def _crate(root: Path, rel: str, body: str) -> Path:
    crate_dir = root / rel
    crate_dir.mkdir(parents=True)
    (crate_dir / "Cargo.toml").write_text(body)
    return crate_dir


_INHERITING_ROOT = (
    "[workspace]\n"
    'members = ["foo"]\n\n'
    "[workspace.package]\n"
    'version = "1.2.3"\n\n'
    "[package]\n"
    'name = "root"\n'
    "version.workspace = true\n"
)


def test_cargo_version_from_package() -> None:
    data = {"package": {"name": "x", "version": "1.0.0"}, "workspace": {"package": {}}}
    assert cargo_version_table(data) == ("package",)


def test_cargo_version_from_virtual_workspace(tmp_path: Path) -> None:
    data = {"workspace": {"members": ["foo"], "package": {"version": "2.0.0"}}}
    assert cargo_version_table(data) == ("workspace", "package")
    _rust_repo(
        tmp_path,
        '[workspace]\nmembers = ["foo"]\n\n[workspace.package]\nversion = "2.0.0"\n',
        "2.0.0",
    )
    assert show(tmp_path) == "2.0.0"


def test_cargo_version_from_inheriting_root(tmp_path: Path) -> None:
    data = {
        "package": {"name": "root", "version": {"workspace": True}},
        "workspace": {"package": {"version": "1.2.3"}},
    }
    assert cargo_version_table(data) == ("workspace", "package")
    _rust_repo(tmp_path, _INHERITING_ROOT, "1.2.3")
    assert show(tmp_path) == "1.2.3"


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({}, r"no \[package\] and \[workspace\.package\] has no version"),
        ({"workspace": {"members": []}}, r"no \[package\] and \[workspace\.package\]"),
        ({"workspace": {"package": {"edition": "2021"}}}, r"\[workspace\.package\]"),
        ({"package": {"name": "x"}}, r"\[package\] has no version.*\[workspace\.package\]"),
        (
            {"package": {"name": "x", "version": {"workspace": False}}},
            r"\[package\] has no version.*\[workspace\.package\]",
        ),
        (
            {"package": {"name": "x", "version": {"workspace": True}}},
            r"\[package\] inherits its version but \[workspace\.package\]",
        ),
    ],
)
def test_cargo_version_table_without_version_fails(data: dict[str, object], match: str) -> None:
    with pytest.raises(CargoVersionError, match=match):
        cargo_version_table(data)


def test_cargo_version_error_is_not_a_value_error() -> None:
    """bump()'s ``except (FileNotFoundError, ValueError)`` must not swallow it."""
    assert not issubclass(CargoVersionError, ValueError)


def test_show_with_no_version_table_fails(tmp_path: Path) -> None:
    _rust_repo(tmp_path, '[workspace]\nmembers = ["foo"]\n', "1.0.0")
    with pytest.raises(CargoVersionError, match=r"\[package\].*\[workspace\.package\]"):
        show(tmp_path)


def test_bump_with_no_version_table_fails(tmp_path: Path) -> None:
    manifest = '[workspace]\nmembers = ["foo"]\n'
    _rust_repo(tmp_path, manifest, "1.0.0")
    with (
        patch("vergil_tooling.lib.version.subprocess.run") as mock_run,
        pytest.raises(CargoVersionError, match=r"\[package\].*\[workspace\.package\]"),
    ):
        bump(tmp_path)
    mock_run.assert_not_called()
    assert (tmp_path / "VERSION").read_text() == "1.0.0\n"
    assert (tmp_path / "Cargo.toml").read_text() == manifest


def test_bump_writes_workspace_package_not_dependencies(tmp_path: Path) -> None:
    manifest = (
        "[workspace]\n"
        'members = ["foo"]\n\n'
        "[workspace.dependencies]\n"
        'foo = { path = "foo", version = "1.2.3" }\n'
        "[workspace.dependencies.bar]\n"
        'version = "1.2.3"\n\n'
        "[workspace.package]  # shared metadata\n"
        'edition = "2021"\n'
        'version = "1.2.3"  # the release version\n'
    )
    _rust_repo(tmp_path, manifest, "1.2.3")
    _crate(tmp_path, "foo", '[package]\nname = "foo"\nversion.workspace = true\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path) == "1.2.4"
    assert (tmp_path / "Cargo.toml").read_text() == manifest.replace(
        'version = "1.2.3"  # the release version', 'version = "1.2.4"  # the release version'
    )
    assert (tmp_path / "VERSION").read_text() == "1.2.4\n"
    assert show(tmp_path) == "1.2.4"


def test_bump_inheriting_root_writes_workspace_package(tmp_path: Path) -> None:
    _rust_repo(tmp_path, _INHERITING_ROOT, "1.2.3")
    _crate(tmp_path, "foo", '[package]\nname = "foo"\nversion.workspace = true\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path, "minor") == "1.3.0"
    text = (tmp_path / "Cargo.toml").read_text()
    assert text == _INHERITING_ROOT.replace('version = "1.2.3"', 'version = "1.3.0"')


def test_bump_package_version_skips_earlier_tables(tmp_path: Path) -> None:
    """A single crate whose dependency happens to share its version is untouched."""
    manifest = (
        "[dependencies]\n"
        'serde = { version = "0.3.7" }\n\n'
        "[[bin]]\n"
        'name = "tool"\n\n'
        '[ "package" ]\n'
        'name = "example"\n'
        "version = '0.3.7'\n"
    )
    _rust_repo(tmp_path, manifest, "0.3.7")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path) == "0.3.8"
    text = (tmp_path / "Cargo.toml").read_text()
    assert text == manifest.replace("version = '0.3.7'", "version = '0.3.8'")


def test_bump_skips_version_line_inside_multiline_string(tmp_path: Path) -> None:
    manifest = (
        "[package]\n"
        'name = "example"\n'
        'description = """\n'
        'version = "0.1.0"\n'
        '"""\n'
        'version = "0.1.0"\n'
    )
    _rust_repo(tmp_path, manifest, "0.1.0")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        bump(tmp_path)
    text = (tmp_path / "Cargo.toml").read_text()
    assert text == manifest[: manifest.rindex('version = "0.1.0"')] + 'version = "0.1.1"\n'


def test_bump_version_not_rewritable_fails_first(tmp_path: Path) -> None:
    manifest = '[workspace]\npackage = { version = "1.0.0" }\n'
    _rust_repo(tmp_path, manifest, "1.0.0")
    with (
        patch("vergil_tooling.lib.version.subprocess.run"),
        pytest.raises(CargoVersionError, match=r"cannot rewrite.*\[workspace\.package\]"),
    ):
        bump(tmp_path)
    assert (tmp_path / "VERSION").read_text() == "1.0.0\n"
    assert (tmp_path / "Cargo.toml").read_text() == manifest


def test_bump_rust_version_mismatch_fails_first(tmp_path: Path) -> None:
    manifest = '[package]\nname = "example"\nversion = "0.9.0"\n'
    _rust_repo(tmp_path, manifest, "1.0.0")
    with (
        patch("vergil_tooling.lib.version.subprocess.run"),
        pytest.raises(VersionSyncError, match=r"VERSION contains 1\.0\.0.*\[package\].*0\.9\.0"),
    ):
        bump(tmp_path)
    assert (tmp_path / "VERSION").read_text() == "1.0.0\n"


def test_bump_rust_without_cargo_toml(tmp_path: Path) -> None:
    _write_toml(tmp_path, "rust")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path) == "1.0.1"
    assert not (tmp_path / "Cargo.toml").exists()


_VERSIONED_MEMBER_DEP = (
    "[workspace]\n"
    'members = ["foo"]\n\n'
    "[workspace.package]\n"
    'version = "0.1.3"\n\n'
    "[workspace.dependencies]\n"
    'foo = { path = "foo", version = "0.1.3" }\n'
)


def test_bump_breaking_member_requirement_fails_first(tmp_path: Path) -> None:
    _rust_repo(tmp_path, _VERSIONED_MEMBER_DEP, "0.1.3")
    _crate(tmp_path, "foo", '[package]\nname = "foo"\nversion.workspace = true\n')
    with (
        patch("vergil_tooling.lib.version.subprocess.run") as mock_run,
        pytest.raises(CargoVersionError, match=r"(?s)0\.2\.0.*\[workspace\.dependencies\] foo"),
    ):
        bump(tmp_path, "minor")
    mock_run.assert_not_called()
    assert (tmp_path / "VERSION").read_text() == "0.1.3\n"
    assert (tmp_path / "Cargo.toml").read_text() == _VERSIONED_MEMBER_DEP

    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path, "patch") == "0.1.4"
    assert 'foo = { path = "foo", version = "0.1.3" }' in (tmp_path / "Cargo.toml").read_text()


def test_bump_breaking_member_manifest_requirement_fails_first(tmp_path: Path) -> None:
    manifest = '[workspace]\nmembers = ["crates/*"]\n\n[workspace.package]\nversion = "0.1.3"\n'
    _rust_repo(tmp_path, manifest, "0.1.3")
    _crate(tmp_path, "crates/foo", '[package]\nname = "foo"\nversion.workspace = true\n')
    baz = _crate(
        tmp_path,
        "crates/baz",
        '[package]\nname = "baz"\nversion.workspace = true\n\n'
        "[dependencies]\n"
        'bar = { path = "../foo", version = "0.1.3", package = "foo" }\n\n'
        "[target.'cfg(unix)'.dev-dependencies]\n"
        'qux = { path = "../foo", version = "~0.1" }\n',
    )
    (tmp_path / "crates" / "README.md").write_text("not a crate\n")
    before = (baz / "Cargo.toml").read_text()
    with (
        patch("vergil_tooling.lib.version.subprocess.run"),
        pytest.raises(CargoVersionError) as excinfo,
    ):
        bump(tmp_path, "minor")
    message = str(excinfo.value)
    assert "crates/baz/Cargo.toml [dependencies] bar" in message
    assert "crates/baz/Cargo.toml [target.cfg(unix).dev-dependencies] qux" in message
    assert (tmp_path / "VERSION").read_text() == "0.1.3\n"
    assert (tmp_path / "Cargo.toml").read_text() == manifest
    assert (baz / "Cargo.toml").read_text() == before

    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path, "patch") == "0.1.4"


def test_bump_ignores_dependencies_on_crates_it_does_not_bump(tmp_path: Path) -> None:
    """Only a crate whose version the bump writes is checked."""
    manifest = (
        "[workspace]\n"
        'members = ["own", "user"]\n'
        'exclude = ["vendored"]\n\n'
        "[workspace.package]\n"
        'version = "0.1.3"\n\n'
        "[workspace.dependencies]\n"
        'own = { path = "own", version = "0.1.3" }\n'
        'serde = "1.0"\n'
        'outside = { path = "../outside", version = "0.1.3" }\n'
        'unversioned = { path = "user" }\n'
    )
    _rust_repo(tmp_path, manifest, "0.1.3")
    _crate(tmp_path, "own", '[package]\nname = "own"\nversion = "0.1.3"\n')
    _crate(
        tmp_path,
        "user",
        '[package]\nname = "user"\nversion.workspace = true\n\n'
        "[dependencies]\n"
        'vendored = { path = "../vendored", version = "0.1.3" }\n'
        "own = { workspace = true }\n",
    )
    _crate(tmp_path, "vendored", '[package]\nname = "vendored"\nversion.workspace = true\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path, "minor") == "0.2.0"


def test_bump_root_package_checks_members_depending_on_it(tmp_path: Path) -> None:
    """A root [package] with its own version: members' requirements on it are checked."""
    manifest = (
        "[package]\n"
        'name = "root"\n'
        'version = "1.4.0"\n\n'
        "[dependencies]\n"
        'helper = { path = "helper" }\n\n'
        "[workspace]\n"
    )
    _rust_repo(tmp_path, manifest, "1.4.0")
    _crate(
        tmp_path,
        "helper",
        '[package]\nname = "helper"\nversion = "0.1.0"\n\n'
        "[build-dependencies]\n"
        'root = { path = "..", version = "1.4" }\n',
    )
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path, "minor") == "1.5.0"
        with pytest.raises(CargoVersionError, match=r"helper/Cargo\.toml \[build-dependencies\]"):
            bump(tmp_path, "major")
    assert (tmp_path / "VERSION").read_text() == "1.5.0\n"


def test_bump_unparseable_member_requirement_fails(tmp_path: Path) -> None:
    manifest = _VERSIONED_MEMBER_DEP.replace('version = "0.1.3" }', 'version = "nope" }')
    _rust_repo(tmp_path, manifest, "0.1.3")
    _crate(tmp_path, "foo", '[package]\nname = "foo"\nversion.workspace = true\n')
    with (
        patch("vergil_tooling.lib.version.subprocess.run"),
        pytest.raises(CargoVersionError, match=r"cannot check Cargo\.toml .*foo"),
    ):
        bump(tmp_path)
    assert (tmp_path / "VERSION").read_text() == "0.1.3\n"


def test_bump_missing_member_manifest_fails(tmp_path: Path) -> None:
    _rust_repo(tmp_path, _VERSIONED_MEMBER_DEP, "0.1.3")
    (tmp_path / "foo").mkdir()
    with (
        patch("vergil_tooling.lib.version.subprocess.run"),
        pytest.raises(CargoVersionError, match=r"foo/Cargo\.toml not found"),
    ):
        bump(tmp_path)
    assert (tmp_path / "VERSION").read_text() == "0.1.3\n"


def test_bump_unparseable_member_manifest_fails(tmp_path: Path) -> None:
    _rust_repo(tmp_path, _VERSIONED_MEMBER_DEP, "0.1.3")
    _crate(tmp_path, "foo", "[package\n")
    with (
        patch("vergil_tooling.lib.version.subprocess.run"),
        pytest.raises(CargoVersionError, match=r"cannot parse foo/Cargo\.toml"),
    ):
        bump(tmp_path)
    assert (tmp_path / "VERSION").read_text() == "0.1.3\n"


def test_bump_tolerates_odd_manifest_shapes(tmp_path: Path) -> None:
    """Non-table values where tables are expected are not dependencies."""
    manifest = (
        "workspace = 1\n"
        'target = "x"\n'
        'dependencies = "x"\n\n'
        "[package]\n"
        'name = "odd"\n'
        'version = "1.0.0"\n'
    )
    _rust_repo(tmp_path, manifest, "1.0.0")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path) == "1.0.1"


def test_bump_target_table_that_is_not_a_table(tmp_path: Path) -> None:
    manifest = '[package]\nname = "odd"\nversion = "1.0.0"\n\n[target]\nlinux = "x"\n'
    _rust_repo(tmp_path, manifest, "1.0.0")
    with patch("vergil_tooling.lib.version.subprocess.run"):
        assert bump(tmp_path) == "1.0.1"
