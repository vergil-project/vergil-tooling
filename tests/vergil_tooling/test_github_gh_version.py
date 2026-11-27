"""Every direct gh call checks gh's minimum version first (#5046).

The conftest's ``_hermetic_gh_version`` fixture turns :func:`github.require_gh`
off for the rest of the suite; this module captures the real check at import
and puts it back, then drives the gh callers against a fake gh on PATH.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import cd_runs, gh_version, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import collect
from vergil_tooling.lib.package.repo_setup import local_run

if TYPE_CHECKING:
    from pathlib import Path

_REAL_REQUIRE_GH = github.require_gh

_OLD = "gh version 2.96.0 (2026-07-01)"
_CURRENT = "gh version 2.103.3 (2026-10-26)"


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A PATH entry for the fake gh, a private gh-version cache, and the real check."""
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", f"{path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(github, "require_gh", _REAL_REQUIRE_GH)
    return path


def _fake_gh(bin_dir: Path, version_line: str, stdout: str = "[]") -> None:
    """A ``gh`` that logs each argv, prints *version_line* for ``--version``, else *stdout*."""
    gh = bin_dir / "gh"
    log = bin_dir / "calls"
    gh.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'if [ "$1" = --version ]; then\n'
        f"  printf '%s\\n' '{version_line}'\n"
        "else\n"
        f"  printf '%s\\n' '{stdout}'\n"
        "fi\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)


def _calls(bin_dir: Path) -> list[str]:
    log = bin_dir / "calls"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_require_gh_refuses_an_old_gh(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _OLD)
    with pytest.raises(gh_version.GhVersionError, match=r"gh 2\.96\.0 .*needs gh 2\.97\.0"):
        github.require_gh()


def test_gh_env_checks_gh_before_minting_a_token(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(bin_dir, _OLD)

    def _no_token(**_: object) -> str:
        pytest.fail("a token was minted for an old gh")

    monkeypatch.setattr(github, "get_installation_token", _no_token)
    with pytest.raises(gh_version.GhVersionError):
        github._gh_env()


def test_library_call_refuses_an_old_gh_without_running_it(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _OLD)
    with pytest.raises(gh_version.GhVersionError):
        github.read_output("api", "user")
    assert _calls(bin_dir) == ["--version"]


def test_library_call_runs_a_current_gh(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _CURRENT, stdout="hello")
    assert github.read_output("api", "user") == "hello"
    assert _calls(bin_dir) == ["--version", "api user"]


def test_cd_run_list_refuses_an_old_gh(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _OLD)
    with pytest.raises(gh_version.GhVersionError):
        cd_runs._list_cd_runs("develop", repo="o/r")
    assert _calls(bin_dir) == ["--version"]


def test_cd_run_view_raises_an_old_gh_rather_than_a_failed_read(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _OLD)
    with pytest.raises(gh_version.GhVersionError):
        cd_runs._read_run_view(42)
    assert _calls(bin_dir) == ["--version"]


def test_cd_run_list_runs_a_current_gh(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _CURRENT)
    assert cd_runs._list_cd_runs("develop", repo="o/r") == []
    assert _calls(bin_dir)[0] == "--version"
    assert _calls(bin_dir)[1].startswith("run list --repo o/r")


def test_package_index_gh_call_is_a_package_error_for_an_old_gh(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _OLD)
    with pytest.raises(PackageError, match=r"needs gh 2\.97\.0"):
        collect.packaged_tags("o/r", local_run)
    assert _calls(bin_dir) == ["--version"]


def test_package_index_local_tool_is_not_gh_checked(bin_dir: Path) -> None:
    _fake_gh(bin_dir, _OLD)
    ran: list[tuple[str, ...]] = []

    def _run(*argv: str) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    collect._invoke(_run, "rpm", "-qp", "x.rpm")
    assert ran == [("rpm", "-qp", "x.rpm")]
    assert _calls(bin_dir) == []


def test_a_vrg_tool_refuses_an_old_gh_with_one_line_and_exit_1(
    bin_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_gh(bin_dir, _OLD)

    @cli_main
    def main(argv: list[str] | None = None) -> int:
        github.read_output("api", "user")
        return 0

    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.startswith("test-github-gh-version: gh 2.96.0 (")
    assert "needs gh 2.97.0 or newer" in err
    assert len(err.splitlines()) == 1
    assert _calls(bin_dir) == ["--version"]
