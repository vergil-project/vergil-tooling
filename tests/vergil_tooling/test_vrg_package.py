"""Tests for the vrg-package CLI (vergil_tooling.bin.vrg_package)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_package
from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import published, site
from vergil_tooling.lib.package.index.collect import Identity

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
    assert list(out) == ["enabled", "tier", "build", "test"]
    assert out["enabled"] is True
    assert out["tier"] == "full"
    assert len(out["build"]) == 2
    assert len(out["test"]) == 8


def test_matrix_records_the_pinned_runtime_in_every_cell_and_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix", "--manifest", "m.json"]) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert [c["runtime"] for c in out["build"]] == ["3.14.4", "3.14.4"]
    assert all("runtime" not in c for c in out["test"])
    assert json.loads((tmp_path / "m.json").read_text())["runtime"] == "3.14.4"
    assert "vrg-package: release runtime vergil-python 3.14.4" in captured.err


def test_matrix_derives_an_unpinned_runtime_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fixture window publishes a runtime for 3.14 only.
    toml = _PKG_PY_TOML.replace('[package.python]\nruntime = "3.14.4"\n', "[package.python]\n")
    (tmp_path / "vergil.toml").write_text(toml)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.chdir(tmp_path)
    calls: list[Path] = []
    real = vrg_package.python_builder.package_runtime

    def once(root: Path) -> str:
        calls.append(root)
        return real(root)

    monkeypatch.setattr(vrg_package.python_builder, "package_runtime", once)
    gho = tmp_path / "gho"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    assert vrg_package.main(["matrix", "--github-output", "--manifest", "m.json"]) == 0
    assert calls == [tmp_path]
    build_line = next(ln for ln in gho.read_text().splitlines() if ln.startswith("build="))
    assert [c["runtime"] for c in json.loads(build_line.removeprefix("build="))] == ["3.14"] * 2
    assert json.loads((tmp_path / "m.json").read_text())["runtime"] == "3.14"
    assert "release runtime vergil-python 3.14" in capsys.readouterr().err


def test_matrix_staged_builder_records_no_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staged = _PKG_PY_TOML.replace('builder = "python"', 'builder = "staged"\nname = "t"').replace(
        '[package.python]\nruntime = "3.14.4"\n', '[package.staged]\nbuild-command = "true"\n'
    )
    (tmp_path / "vergil.toml").write_text(staged)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix", "--manifest", "m.json"]) == 0
    captured = capsys.readouterr()
    assert all("runtime" not in c for c in json.loads(captured.out)["build"])
    assert "runtime" not in json.loads((tmp_path / "m.json").read_text())
    assert "release runtime" not in captured.err


def test_matrix_explicit_full_tier_matches_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix"]) == 0
    default = capsys.readouterr().out
    assert vrg_package.main(["matrix", "--tier", "full"]) == 0
    assert capsys.readouterr().out == default


def test_matrix_reduced_tier_prints_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix", "--tier", "reduced"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is True
    assert out["tier"] == "reduced"
    assert [c["id"] for c in out["build"]] == ["shared-amd64", "shared-arm64"]
    assert [c["id"] for c in out["test"]] == ["test-rhel-9-amd64", "test-ubuntu-24.04-amd64"]


def test_matrix_reduced_github_output_keeps_full_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    gho = tmp_path / "gho"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    assert vrg_package.main(["matrix", "--manifest", "full.json"]) == 0
    capsys.readouterr()
    argv = ["matrix", "--tier", "reduced", "--github-output", "--manifest", "m.json"]
    assert vrg_package.main(argv) == 0
    lines = gho.read_text().splitlines()
    assert [ln.split("=", 1)[0] for ln in lines] == ["enabled", "build", "test", "tier"]
    assert lines[0] == "enabled=true"
    assert lines[3] == "tier=reduced"
    assert len(json.loads(lines[1].removeprefix("build="))) == 2
    tests = json.loads(lines[2].removeprefix("test="))
    assert [c["target"] for c in tests] == ["rhel/9/amd64", "ubuntu/24.04/amd64"]
    assert (tmp_path / "m.json").read_text() == (tmp_path / "full.json").read_text()
    assert capsys.readouterr().out == ""


def test_matrix_rejects_unknown_tier(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_package.main(["matrix", "--tier", "minimal"])
    assert exc.value.code == 2
    assert "invalid choice: 'minimal'" in capsys.readouterr().err


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
    assert len(json.loads(lines[3].removeprefix("test="))) == 8
    assert lines[4] == "tier=full"
    assert len(lines) == 5
    assert json.loads((tmp_path / "m.json").read_text())["artifacts"]
    assert capsys.readouterr().out == ""


def test_matrix_without_package_section_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML_TEXT)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["matrix"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "enabled": False,
        "tier": "full",
        "build": [],
        "test": [],
    }
    assert vrg_package.main(["matrix", "--tier", "reduced"]) == 0
    assert json.loads(capsys.readouterr().out)["tier"] == "reduced"


def test_disabled_github_output_and_no_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML_TEXT)
    gho = tmp_path / "gho"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(gho))
    assert vrg_package.main(["matrix", "--github-output", "--manifest", "m.json"]) == 0
    assert gho.read_text().splitlines() == ["enabled=false", "build=[]", "test=[]", "tier=full"]
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
        repo_root: Path,
        cell_id: str,
        version: str,
        out_dir: Path,
        staging_root: Path,
        *,
        runtime: str | None,
    ) -> list[Path]:
        seen.update(
            root=repo_root,
            cell=cell_id,
            version=version,
            out=out_dir,
            staging=staging_root,
            runtime=runtime,
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
        "runtime": None,
    }
    out = capsys.readouterr().out.splitlines()
    assert out == [str(tmp_path / "dist/a.deb"), str(tmp_path / "dist/a.rpm")]
    assert vrg_package.main([*argv, "--runtime", "3.14"]) == 0
    assert seen["runtime"] == "3.14"
    # The workflow passes the cell's runtime verbatim; empty means none recorded.
    assert vrg_package.main([*argv, "--runtime", ""]) == 0
    assert seen["runtime"] is None


def test_build_defaults_and_absolute_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(
        repo_root: Path,
        cell_id: str,
        version: str,
        out_dir: Path,
        staging_root: Path,
        *,
        runtime: str | None,
    ) -> list[Path]:
        seen.update(out=out_dir, staging=staging_root, runtime=runtime)
        return []

    monkeypatch.setattr(vrg_package.build, "run_build", fake)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["build", "--cell", "c", "--version", "1"]) == 0
    assert seen == {
        "out": tmp_path / "dist/packages",
        "staging": tmp_path / ".vergil/package-staging",
        "runtime": None,
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


@pytest.mark.parametrize(
    "argv",
    [
        ["index", "--config", "c", "--out", "o"],
        ["index", "--config", "c", "--keys", "k"],
    ],
)
def test_index_build_requires_keys_and_out(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_package.main(argv)
    assert exc.value.code == 2
    assert "--keys and --out are required unless --check-config" in capsys.readouterr().err


_VALID_PACKAGES_TOML = """\
vendor = "vergil"
products = ["vergil-project/packages"]

