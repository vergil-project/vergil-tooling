"""Tests for vergil_tooling.lib.package.install_test (spec §8.1, §6.5)."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.package import PackageError, install_test
from vergil_tooling.lib.package.matrix import TestCell

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

_BASE_TOML = """\
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

_PYTHON_PKG = """
[package]
builder = "python"
vendor = "vergil"
summary = "Shared tooling"
smoke = "vrg-whoami --mode"

[package.python]
runtime = "3.14.4"
"""

_STAGED_PKG = """
[package]
builder = "staged"
vendor = "vergil"
name = "vergil-thing"
summary = "A thing"
smoke = "thing --version"

[package.staged]
build-command = "true"
"""

_TOOLING_LISTING = "/.\n/usr/bin/vrg-whoami\n/opt/vergil/vergil-tooling/venv/bin/python\n"


def _repo_with_python_package(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vergil.toml").write_text(_BASE_TOML + _PYTHON_PKG)
    (repo / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    # builder = "python" requires a uv.lock next to vergil.toml (spec §5.4, #3077).
    (repo / "uv.lock").write_text("version = 1\n")
    return repo


def _repo_with_staged_package(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vergil.toml").write_text(_BASE_TOML + _STAGED_PKG)
    return repo


def _arts(tmp_path: Path, *names: str) -> Path:
    arts = tmp_path / "arts"
    arts.mkdir()
    for n in names:
        (arts / n).write_bytes(b"x")
    return arts


def _fake_run(
    calls: list[str],
    *,
    listing: str,
    resolve: dict[str, str] | None = None,
    residue: Iterable[str] = (),
    fail: str | None = None,
    raise_on_absent: bool = False,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A ``run`` fake: records every call; ``test -e`` exits 1 unless the path is residue."""
    left = set(residue)

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        line = " ".join(argv)
        calls.append(line)
        if fail is not None and fail in line:
            raise subprocess.CalledProcessError(1, list(argv), "", "boom")
        if argv[:2] in (("dpkg", "-L"), ("rpm", "-ql")):
            return subprocess.CompletedProcess(argv, 0, listing, "")
        if argv[:2] == ("test", "-e"):
            if argv[2] in left:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if raise_on_absent:
                raise subprocess.CalledProcessError(1, list(argv), "", "")
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[-1].startswith("command -v "):
            cmd = argv[-1].removeprefix("command -v ")
            out = (resolve or {}).get(cmd, f"/usr/bin/{cmd}")
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def _no_bootstrap(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    boot: list[tuple[str, str]] = []

    monkeypatch.setattr(
        install_test.repo_setup,
        "bootstrap",
        lambda run, org, fmt, suite, sudo: boot.append((fmt, suite)),
    )
    return boot


# --- artifact selection ------------------------------------------------------


def test_selects_shared_deb_for_its_arch(tmp_path: Path) -> None:
    for f in ("t_2.1.240-1_amd64.deb", "t_2.1.240-1_arm64.deb", "t-2.1.240-1.x86_64.rpm"):
        (tmp_path / f).write_bytes(b"x")
    cell = TestCell(
        "test-ubuntu-24.04-amd64",
        "ubuntu/24.04/amd64",
        "amd64",
        "r",
        "ubuntu:24.04",
        "deb",
        "noble",
        False,
    )
    assert (
        install_test.select_artifact(tmp_path, "t", cell, noarch=False).name
        == "t_2.1.240-1_amd64.deb"
    )


def test_selects_native_suffix(tmp_path: Path) -> None:
    for f in ("t_2.1.240-1_amd64.deb", "t_2.1.240-1~resolute_amd64.deb"):
        (tmp_path / f).write_bytes(b"x")
    cell = TestCell(
        "x", "ubuntu/26.04/amd64", "amd64", "r", "ubuntu:26.04", "deb", "resolute", True
    )
    assert (
        install_test.select_artifact(tmp_path, "t", cell, noarch=False).name
        == "t_2.1.240-1~resolute_amd64.deb"
    )


def test_shared_deb_ignores_native_suffix(tmp_path: Path) -> None:
    for f in ("t_2.1.240-1_amd64.deb", "t_2.1.240-1~resolute_amd64.deb"):
        (tmp_path / f).write_bytes(b"x")
    cell = TestCell(
        "x", "ubuntu/26.04/amd64", "amd64", "r", "ubuntu:26.04", "deb", "resolute", False
    )
    assert (
        install_test.select_artifact(tmp_path, "t", cell, noarch=False).name
        == "t_2.1.240-1_amd64.deb"
    )


def test_deb_noarch(tmp_path: Path) -> None:
    for f in ("k_1.0.0-1_all.deb", "k-1.0.0-1.noarch.rpm"):
        (tmp_path / f).write_bytes(b"x")
    cell = TestCell("x", "ubuntu/24.04/arm64", "arm64", "r", "ubuntu:24.04", "deb", "noble", False)
    got = install_test.select_artifact(tmp_path, "k", cell, noarch=True)
    assert got.name == "k_1.0.0-1_all.deb"


def test_rpm_noarch(tmp_path: Path) -> None:
    (tmp_path / "k-1.0.0-1.noarch.rpm").write_bytes(b"x")
    cell = TestCell("x", "rhel/9/arm64", "arm64", "r", "ubi9", "rpm", "el9", False)
    assert (
        install_test.select_artifact(tmp_path, "k", cell, noarch=True).name
        == "k-1.0.0-1.noarch.rpm"
    )


def test_rpm_shared_uses_rpm_arch(tmp_path: Path) -> None:
    for f in ("t-2.1.240-1.x86_64.rpm", "t-2.1.240-1.aarch64.rpm", "t-2.1.240-1.el9.x86_64.rpm"):
        (tmp_path / f).write_bytes(b"x")
    cell = TestCell("x", "rhel/9/arm64", "arm64", "r", "ubi9", "rpm", "el9", False)
    assert (
        install_test.select_artifact(tmp_path, "t", cell, noarch=False).name
        == "t-2.1.240-1.aarch64.rpm"
    )


def test_rpm_native_suffix(tmp_path: Path) -> None:
    for f in ("t-2.1.240-1.x86_64.rpm", "t-2.1.240-1.el10.x86_64.rpm"):
        (tmp_path / f).write_bytes(b"x")
    cell = TestCell("x", "rhel/10/amd64", "amd64", "r", "ubi10", "rpm", "el10", True)
    assert (
        install_test.select_artifact(tmp_path, "t", cell, noarch=False).name
        == "t-2.1.240-1.el10.x86_64.rpm"
    )


def test_zero_or_many_matches_is_fatal(tmp_path: Path) -> None:
    cell = TestCell("x", "rhel/9/amd64", "amd64", "r", "ubi9", "rpm", "el9", False)
    with pytest.raises(
        PackageError, match=r"expected exactly one artifact for t on rhel/9/amd64, found 0"
    ):
        install_test.select_artifact(tmp_path, "t", cell, noarch=False)
    for f in ("t-2.1.240-1.x86_64.rpm", "t-2.1.241-1.x86_64.rpm"):
        (tmp_path / f).write_bytes(b"x")
    with pytest.raises(
        PackageError,
        match=r"expected exactly one artifact for t on rhel/9/amd64, found 2 .*t-2\.1\.241",
    ):
        install_test.select_artifact(tmp_path, "t", cell, noarch=False)


# --- the install-test sequence -----------------------------------------------


def test_sequence_python_product_deb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    boot = _no_bootstrap(monkeypatch)
    calls: list[str] = []
    run = _fake_run(calls, listing=_TOOLING_LISTING)
    rep = tmp_path / "r.json"
    install_test.run_install_test(repo, "test-ubuntu-24.04-amd64", arts, rep, run=run)
    assert boot == [("deb", "noble")]
    artifact = arts / "vergil-tooling_2.1.240-1_amd64.deb"
    assert calls == [
        "apt-get update",
        f"apt-get install -y {artifact.resolve()}",
        "dpkg -L vergil-tooling",
        f"{install_test.CLEAN_ENV} bash -c vrg-whoami --mode",
        f"{install_test.CLEAN_ENV} bash -c command -v vrg-whoami",
        "apt-get purge -y vergil-tooling",
        "test -e /opt/vergil/vergil-tooling",
        "test -e /usr/bin/vrg-whoami",
    ]
    assert json.loads(rep.read_text()) == {
        "cell": "test-ubuntu-24.04-amd64",
        "target": "ubuntu/24.04/amd64",
        "artifact": "vergil-tooling_2.1.240-1_amd64.deb",
        "units": [],
        "smoke": "pass",
        "residue": [],
    }


def test_bootstrap_uses_the_vergil_org_without_sudo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling-2.1.240-1.el9.aarch64.rpm")
    native = _PYTHON_PKG.replace("[package.python]", 'native = ["rhel/9/*"]\n\n[package.python]')
    (repo / "vergil.toml").write_text(_BASE_TOML + native)
    seen: list[tuple[object, ...]] = []

    def fake(run: object, org: object, fmt: str, suite: str, *, sudo: bool) -> None:
        seen.append((run, org, fmt, suite, sudo))

    monkeypatch.setattr(install_test.repo_setup, "bootstrap", fake)
    run = _fake_run([], listing="")
    install_test.run_install_test(repo, "test-rhel-9-arm64", arts, tmp_path / "r.json", run=run)
    assert seen == [(run, install_test.orgs.for_vendor("vergil"), "rpm", "el9", False)]


def test_sequence_staged_product_rpm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_staged_package(tmp_path)
    arts = _arts(tmp_path, "vergil-thing-1.0.0-1.x86_64.rpm", "vergil-thing_1.0.0-1_amd64.deb")
    boot = _no_bootstrap(monkeypatch)
    calls: list[str] = []
    run = _fake_run(calls, listing="/usr/bin/thing\n/opt/vergil/vergil-thing/x\n")
    install_test.run_install_test(repo, "test-rhel-10-amd64", arts, tmp_path / "r.json", run=run)
    assert boot == []
    assert calls == [
        f"dnf install -y {(arts / 'vergil-thing-1.0.0-1.x86_64.rpm').resolve()}",
        "rpm -ql vergil-thing",
        f"{install_test.CLEAN_ENV} bash -c thing --version",
        f"{install_test.CLEAN_ENV} bash -c command -v thing",
        "dnf remove -y vergil-thing",
        "test -e /opt/vergil/vergil-thing",
        "test -e /usr/bin/thing",
    ]


def test_staged_product_does_not_bootstrap_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_staged_package(tmp_path)
    arts = _arts(tmp_path, "vergil-thing_1.0.0-1_amd64.deb")
    boot = _no_bootstrap(monkeypatch)
    calls: list[str] = []
    run = _fake_run(calls, listing="/usr/bin/thing\n")
    install_test.run_install_test(
        repo, "test-ubuntu-24.04-amd64", arts, tmp_path / "r.json", run=run
    )
    assert boot == []
    installs = [c for c in calls if c.startswith("apt-get install -y ")]
    assert installs == [f"apt-get install -y {(arts / 'vergil-thing_1.0.0-1_amd64.deb').resolve()}"]


def test_residue_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    _no_bootstrap(monkeypatch)
    calls: list[str] = []
    run = _fake_run(
        calls,
        listing=_TOOLING_LISTING,
        residue=("/opt/vergil/vergil-tooling", "/usr/bin/vrg-whoami"),
    )
    rep = tmp_path / "r.json"
    with pytest.raises(
        PackageError,
        match=r"left behind after removal: /opt/vergil/vergil-tooling, /usr/bin/vrg-whoami",
    ):
        install_test.run_install_test(repo, "test-ubuntu-24.04-amd64", arts, rep, run=run)
    assert not rep.exists()


def test_absent_path_raising_called_process_error_is_not_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # local_run uses check=True, so a missing path raises instead of returning 1.
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    _no_bootstrap(monkeypatch)
    run = _fake_run([], listing=_TOOLING_LISTING, raise_on_absent=True)
    rep = tmp_path / "out" / "r.json"
    install_test.run_install_test(repo, "test-ubuntu-24.04-amd64", arts, rep, run=run)
    assert json.loads(rep.read_text())["residue"] == []


def test_units_are_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    _no_bootstrap(monkeypatch)
    calls: list[str] = []
    listing = (
        _TOOLING_LISTING
        + "/usr/lib/systemd/system\n"
        + "/usr/lib/systemd/system/x.service\n"
        + "/usr/lib/systemd/system/x.timer\n"
        + "/usr/lib/systemd/system/x.socket\n"
        + "/usr/lib/systemd/system/x.conf\n"
        + "/usr/share/doc/x.service\n"
    )
    run = _fake_run(calls, listing=listing)
    rep = tmp_path / "r.json"
    install_test.run_install_test(repo, "test-ubuntu-24.04-amd64", arts, rep, run=run)
    units = [
        "/usr/lib/systemd/system/x.service",
        "/usr/lib/systemd/system/x.timer",
        "/usr/lib/systemd/system/x.socket",
    ]
    i = calls.index("apt-get install -y systemd")
    assert calls[i - 1] == "dpkg -L vergil-tooling"
    assert calls[i + 1 : i + 4] == [f"systemd-analyze verify {u}" for u in units]
    assert calls[i + 4] == f"{install_test.CLEAN_ENV} bash -c vrg-whoami --mode"
    assert json.loads(rep.read_text())["units"] == units


def test_rpm_units_install_systemd_with_dnf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_staged_package(tmp_path)
    arts = _arts(tmp_path, "vergil-thing-1.0.0-1.aarch64.rpm")
    calls: list[str] = []
    run = _fake_run(calls, listing="/usr/lib/systemd/system/thing.service\n")
    install_test.run_install_test(repo, "test-rhel-9-arm64", arts, tmp_path / "r.json", run=run)
    assert "dnf install -y systemd" in calls
    assert "systemd-analyze verify /usr/lib/systemd/system/thing.service" in calls


def test_smoke_runs_with_sanitized_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The uv copy of vergil-tooling that drives the test lives in ~/.local/bin;
    # the smoke must never resolve it.
    system_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    expected = f"env -i HOME=/root PATH={system_path}"
    assert expected == install_test.CLEAN_ENV
    assert ".local" not in install_test.CLEAN_ENV
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    _no_bootstrap(monkeypatch)
    argvs: list[tuple[str, ...]] = []
    inner = _fake_run(
        [],
        listing=_TOOLING_LISTING,
        resolve={"vrg-whoami": "/root/.local/bin/vrg-whoami"},
    )

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        argvs.append(argv)
        return inner(*argv)

    with pytest.raises(
        PackageError,
        match=(
            r"shim vrg-whoami resolves to /root/\.local/bin/vrg-whoami, "
            r"expected /usr/bin/vrg-whoami"
        ),
    ):
        install_test.run_install_test(
            repo, "test-ubuntu-24.04-amd64", arts, tmp_path / "r.json", run=run
        )
    clean = tuple(install_test.CLEAN_ENV.split())
    smoke = (*clean, "bash", "-c", "vrg-whoami --mode")
    shim = (*clean, "bash", "-c", "command -v vrg-whoami")
    assert smoke in argvs
    assert shim in argvs
    assert argvs.index(smoke) < argvs.index(shim)
    assert not any(a[:1] == ("apt-get",) and "purge" in a for a in argvs)


def test_smoke_failure_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    _no_bootstrap(monkeypatch)
    run = _fake_run([], listing=_TOOLING_LISTING, fail="bash -c vrg-whoami --mode")
    with pytest.raises(
        PackageError,
        match=r"smoke command failed on ubuntu/24\.04/amd64: vrg-whoami --mode \(exit 1\)\nboom",
    ):
        install_test.run_install_test(
            repo, "test-ubuntu-24.04-amd64", arts, tmp_path / "r.json", run=run
        )


def test_unresolvable_shim_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    _no_bootstrap(monkeypatch)
    run = _fake_run([], listing=_TOOLING_LISTING, fail="command -v vrg-whoami")
    with pytest.raises(
        PackageError, match=r"shim vrg-whoami does not resolve on ubuntu/24\.04/amd64"
    ):
        install_test.run_install_test(
            repo, "test-ubuntu-24.04-amd64", arts, tmp_path / "r.json", run=run
        )


def test_other_command_failures_propagate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo_with_python_package(tmp_path)
    arts = _arts(tmp_path, "vergil-tooling_2.1.240-1_amd64.deb")
    _no_bootstrap(monkeypatch)
    run = _fake_run([], listing=_TOOLING_LISTING, fail="apt-get install")
    with pytest.raises(subprocess.CalledProcessError):
        install_test.run_install_test(
            repo, "test-ubuntu-24.04-amd64", arts, tmp_path / "r.json", run=run
        )


def test_only_top_level_usr_bin_entries_are_shims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_staged_package(tmp_path)
    arts = _arts(tmp_path, "vergil-thing_1.0.0-1_amd64.deb")
    calls: list[str] = []
    run = _fake_run(calls, listing="/usr/bin\n/usr/bin/thing\n/usr/bin/sub/deep\n")
    install_test.run_install_test(
        repo, "test-ubuntu-24.04-amd64", arts, tmp_path / "r.json", run=run
    )
    resolved = [c for c in calls if "command -v" in c]
    assert resolved == [f"{install_test.CLEAN_ENV} bash -c command -v thing"]


def test_requires_package_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_BASE_TOML)
    with pytest.raises(PackageError, match=r"no \[package\] section"):
        install_test.run_install_test(
            tmp_path, "test-ubuntu-24.04-amd64", tmp_path, tmp_path / "r.json"
        )


def test_unknown_cell_is_fatal(tmp_path: Path) -> None:
    repo = _repo_with_staged_package(tmp_path)
    with pytest.raises(PackageError, match=r"unknown test cell 'nope' \(known: test-"):
        install_test.run_install_test(repo, "nope", tmp_path, tmp_path / "r.json")
