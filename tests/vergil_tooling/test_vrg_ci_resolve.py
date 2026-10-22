"""Tests for vergil_tooling.bin.vrg_ci_resolve (epic vergil-project/.github#407).

The window is the conftest fixture (python 3.14/3.13/3.12, cpp clang 20/19 and
gcc 14, ...), set through ``VRG_VERSION_WINDOW``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin.vrg_ci_resolve import main
from vergil_tooling.lib.versions.resolve import resolve_config
from vergil_tooling.lib.versions.window import WINDOW_ENV

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.config import VergilConfig
    from vergil_tooling.lib.versions.resolve import Resolution


def _write_config(
    root: Path, *, language: str | None, repo_type: str = "library", ci: str = ""
) -> None:
    lang = f'primary-language = "{language}"\n' if language else ""
    (root / "vergil.toml").write_text(
        "[project]\n"
        f'repository-type = "{repo_type}"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        f"{lang}"
        f"[ci]\n{ci}"
        '[dependencies]\nvergil = "v2.1"\n'
    )


def _lines(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.strip().splitlines())


def test_cli_json_is_the_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(tmp_path, language="python")
    assert main(["--repo-root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "versions": ["3.14", "3.13", "3.12"],
        "primary-version": "3.14",
        "primary-container-tag": "3.14",
        "container-tags": {"3.14": "3.14", "3.13": "3.13", "3.12": "3.12"},
        "container-suffixes": {"3.14": "python", "3.13": "python", "3.12": "python"},
    }


def test_cli_github_output_matches_derive_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exactly the three keys derive.sh writes to $GITHUB_OUTPUT, with the same
    # meanings: a compact JSON array, the primary, and its family-free tag.
    _write_config(tmp_path, language="cpp", ci='versions = ["gcc-14", "clang-20"]\n')
    assert main(["--format", "github-output", "--repo-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert _lines(out) == {
        "versions": '["clang-20","gcc-14"]',
        "primary-version": "clang-20",
        "primary-container-tag": "20",
    }


def test_cli_reads_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, language="go", repo_type="application")
    monkeypatch.chdir(tmp_path)
    assert main(["--format", "github-output"]) == 0
    assert _lines(capsys.readouterr().out)["versions"] == '["1.26"]'


def test_cli_no_language_emits_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A repo with no language is not resolved, so the window is never read.
    monkeypatch.setenv(WINDOW_ENV, str(tmp_path / "absent.json"))
    _write_config(tmp_path, language=None)
    assert main(["--format", "github-output", "--repo-root", str(tmp_path)]) == 0
    assert _lines(capsys.readouterr().out) == {
        "versions": '["latest"]',
        "primary-version": "latest",
        "primary-container-tag": "latest",
    }


def test_cli_error_exits_1_with_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(tmp_path, language="python", ci='versions = ["3.11"]\n')
    assert main(["--repo-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vrg-ci-resolve: error: [ci].versions pins 3.11")
    assert "remove it from [ci].versions" in captured.err


def test_cli_window_failure_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(WINDOW_ENV, str(tmp_path / "absent.json"))
    _write_config(tmp_path, language="python")
    assert main(["--repo-root", str(tmp_path)]) == 1
    assert "cannot read the version window" in capsys.readouterr().err


def test_cli_missing_config_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--repo-root", str(tmp_path)]) == 1
    assert "vergil.toml not found" in capsys.readouterr().err


def test_cli_invalid_config_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text("[invalid\n")
    assert main(["--repo-root", str(tmp_path)]) == 1
    assert "not valid TOML" in capsys.readouterr().err


@pytest.mark.parametrize("fmt", ["json", "github-output"])
def test_cli_app_multi_pin_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str
) -> None:
    # An app builds exactly one version: a list of two fails CI setup loudly.
    _write_config(
        tmp_path, language="python", repo_type="tooling", ci='versions = ["3.12", "3.13"]\n'
    )
    assert main(["--format", fmt, "--repo-root", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vrg-ci-resolve: error: [ci].versions pins 2 versions")
    assert "pin a single version" in captured.err
    assert 'set [project].repository-type = "library"' in captured.err


@pytest.mark.parametrize(
    ("fmt", "prefix"), [("json", "warning: "), ("github-output", "::warning::")]
)
def test_cli_prints_warnings_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str,
    prefix: str,
) -> None:
    def _with_warning(cfg: VergilConfig, **kwargs: Any) -> Resolution | None:
        resolution = resolve_config(cfg, **kwargs)
        assert resolution is not None
        return replace(resolution, warnings=("a resolution warning",))

    monkeypatch.setattr("vergil_tooling.bin.vrg_ci_resolve.resolve_config", _with_warning)
    _write_config(tmp_path, language="python")
    assert main(["--format", fmt, "--repo-root", str(tmp_path)]) == 0
    assert capsys.readouterr().err == f"{prefix}a resolution warning\n"


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "usage: vrg-ci-resolve" in capsys.readouterr().out
