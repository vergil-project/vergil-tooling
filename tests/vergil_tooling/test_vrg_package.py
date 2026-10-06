"""Tests for the vrg-package CLI (vergil_tooling.bin.vrg_package)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_package
from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import site

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
    (tmp_path / "uv.lock").write_text("version = 1\n")
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
    (tmp_path / "uv.lock").write_text("version = 1\n")
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
    (tmp_path / "uv.lock").write_text("version = 1\n")
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


# --- index ---------------------------------------------------------------------


def _record_build_site(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake(config: Path, keys: Path, out: Path, work: Path, *, base_url: str) -> None:
        calls.append((str(config), str(keys), str(out), str(work), base_url))

    monkeypatch.setattr(site, "build_site", fake)
    return calls


def test_index_builds_the_site(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _record_build_site(monkeypatch)
    argv = ["index", "--config", "packages.toml", "--keys", "keys", "--out", "_site"]
    assert vrg_package.main([*argv, "--base-url", "https://v.example/p"]) == 0
    assert calls == [
        ("packages.toml", "keys", "_site", ".vergil/index-work", "https://v.example/p")
    ]
    assert "package site written to _site" in capsys.readouterr().out


def test_index_base_url_defaults_from_github_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_build_site(monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "vergil-project/packages")
    argv = ["index", "--config", "c", "--keys", "k", "--out", "o", "--work", "w"]
    assert vrg_package.main(argv) == 0
    assert calls == [("c", "k", "o", "w", "https://vergil-project.github.io/packages")]


def test_index_without_base_url_fails_before_building(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _record_build_site(monkeypatch)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert vrg_package.main(["index", "--config", "c", "--keys", "k", "--out", "o"]) == 1
    assert calls == []
    assert "pass --base-url or set GITHUB_REPOSITORY" in capsys.readouterr().err


def test_index_size_guard_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def too_big(*_a: object, **_k: object) -> None:
        msg = "package site is 950000000 bytes, over the 900000000-byte limit"
        raise PackageError(msg)

    monkeypatch.setattr(site, "build_site", too_big)
    argv = ["index", "--config", "c", "--keys", "k", "--out", "o", "--base-url", "u"]
    assert vrg_package.main(argv) == 1
    assert "ERROR: package site is 950000000 bytes" in capsys.readouterr().err


def test_index_requires_config_keys_and_out(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_package.main(["index"])
    assert exc.value.code == 2
    assert "--config" in capsys.readouterr().err
