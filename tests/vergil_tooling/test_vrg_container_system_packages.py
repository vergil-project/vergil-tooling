"""Tests for the vrg-container-system-packages accessor CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.bin import vrg_container_system_packages as cli

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


def _write(tmp_path: Path, packages: str) -> Path:
    (tmp_path / "vergil.toml").write_text(
        _VALID_TOML + f"[container]\nenv-prefixes = []\nsystem-packages = {packages}\n"
    )
    return tmp_path


def test_list_mode_prints_one_per_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, '["lilypond", "fluidsynth"]')
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    assert capsys.readouterr().out.split() == ["lilypond", "fluidsynth"]


def test_list_mode_empty_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "[]")
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_list_mode_no_config_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_install_script_mode_emits_apt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, '["lilypond"]')
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--install-script"]) == 0
    out = capsys.readouterr().out
    assert "apt-get install -y --no-install-recommends" in out
    assert "lilypond" in out


def test_install_script_mode_empty_prints_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "[]")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--install-script"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_repo_root_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write(tmp_path, '["lilypond"]')
    assert cli.main(["--repo-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["lilypond"]
