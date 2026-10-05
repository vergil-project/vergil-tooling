"""Tests for the vrg-package CLI (vergil_tooling.bin.vrg_package)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_package

if TYPE_CHECKING:
    from pathlib import Path

_VALID_TOML_TEXT = """\
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

_PKG_PY_TOML = (
    _VALID_TOML_TEXT
    + """
[package]
builder = "python"
vendor = "vergil"
summary = "Shared tooling"
smoke = "vrg-whoami --mode"

[package.python]
runtime = "3.14.4"
"""
)


def test_matrix_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is True
    assert len(out["build"]) == 2
    assert len(out["test"]) == 8


def test_matrix_github_output_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    gho = tmp_path / "gho"
    gho.write_text("prior=1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    assert vrg_package.main(["matrix", "--github-output", "--manifest", "m.json"]) == 0
    lines = gho.read_text().splitlines()
    assert lines[0] == "prior=1"
    assert lines[1] == "enabled=true"
    assert lines[2].startswith("build=[")
    assert lines[3].startswith("test=[")
    assert len(json.loads(lines[2].removeprefix("build="))) == 2
    assert len(lines) == 4
    assert json.loads((tmp_path / "m.json").read_text())["artifacts"]
    assert capsys.readouterr().out == ""


def test_matrix_without_package_section_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML_TEXT)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix"]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False, "build": [], "test": []}


def test_disabled_github_output_and_no_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML_TEXT)
    gho = tmp_path / "gho"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    assert vrg_package.main(["matrix", "--github-output", "--manifest", "m.json"]) == 0
    assert gho.read_text().splitlines() == ["enabled=false", "build=[]", "test=[]"]
    assert not (tmp_path / "m.json").exists()
    assert "no [package] section; manifest not written" in capsys.readouterr().err


def test_github_output_requires_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert vrg_package.main(["matrix", "--github-output"]) == 1
    assert "GITHUB_OUTPUT is not set" in capsys.readouterr().err


def test_config_error_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(
        _PKG_PY_TOML.replace("[package]\n", '[package]\nexclude = ["*/*/*"]\n')
    )
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix"]) == 1
    assert "selects no targets" in capsys.readouterr().err


def test_missing_vergil_toml_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix"]) == 1
    assert "vergil.toml not found" in capsys.readouterr().err


def test_subcommand_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_package.main([])
    assert exc.value.code == 2
    assert "matrix" in capsys.readouterr().err


def test_build_subcommand_wires_run_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def fake(
        repo_root: Path, cell_id: str, version: str, out_dir: Path, staging_root: Path
    ) -> list[Path]:
        seen.update(
            root=repo_root, cell=cell_id, version=version, out=out_dir, staging=staging_root
        )
        return [out_dir / "a.deb", out_dir / "a.rpm"]

    monkeypatch.setattr(vrg_package.build, "run_build", fake)
    monkeypatch.chdir(tmp_path)
    argv = ["build", "--cell", "shared-amd64", "--version", "2.1.240", "--out", "dist"]
    assert vrg_package.main(argv) == 0
    assert seen == {
        "root": tmp_path,
        "cell": "shared-amd64",
        "version": "2.1.240",
        "out": tmp_path / "dist",
        "staging": tmp_path / ".vergil/package-staging",
    }
    out = capsys.readouterr().out.splitlines()
    assert out == [str(tmp_path / "dist/a.deb"), str(tmp_path / "dist/a.rpm")]


def test_build_defaults_and_absolute_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(
        repo_root: Path, cell_id: str, version: str, out_dir: Path, staging_root: Path
    ) -> list[Path]:
        seen.update(out=out_dir, staging=staging_root)
        return []

    monkeypatch.setattr(vrg_package.build, "run_build", fake)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["build", "--cell", "c", "--version", "1"]) == 0
    assert seen == {
        "out": tmp_path / "dist/packages",
        "staging": tmp_path / ".vergil/package-staging",
    }
    stage = tmp_path / "elsewhere"
    argv = ["build", "--cell", "c", "--version", "1", "--staging", str(stage)]
    assert vrg_package.main(argv) == 0
    assert seen["staging"] == stage


def test_build_package_error_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML_TEXT)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["build", "--cell", "shared-amd64", "--version", "1.0.0"]) == 1
    assert "no [package] section" in capsys.readouterr().err


def test_build_requires_cell_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_package.main(["build", "--version", "1"])
    assert exc.value.code == 2
    assert "--cell" in capsys.readouterr().err
