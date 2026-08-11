"""Tests for the vrg-container-build-command accessor CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.bin import vrg_container_build_command as cli

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_VALID_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


def _write(tmp_path: Path, build_command: str) -> Path:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + f"[container]\nenv-prefixes = []\nbuild-command = {build_command}\n"
    )
    return tmp_path


def test_script_mode_prints_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, '"make deps"')
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--script"]) == 0
    assert capsys.readouterr().out.strip() == "make deps"


def test_script_mode_no_command_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--script"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_default_mode_prints_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, '"make deps"')
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    assert capsys.readouterr().out.strip() == "make deps"


def test_default_mode_no_command_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_repo_root_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, '"make deps"')
    assert cli.main(["--repo-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "make deps"
