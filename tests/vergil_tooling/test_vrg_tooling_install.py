"""vrg-tooling-install: the Mac's locked install of an exact release (vergil-tooling#4827)."""

from __future__ import annotations

import os
import tempfile
from typing import TYPE_CHECKING

import pytest

from tests.vergil_tooling.tooling_install_stubs import STUB_SHA, stub_bin
from vergil_tooling.bin import vrg_tooling_install
from vergil_tooling.lib.tooling_install import (
    GIT_URL,
    Installed,
    ToolingInstallError,
    install_release,
)

if TYPE_CHECKING:
    from pathlib import Path

_TAG = "v2.1.10"
_UV_VARS = ("UV_PYTHON", "UV_PYTHON_PREFERENCE", "UV_NO_MANAGED_PYTHON")


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the git and uv stubs first on PATH, and the temp root under *tmp_path*.

    Returns the stub call log. The three ``UV_*`` interpreter variables are
    set, to show the install clears them.
    """
    log = tmp_path / "stub.log"
    log.write_text("")
    monkeypatch.setenv("PATH", os.pathsep.join([str(stub_bin(tmp_path)), "/usr/bin", "/bin"]))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("UV_PYTHON", "/opt/hostile/python")
    monkeypatch.setenv("UV_PYTHON_PREFERENCE", "only-system")
    monkeypatch.setenv("UV_NO_MANAGED_PYTHON", "1")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return log


def _calls(log: Path, command: str) -> list[list[str]]:
    rows = [line.split("\t") for line in log.read_text().splitlines()]
    return [row[1:] for row in rows if row[0] == command]


def _temp_left(log: Path) -> list[Path]:
    return list((log.parent / "temp").iterdir())


def test_installs_the_tag_commit_with_its_lock(stubs: Path) -> None:
    assert install_release(_TAG) == Installed(tag=_TAG, sha=STUB_SHA)

    git = _calls(stubs, "git")
    assert git[0][:2] == ["init", "-q"]
    src = git[0][2]
    assert git[1] == ["-C", src, "fetch", "-q", "--depth", "1", GIT_URL, f"refs/tags/{_TAG}"]
    assert git[2] == ["-C", src, "checkout", "-q", "FETCH_HEAD"]
    assert git[3] == ["-C", src, "rev-parse", "HEAD"]
    assert not any("ls-remote" in call for call in git)

    export, install = _calls(stubs, "uv")
    assert export[0] == "export"
    for flag in ("--no-config", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"):
        assert flag in export
    constraints = export[export.index("-o") + 1]
    assert install[:2] == ["tool", "install"]
    for flag in ("--force", "--no-config", "--managed-python"):
        assert flag in install
    assert install[install.index("--python") + 1] == "3.15"
    assert install[install.index("-c") + 1] == constraints
    # The code installs from the same commit the lock was read from.
    assert install[-1] == f"vergil-tooling @ git+{GIT_URL}@{STUB_SHA}"

    uv_env = _calls(stubs, "uv-env")
    for seen in uv_env:
        env = dict(item.split("=", 1) for item in seen)
        for name in _UV_VARS:
            assert env[name] == "<unset>"
    # The export runs in the checkout, where the tag's uv.lock is.
    assert dict(item.split("=", 1) for item in uv_env[0])["PWD"] == src
    assert _temp_left(stubs) == []


@pytest.mark.parametrize("tag", ["v2.1", "2.1.10", "develop", "v2.1.10-rc1"])
def test_refuses_anything_but_an_exact_tag(stubs: Path, tag: str) -> None:
    with pytest.raises(ToolingInstallError, match="not an exact release tag"):
        install_release(tag)
    assert _calls(stubs, "git") == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"STUB_FETCH_RC": "3"}, f"fetching tag {_TAG} from {GIT_URL} failed .exit 3."),
        ({"STUB_REV_PARSE": "not-a-sha"}, "not a commit SHA"),
        ({"STUB_EXPORT_RC": "4"}, f"exporting the uv.lock of {_TAG} .*failed .exit 4.: error"),
        ({"STUB_EXPORT": ""}, "is empty"),
        ({"STUB_EXPORT": "rich==15.0.0\n-e ./local\n"}, "non-registry package: '-e ./local'"),
        ({"STUB_EXPORT": "vergil-tooling==2.1.10\n"}, "non-registry package"),
        ({"STUB_EXPORT": "foo @ git+https://x/y\ngit+https://x/y\n"}, "non-registry package"),
    ],
)
def test_a_failed_step_stops_before_the_install(
    stubs: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ToolingInstallError, match=message):
        install_release(_TAG)
    assert [c for c in _calls(stubs, "uv") if c[0] == "tool"] == []
    assert _temp_left(stubs) == []


def test_a_failed_install_raises(stubs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_INSTALL_RC", "5")
    with pytest.raises(ToolingInstallError, match=rf"uv tool install of {_TAG} .*failed .exit 5."):
        install_release(_TAG)
    assert _temp_left(stubs) == []


def test_a_missing_tool_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ToolingInstallError, match="git could not run"):
        install_release(_TAG)


def test_main_reports_the_install(stubs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_tooling_install.main([_TAG]) == 0
    out = capsys.readouterr().out
    assert f"vrg-tooling-install: installed {_TAG} (commit {STUB_SHA})" in out


def test_main_reports_a_failure(
    stubs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STUB_FETCH_RC", "3")
    assert vrg_tooling_install.main([_TAG]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"vrg-tooling-install: fetching tag {_TAG}")
    assert _calls(stubs, "uv") == []


def test_main_needs_a_tag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_tooling_install.main([])
    assert exc.value.code == 2
    assert "vX.Y.Z" in capsys.readouterr().err