[retention]
keep = 2
retire = ["old-pkg"]
"""


def test_index_check_config_accepts_a_valid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _record_build_site(monkeypatch)
    # No base URL, keys or output: none of them is needed to check the config.
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    cfg = tmp_path / "packages.toml"
    cfg.write_text(_VALID_PACKAGES_TOML, encoding="utf-8")
    assert vrg_package.main(["index", "--check-config", "--config", str(cfg)]) == 0
    assert calls == []
    assert f"vrg-package: {cfg}: ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            'vendor = "v"\nproducts = ["o/r"]\n[retention]\nkeep = 0\n',
            "retention.keep must be an integer >= 1, got 0",
        ),
        (
            'vendor = "v"\nproducts = ["o/r"]\n[retention]\nretire = ["x", "x"]\n',
            "retention.retire lists 'x' more than once",
        ),
        (
            'vendor = "v"\nproducts = ["o/r"]\n[retention]\nkeeep = 2\n',
            "unknown key(s) in [retention]: keeep",
        ),
    ],
)
def test_index_check_config_rejects_an_invalid_config(
    body: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _record_build_site(monkeypatch)
    cfg = tmp_path / "packages.toml"
    cfg.write_text(body, encoding="utf-8")
    assert vrg_package.main(["index", "--check-config", "--config", str(cfg)]) == 1
    assert calls == []
    captured = capsys.readouterr()
    assert f"ERROR: packages.toml: {error}" in captured.err
    assert captured.out == ""


def test_index_check_config_missing_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "packages.toml"
    assert vrg_package.main(["index", "--check-config", "--config", str(missing)]) == 1
    assert f"ERROR: packages.toml not found at {missing}" in capsys.readouterr().err


def test_install_test_subcommand_wires_run_install_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vergil_tooling.lib.package import install_test

    seen: dict[str, object] = {}

    def fake(repo_root: Path, cell_id: str, artifacts: Path, report: Path) -> None:
        seen.update(root=repo_root, cell=cell_id, artifacts=artifacts, report=report)

    monkeypatch.setattr(install_test, "run_install_test", fake)
    monkeypatch.chdir(tmp_path)
    argv = ["install-test", "--cell", "test-ubuntu-24.04-amd64"]
    argv += ["--artifacts", "dist/packages", "--report", "/abs/report.json"]
    assert vrg_package.main(argv) == 0
    assert seen == {
        "root": tmp_path,
        "cell": "test-ubuntu-24.04-amd64",
        "artifacts": tmp_path / "dist/packages",
        "report": tmp_path / "/abs/report.json",
    }


def test_install_test_command_failure_returns_1_with_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    from vergil_tooling.lib.package import install_test

    def fake(repo_root: Path, cell_id: str, artifacts: Path, report: Path) -> None:
        raise subprocess.CalledProcessError(100, ["apt-get", "update"], "", "E: no network\n")

    monkeypatch.setattr(install_test, "run_install_test", fake)
    monkeypatch.chdir(tmp_path)
    argv = ["install-test", "--cell", "c", "--artifacts", "a", "--report", "r.json"]
    assert vrg_package.main(argv) == 1
    err = capsys.readouterr().err
    expected = "ERROR: Command 'apt-get update' returned non-zero exit status 100.\nE: no network"
    assert expected in err


def test_install_test_package_error_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML_TEXT)
    monkeypatch.chdir(tmp_path)
    argv = ["install-test", "--cell", "c", "--artifacts", "a", "--report", "r.json"]
    assert vrg_package.main(argv) == 1
    assert "no [package] section" in capsys.readouterr().err


def test_install_test_requires_all_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_package.main(["install-test", "--cell", "c", "--artifacts", "a"])
    assert exc.value.code == 2
    assert "--report" in capsys.readouterr().err


def _record_check_release(
    monkeypatch: pytest.MonkeyPatch, result: list[Identity] | Exception
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake(repo_root: Path, pkg: Any, product: str, version: str) -> list[Identity]:
        calls.append((repo_root, pkg is not None, product, version))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(published, "check_release", fake)
    return calls


def _packaged_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "vergil.toml").write_text(_PKG_PY_TOML)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    monkeypatch.chdir(tmp_path)


def test_check_unpublished_lists_what_would_be_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _packaged_repo(tmp_path, monkeypatch)
    calls = _record_check_release(monkeypatch, [Identity("deb", "p", "1.0.0-1", "amd64")])
    assert vrg_package.main(["check-unpublished", "--version", "1.0.0", "--repo", "o/r"]) == 0
    assert calls == [(tmp_path, True, "o/r", "1.0.0")]
    assert "p 1.0.0-1 amd64 (deb): not yet published by o/r" in capsys.readouterr().out


def test_check_unpublished_repo_defaults_from_github_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _packaged_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/from-env")
    calls = _record_check_release(monkeypatch, [Identity("deb", "p", "1.0.0-1", "amd64")])
    assert vrg_package.main(["check-unpublished", "--version", "1.0.0"]) == 0
    assert calls[0][2] == "o/from-env"


def test_check_unpublished_without_a_repo_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _packaged_repo(tmp_path, monkeypatch)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    calls = _record_check_release(monkeypatch, [])
    assert vrg_package.main(["check-unpublished", "--version", "1.0.0"]) == 1
    assert "pass --repo OWNER/NAME" in capsys.readouterr().err
    assert calls == []


def test_check_unpublished_conflict_exits_1_with_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _packaged_repo(tmp_path, monkeypatch)
    _record_check_release(monkeypatch, PackageError("o/r@v1.0.1 would re-publish 1 package(s)"))
    assert vrg_package.main(["check-unpublished", "--version", "1.0.1", "--repo", "o/r"]) == 1
    assert "ERROR: o/r@v1.0.1 would re-publish" in capsys.readouterr().err


def test_check_unpublished_without_package_section_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML_TEXT)
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["check-unpublished", "--version", "1.0.0", "--repo", "o/r"]) == 0
    assert "no [package] section; o/r v1.0.0 publishes no packages" in capsys.readouterr().out


# --- check-runtime (#3478) -----------------------------------------------------


def _release_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depends: dict[str, list[str]], manifest: Any
) -> list[str]:
    """Write ``manifest`` and one file per ``depends`` key; fake the metadata readers."""
    arts = tmp_path / "dist"
    arts.mkdir()
    for name in depends:
        (arts / name).write_bytes(b"pkg")
    (arts / "SHA256SUMS").write_text("not a package\n")
    m = tmp_path / "m.json"
    m.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
    read: list[str] = []

    def meta(path: Path, *_a: object) -> tuple[str, str, str, str, tuple[str, ...]]:
        read.append(path.name)
        return ("t", "1.0.0", "1", "amd64", tuple(depends[path.name]))

    monkeypatch.setattr(vrg_package.collect, "deb_metadata", meta)
    monkeypatch.setattr(vrg_package.collect, "rpm_metadata", meta)
    monkeypatch.chdir(tmp_path)
    return read


_CHECK = ["check-runtime", "--manifest", "m.json", "--artifacts", "dist"]


def test_check_runtime_passes_a_consistent_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    depends = {"t_1-1_amd64.deb": ["vergil-python3.14"], "t-1-1.aarch64.rpm": ["vergil-python3.14"]}
    read = _release_dir(tmp_path, monkeypatch, depends, {"artifacts": [], "runtime": "3.14"})
    assert vrg_package.main(_CHECK) == 0
    assert read == ["t-1-1.aarch64.rpm", "t_1-1_amd64.deb"]
    assert "all 2 package(s) in dist depend on vergil-python3.14" in capsys.readouterr().out


def test_check_runtime_fails_mixed_runtimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    depends = {"t_1-1_amd64.deb": ["vergil-python3.15"], "t_1-1_arm64.deb": ["vergil-python3.14"]}
    _release_dir(tmp_path, monkeypatch, depends, {"artifacts": [], "runtime": "3.14"})
    assert vrg_package.main(_CHECK) == 1
    err = capsys.readouterr().err
    assert "ERROR: dist: packages disagree with the recorded runtime vergil-python3.14" in err
    assert "t_1-1_amd64.deb depends on vergil-python3.15" in err


def test_check_runtime_fails_when_no_packages_were_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _release_dir(tmp_path, monkeypatch, {}, {"artifacts": [], "runtime": "3.14"})
    assert vrg_package.main(_CHECK) == 1
    assert "no packages to check" in capsys.readouterr().err


def test_check_runtime_passes_a_manifest_without_a_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    read = _release_dir(tmp_path, monkeypatch, {"k_1-1_all.deb": []}, {"artifacts": []})
    assert vrg_package.main(_CHECK) == 0
    assert read == []
    assert "records no runtime (not a python-builder release); passed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        ("{nope", r"m\.json is not valid JSON"),
        ("[1]", r"m\.json: expected a JSON object"),
        ({"runtime": 3.14}, r"m\.json: 'runtime' must be a string, got 3\.14"),
    ],
)
def test_check_runtime_malformed_manifest_is_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manifest: Any,
    match: str,
) -> None:
    _release_dir(tmp_path, monkeypatch, {}, manifest)
    assert vrg_package.main(_CHECK) == 1
    assert re.search(match, capsys.readouterr().err)


def test_check_runtime_missing_artifacts_dir_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "m.json").write_text(json.dumps({"runtime": "3.14"}))
    monkeypatch.chdir(tmp_path)
    assert vrg_package.main(["check-runtime", "--manifest", "m.json", "--artifacts", "nope"]) == 1
    assert "ERROR:" in capsys.readouterr().err
