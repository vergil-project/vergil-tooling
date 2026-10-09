"""Tests for vergil_tooling.lib.package.repo_setup (spec §7.4, §9)."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vergil_tooling.lib.package import PackageError, repo_setup
from vergil_tooling.lib.package.orgs import OrgRepo

_PRIMARY = "0123456789ABCDEF0123456789ABCDEF01234567"
_SUBKEY = "9999888877776666555544443333222211110000"
_COLONS = f"""\
pub:u:255:22:ABCDEF0123456789:1700000000:2015000000::u:::cC:::::ed25519:::0:
fpr:::::::::{_PRIMARY}:
uid:u::::1700000000::HASH::vergil-project packages <packages@vergil-project.invalid>::::::::::0:
sub:u:255:22:1111222233334444:1700000000:1763000000:::::s:::::ed25519::
fpr:::::::::{_SUBKEY}:
"""
_ORG = OrgRepo(
    "vergil",
    "vergil-project",
    "https://vergil-project.github.io/packages",
    _PRIMARY,
    "vergil-archive-keyring",
)
_TMP = "/guest/scratch.AbC123"  # what the fake ``mktemp -d`` answers


_FAST = (
    "-o Acquire::Retries=3 -o Acquire::Retries::Delay=false"
    " -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20"
    " -o DPkg::Lock::Timeout=60"
)
_SCOPE = (
    "-o Dir::Etc::sourcelist=/etc/apt/sources.list.d/vergil-bootstrap.sources"
    " -o Dir::Etc::sourceparts=-"
)
_SCOPED = f"{_SCOPE} -o APT::Get::List-Cleanup=0"
_FULL = "installed\nsource\nkey\n"  # the keyring probe on a host that already has the keyring
_DEB_STALE = (
    "rm -f /etc/apt/sources.list.d/vergil-bootstrap.sources /etc/apt/keyrings/vergil-bootstrap.asc"
)
_RPM_STALE = (
    "rm -f /etc/yum.repos.d/vergil-bootstrap.repo /etc/pki/rpm-gpg/RPM-GPG-KEY-vergil-bootstrap"
)
_DEB_PROBE = repo_setup.keyring_probe(
    "deb",
    "vergil-archive-keyring",
    "/etc/apt/sources.list.d/vergil.sources",
    "/usr/share/keyrings/vergil-archive-keyring.asc",
)
_RPM_PROBE = repo_setup.keyring_probe(
    "rpm",
    "vergil-archive-keyring",
    "/etc/yum.repos.d/vergil.repo",
    "/etc/pki/rpm-gpg/RPM-GPG-KEY-vergil",
)


class FakeRun:
    """Records every ``run(*argv)`` call; answers ``mktemp``, ``gpg`` and the prerequisite probe.

    ``missing`` is what the prerequisite probe reports (one name per line):
    nothing by default, as on a provisioned VM or a set-up CI container.
    """

    def __init__(
        self,
        colons: str = _COLONS,
        *,
        mktemp: str = f"{_TMP}\n",
        fail_on: str | None = None,
        missing: str = "",
        state: tuple[str, ...] = ("", _FULL),
        installed_colons: str = _COLONS,
    ) -> None:
        self.colons = colons
        self.mktemp = mktemp
        self.fail_on = fail_on
        self.missing = missing
        self.state = list(state)
        self.installed_colons = installed_colons
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self.fail_on is not None and self.fail_on in " ".join(argv):
            raise subprocess.CalledProcessError(100, argv, "", "E: boom")
        out = ""
        if "--with-colons" in argv:
            out = self.colons if argv[-1].startswith(_TMP) else self.installed_colons
        elif argv[:2] == ("bash", "-c") and "echo installed" in argv[-1]:
            out = self.state.pop(0)
        elif argv[:1] == ("mktemp",):
            out = self.mktemp
        elif argv[-1] in (repo_setup.DEB_PREREQ_PROBE, repo_setup.RPM_PREREQ_PROBE):
            out = self.missing
        return subprocess.CompletedProcess(argv, 0, out, "")

    @property
    def flat(self) -> list[str]:
        return [" ".join(c) for c in self.calls]

    def index(self, prefix: str) -> int:
        return next(i for i, c in enumerate(self.flat) if c.startswith(prefix))


# --- local_run -------------------------------------------------------------


def test_local_run_captures_text_and_raises_on_failure() -> None:
    ok = repo_setup.local_run(sys.executable, "-c", "print('hi')")
    assert ok.stdout == "hi\n"
    with pytest.raises(subprocess.CalledProcessError):
        repo_setup.local_run(sys.executable, "-c", "raise SystemExit(3)")


# --- parse_primary_fingerprint -----------------------------------------------


def test_primary_fingerprint_is_the_fpr_after_pub() -> None:
    assert repo_setup.parse_primary_fingerprint(_COLONS) == _PRIMARY


def test_fpr_before_any_pub_is_ignored() -> None:
    colons = f"fpr:::::::::{_SUBKEY}:\n{_COLONS}"
    assert repo_setup.parse_primary_fingerprint(colons) == _PRIMARY


def test_no_pub_is_fatal() -> None:
    with pytest.raises(PackageError, match=r"no primary key"):
        repo_setup.parse_primary_fingerprint("sub:...\n")


@pytest.mark.parametrize("fpr", ["fpr:::\n", "fpr:::::::::\n", ""])
def test_pub_without_fingerprint_is_fatal(fpr: str) -> None:
    with pytest.raises(PackageError, match=r"no primary key"):
        repo_setup.parse_primary_fingerprint(f"pub:u:255:22:ABCD::::::::\n{fpr}")


def test_more_than_one_primary_key_is_fatal() -> None:
    # A key file carrying an extra primary would have every key in it trusted
    # as Signed-By, so a second key must never ride along with the pinned one.
    other = _COLONS.replace(_PRIMARY, "F" * 40)
    with pytest.raises(PackageError, match=r"exactly one primary key, found 2"):
        repo_setup.parse_primary_fingerprint(_COLONS + other)


# --- apt argv helpers ----------------------------------------------------------


def test_apt_get_carries_fail_fast_options() -> None:
    # A dead mirror connection must retry in seconds, not stall for minutes.
    assert repo_setup.apt_get("install", "-y", "x") == (
        "apt-get",
        "-o",
        "Acquire::Retries=3",
        "-o",
        "Acquire::Retries::Delay=false",
        "-o",
        "Acquire::http::Timeout=20",
        "-o",
        "Acquire::https::Timeout=20",
        "-o",
        "DPkg::Lock::Timeout=60",
        "install",
        "-y",
        "x",
    )


def test_scoped_update_refreshes_only_the_given_source() -> None:
    assert repo_setup.apt_scoped_update("/etc/apt/sources.list.d/x.sources") == (
        "apt-get",
        "-o",
        "Acquire::Retries=3",
        "-o",
        "Acquire::Retries::Delay=false",
        "-o",
        "Acquire::http::Timeout=20",
        "-o",
        "Acquire::https::Timeout=20",
        "-o",
        "DPkg::Lock::Timeout=60",
        "-o",
        "Dir::Etc::sourcelist=/etc/apt/sources.list.d/x.sources",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        "APT::Get::List-Cleanup=0",
        "update",
    )


def test_deb_source_path_is_the_keyring_postinst_source() -> None:
    assert repo_setup.deb_source_path(_ORG) == "/etc/apt/sources.list.d/vergil.sources"


@pytest.mark.parametrize(("out", "present"), [("present\n", True), ("", False), ("\n", False)])
def test_apt_indexes_present(out: str, present: bool) -> None:
    seen: list[tuple[str, ...]] = []

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")

    assert repo_setup.apt_indexes_present(run) is present
    assert seen == [("bash", "-c", repo_setup.APT_INDEXES_PROBE)]


def _probe(script: str, root: Path, path: str = "/usr/bin:/bin") -> str:
    """Run a probe script with ``root`` substituted for ``/`` in its paths, under ``path``."""
    rooted = script.replace("/var/lib/apt/lists/", f"{root}/lists/")
    env = {"PATH": path}
    return subprocess.run(
        ["/bin/bash", "-c", rooted], check=True, capture_output=True, text=True, env=env
    ).stdout


@pytest.mark.parametrize(
    ("names", "present"),
    [
        ([], False),
        (["lock", "partial"], False),
        (["x_dists_noble_InRelease"], False),
        (["x_dists_noble_main_binary-arm64_Packages"], True),
        (["x_dists_noble_main_binary-arm64_Packages.lz4"], True),
    ],
)
def test_apt_indexes_probe_script(tmp_path: Path, names: list[str], present: bool) -> None:
    lists = tmp_path / "lists"
    lists.mkdir()
    for n in names:
        (lists / n).write_text("")
    assert (_probe(repo_setup.APT_INDEXES_PROBE, tmp_path) == "present\n") is present


def _bin(tmp_path: Path, *tools: str) -> str:
    """A PATH holding only ``tools`` (as trivial scripts) plus the shell builtins."""
    d = tmp_path / "bin"
    d.mkdir()
    for t in tools:
        f = d / t
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    return str(d)


def test_deb_prereq_probe_reports_each_missing_package(tmp_path: Path) -> None:
    # Nothing at all on PATH: all three are missing (dpkg-query absent too).
    path = _bin(tmp_path)
    assert _probe(repo_setup.DEB_PREREQ_PROBE, tmp_path, path).split() == [
        "ca-certificates",
        "curl",
        "gnupg",
    ]


_GREP = f'#!/bin/sh\nexec {shutil.which("grep")} "$@"\n'


def test_deb_prereq_probe_reports_nothing_when_all_present(tmp_path: Path) -> None:
    d = Path(_bin(tmp_path, "curl", "gpg", "grep"))
    (d / "dpkg-query").write_text("#!/bin/sh\nprintf 'install ok installed'\n")
    (d / "dpkg-query").chmod(0o755)
    (d / "grep").write_text(_GREP)
    assert _probe(repo_setup.DEB_PREREQ_PROBE, tmp_path, str(d)) == ""


def test_deb_prereq_probe_treats_a_removed_package_as_missing(tmp_path: Path) -> None:
    d = Path(_bin(tmp_path, "curl", "gpg"))
    (d / "dpkg-query").write_text("#!/bin/sh\nprintf 'deinstall ok config-files'\n")
    (d / "dpkg-query").chmod(0o755)
    (d / "grep").write_text(_GREP)
    (d / "grep").chmod(0o755)
    assert _probe(repo_setup.DEB_PREREQ_PROBE, tmp_path, str(d)).split() == ["ca-certificates"]


def test_rpm_prereq_probe(tmp_path: Path) -> None:
    path = _bin(tmp_path)
    assert _probe(repo_setup.RPM_PREREQ_PROBE, tmp_path, path).split() == [
        "ca-certificates",
        "/usr/bin/curl",
        "/usr/bin/gpg",
    ]
    d = Path(path)
    for t in ("curl", "gpg", "rpm"):
        (d / t).write_text("#!/bin/sh\n")
        (d / t).chmod(0o755)
    assert _probe(repo_setup.RPM_PREREQ_PROBE, tmp_path, path) == ""


# --- bootstrap: apt ------------------------------------------------------------


def _assert_deb_write(write: tuple[str, ...], key_tmp: str) -> None:
    assert write[-2] == "-c"
    assert shlex.split(write[-1]) == [
        "umask",
        "022",
        "&&",
        "install",
        "-D",
        "-m",
        "0644",
        key_tmp,
        "/etc/apt/keyrings/vergil-bootstrap.asc",
        "&&",
        "printf",
        "%s",
        "Types: deb\n"
        "URIs: https://vergil-project.github.io/packages/deb\n"
        "Suites: noble\n"
        "Components: main\n"
        "Signed-By: /etc/apt/keyrings/vergil-bootstrap.asc\n",
        ">",
        "/etc/apt/sources.list.d/vergil-bootstrap.sources",
    ]


def test_apt_bootstrap_with_prerequisites_present_never_runs_a_full_update() -> None:
    run = FakeRun()
    repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    key_tmp = f"{_TMP}/vergil.asc"
    assert run.flat[0] == f"sudo {_DEB_STALE}"
    assert run.calls[1] == ("bash", "-c", repo_setup.DEB_PREREQ_PROBE)
    assert run.calls[2] == ("bash", "-c", _DEB_PROBE)
    assert run.calls[3] == ("mktemp", "-d")
    assert run.calls[4] == (
        "curl",
        "-fsSL",
        "https://vergil-project.github.io/packages/keys/vergil.asc",
        "-o",
        key_tmp,
    )
    assert run.calls[5] == ("gpg", "--show-keys", "--with-colons", key_tmp)
    assert run.calls[6][:3] == ("sudo", "bash", "-c")
    _assert_deb_write(run.calls[6], key_tmp)
    assert run.flat[7:] == [
        f"sudo apt-get {_FAST} {_SCOPED} update",
        f"sudo apt-get {_FAST} {_SCOPE} install -y vergil-archive-keyring",
        f"sudo {_DEB_STALE}",
        f"bash -c {_DEB_PROBE}",
        f"rm -rf {_TMP}",
    ]
    assert not any(c.endswith(" update") and _SCOPED not in c for c in run.flat)


def test_apt_bootstrap_installs_only_missing_prerequisites() -> None:
    # A fresh container: a full update is unavoidable (empty lists), but only once,
    # and only the missing packages are installed.
    run = FakeRun(missing="ca-certificates\ngnupg\n")
    repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=False)
    assert run.flat[:6] == [
        _DEB_STALE,
        f"bash -c {repo_setup.DEB_PREREQ_PROBE}",
        f"apt-get {_FAST} update",
        f"apt-get {_FAST} install -y ca-certificates gnupg",
        f"bash -c {_DEB_PROBE}",
        "mktemp -d",
    ]
    assert run.flat[-5:-3] == [
        f"apt-get {_FAST} {_SCOPED} update",
        f"apt-get {_FAST} {_SCOPE} install -y vergil-archive-keyring",
    ]


def test_apt_bootstrap_without_sudo_has_no_sudo_prefix() -> None:
    run = FakeRun(missing="curl\n")
    repo_setup.bootstrap(run, _ORG, "deb", "resolute", sudo=False)
    assert not any(c[0] == "sudo" for c in run.calls)
    write = run.index("bash -c umask")
    assert "Suites: resolute\n" in run.calls[write][2]


# --- bootstrap: dnf ------------------------------------------------------------


def _assert_rpm_write(write: tuple[str, ...], key_tmp: str) -> None:
    words = shlex.split(write[-1])
    assert words[:9] == [
        "umask",
        "022",
        "&&",
        "install",
        "-D",
        "-m",
        "0644",
        key_tmp,
        "/etc/pki/rpm-gpg/RPM-GPG-KEY-vergil-bootstrap",
    ]
    assert words[-2:] == [">", "/etc/yum.repos.d/vergil-bootstrap.repo"]
    assert words[-3] == (
        "[vergil-bootstrap]\n"
        "name=vergil packages (bootstrap)\n"
        "baseurl=https://vergil-project.github.io/packages/rpm/el$releasever/$basearch\n"
        "enabled=1\n"
        "gpgcheck=1\n"
        "repo_gpgcheck=1\n"
        "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-vergil-bootstrap\n"
    )


def test_dnf_bootstrap_with_prerequisites_present_skips_their_install() -> None:
    run = FakeRun()
    repo_setup.bootstrap(run, _ORG, "rpm", "el9", sudo=False)
    key_tmp = f"{_TMP}/vergil.asc"
    assert run.flat[:4] == [
        _RPM_STALE,
        f"bash -c {repo_setup.RPM_PREREQ_PROBE}",
        f"bash -c {_RPM_PROBE}",
        "mktemp -d",
    ]
    assert run.calls[5] == ("gpg", "--show-keys", "--with-colons", key_tmp)
    assert run.calls[6][:2] == ("bash", "-c")
    _assert_rpm_write(run.calls[6], key_tmp)
    assert run.flat[7:] == [
        "dnf install -y --repo vergil-bootstrap vergil-archive-keyring",
        _RPM_STALE,
        f"bash -c {_RPM_PROBE}",
        f"rm -rf {_TMP}",
    ]


def test_dnf_bootstrap_installs_only_missing_prerequisites() -> None:
    # File provides, not ``curl``: RHEL's preinstalled curl-minimal conflicts
    # with the full ``curl`` package, but satisfies ``/usr/bin/curl``.
    run = FakeRun(missing="/usr/bin/gpg\n")
    repo_setup.bootstrap(run, _ORG, "rpm", "el9", sudo=False)
    assert run.flat[:4] == [
        _RPM_STALE,
        f"bash -c {repo_setup.RPM_PREREQ_PROBE}",
        "dnf install -y /usr/bin/gpg",
        f"bash -c {_RPM_PROBE}",
    ]


def test_dnf_bootstrap_with_sudo() -> None:
    run = FakeRun(missing="ca-certificates\n")
    repo_setup.bootstrap(run, _ORG, "rpm", "el10", sudo=True)
    assert run.flat[0] == f"sudo {_RPM_STALE}"
    assert run.flat[1] == f"bash -c {repo_setup.RPM_PREREQ_PROBE}"
    assert run.flat[2] == "sudo dnf install -y ca-certificates"
    assert "sudo dnf install -y --repo vergil-bootstrap vergil-archive-keyring" in run.flat


# --- bootstrap: failures ----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_fingerprint_mismatch_is_fatal_before_any_source_is_written(fmt: str) -> None:
    run = FakeRun(_COLONS.replace(_PRIMARY, "F" * 40))
    with pytest.raises(PackageError, match=r"fingerprint mismatch: published F{40}, pinned"):
        repo_setup.bootstrap(run, _ORG, fmt, "noble", sudo=True)
    _assert_nothing_written_after_the_probe(run)
    assert run.flat[-1] == f"rm -rf {_TMP}"


def _assert_nothing_written_after_the_probe(run: FakeRun) -> None:
    after = run.flat[3:]  # stale cleanup, prerequisite probe, keyring probe
    assert not any("umask" in c or "keyring" in c or c.startswith("sudo") for c in after)


def test_key_with_an_extra_primary_is_fatal_before_any_source_is_written() -> None:
    run = FakeRun(_COLONS + _COLONS.replace(_PRIMARY, "F" * 40))
    with pytest.raises(PackageError, match=r"exactly one primary key"):
        repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    _assert_nothing_written_after_the_probe(run)


def test_key_download_failure_propagates_and_cleans_the_temp_dir() -> None:
    run = FakeRun(fail_on="curl -fsSL")
    with pytest.raises(subprocess.CalledProcessError):
        repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    _assert_nothing_written_after_the_probe(run)
    assert run.flat[-1] == f"rm -rf {_TMP}"


@pytest.mark.parametrize(
    ("fmt", "fail_on", "src"),
    [
        ("deb", "install -y vergil-archive-keyring", "/etc/apt/sources.list.d/"),
        ("deb", "APT::Get::List-Cleanup=0 update", "/etc/apt/sources.list.d/"),
        ("deb", "umask 022", "/etc/apt/sources.list.d/"),
        ("rpm", "--repo vergil-bootstrap", "/etc/yum.repos.d/"),
    ],
)
def test_failure_after_writing_still_removes_bootstrap_files(
    fmt: str, fail_on: str, src: str
) -> None:
    run = FakeRun(fail_on=fail_on)
    with pytest.raises(subprocess.CalledProcessError):
        repo_setup.bootstrap(run, _ORG, fmt, "noble", sudo=True)
    assert run.flat[-2].startswith(f"sudo rm -f {src}vergil-bootstrap.")
    assert run.flat[-1] == f"rm -rf {_TMP}"


@pytest.mark.parametrize(("fmt", "fail_on"), [("deb", "install -y curl"), ("rpm", "dnf install")])
def test_prerequisite_install_failure_propagates_before_anything_else(
    fmt: str, fail_on: str
) -> None:
    run = FakeRun(fail_on=fail_on, missing="curl\n")
    with pytest.raises(subprocess.CalledProcessError):
        repo_setup.bootstrap(run, _ORG, fmt, "noble", sudo=False)
    assert not any(c.startswith(("mktemp", "curl")) for c in run.flat)


def test_unusable_mktemp_output_is_fatal() -> None:
    run = FakeRun(mktemp="\n")
    with pytest.raises(PackageError, match=r"mktemp -d returned '' "):
        repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=False)
    assert run.flat[-1] == "mktemp -d"


def test_unknown_format_is_fatal_before_anything_runs() -> None:
    run = FakeRun()
    with pytest.raises(PackageError, match=r"unknown package format 'apk'"):
        repo_setup.bootstrap(run, _ORG, "apk", "noble", sudo=True)
    assert run.calls == []


# --- bootstrap: idempotency (#3138) ------------------------------------------------


def test_rpm_source_path_is_the_keyring_repo_file() -> None:
    assert repo_setup.rpm_source_path(_ORG) == "/etc/yum.repos.d/vergil.repo"


@pytest.mark.parametrize(
    ("fmt", "path"),
    [
        ("deb", "/usr/share/keyrings/vergil-archive-keyring.asc"),
        ("rpm", "/etc/pki/rpm-gpg/RPM-GPG-KEY-vergil"),
    ],
)
def test_installed_key_path_is_the_key_the_permanent_source_trusts(fmt: str, path: str) -> None:
    assert repo_setup.installed_key_path(_ORG, fmt) == path


def _stub(d: Path, name: str, body: str) -> None:
    (d / name).write_text(f"#!/bin/sh\n{body}\n")
    (d / name).chmod(0o755)


@pytest.mark.parametrize(
    ("status", "files", "expected"),
    [
        ("install ok installed", ("src", "key"), ["installed", "source", "key"]),
        ("install ok installed", ("key",), ["installed", "key"]),
        ("install ok installed", ("src",), ["installed", "source"]),
        ("deinstall ok config-files", ("src", "key"), ["source", "key"]),
        ("", (), []),
    ],
)
def test_deb_keyring_probe_script(
    tmp_path: Path, status: str, files: tuple[str, ...], expected: list[str]
) -> None:
    d = tmp_path / "bin"
    d.mkdir()
    _stub(d, "dpkg-query", f"[ \"$3\" = 'my pkg' ] && printf '{status}'")
    (d / "grep").write_text(_GREP)
    (d / "grep").chmod(0o755)
    for f in files:
        (tmp_path / f).write_text("")
    probe = repo_setup.keyring_probe("deb", "my pkg", str(tmp_path / "src"), str(tmp_path / "key"))
    assert _probe(probe, tmp_path, f"{d}:/usr/bin:/bin").split() == expected


@pytest.mark.parametrize(("rc", "expected"), [(0, ["installed", "source"]), (1, ["source"])])
def test_rpm_keyring_probe_script(tmp_path: Path, rc: int, expected: list[str]) -> None:
    d = tmp_path / "bin"
    d.mkdir()
    _stub(d, "rpm", f'[ "$1 $2" = "-q vergil-archive-keyring" ] || exit 9; exit {rc}')
    (tmp_path / "src").write_text("")
    probe = repo_setup.keyring_probe(
        "rpm", "vergil-archive-keyring", str(tmp_path / "src"), str(tmp_path / "key")
    )
    assert _probe(probe, tmp_path, f"{d}:/usr/bin:/bin").split() == expected


def test_apt_rerun_on_a_migrated_host_skips_the_bootstrap() -> None:
    # The #3138 failure: a second run wrote vergil-bootstrap.sources next to the
    # keyring's vergil.sources and apt rejected the pair (conflicting Signed-By).
    run = FakeRun(state=(_FULL,))
    repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    assert run.flat == [
        f"sudo {_DEB_STALE}",
        f"bash -c {repo_setup.DEB_PREREQ_PROBE}",
        f"bash -c {_DEB_PROBE}",
        "gpg --show-keys --with-colons /usr/share/keyrings/vergil-archive-keyring.asc",
        f"sudo apt-get {_FAST} -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/vergil.sources"
        " -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0 update",
    ]


def test_dnf_rerun_on_a_migrated_host_skips_the_bootstrap() -> None:
    run = FakeRun(state=(_FULL,))
    repo_setup.bootstrap(run, _ORG, "rpm", "el9", sudo=False)
    assert run.flat == [
        _RPM_STALE,
        f"bash -c {repo_setup.RPM_PREREQ_PROBE}",
        f"bash -c {_RPM_PROBE}",
        "gpg --show-keys --with-colons /etc/pki/rpm-gpg/RPM-GPG-KEY-vergil",
        "dnf makecache --refresh --repo vergil",
    ]


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_stale_bootstrap_files_are_removed_before_any_package_manager_call(fmt: str) -> None:
    # Missing prerequisites force a full index refresh, which reads every source:
    # a stale bootstrap source must already be gone by then.
    run = FakeRun(missing="curl\n")
    repo_setup.bootstrap(run, _ORG, fmt, "noble", sudo=True)
    stale = _DEB_STALE if fmt == "deb" else _RPM_STALE
    assert run.flat[0] == f"sudo {stale}"
    first_pm = next(i for i, c in enumerate(run.flat) if "apt-get" in c or "dnf" in c)
    assert first_pm == 2


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_installed_key_fingerprint_mismatch_is_fatal(fmt: str) -> None:
    run = FakeRun(state=(_FULL,), installed_colons=_COLONS.replace(_PRIMARY, "F" * 40))
    key = repo_setup.installed_key_path(_ORG, fmt)
    with pytest.raises(
        PackageError,
        match=rf"installed vergil key {key} has primary fingerprint F{{40}}, pinned {_PRIMARY}",
    ):
        repo_setup.bootstrap(run, _ORG, fmt, "noble", sudo=True)
    assert run.flat[-1] == f"gpg --show-keys --with-colons {key}"


def test_installed_key_with_an_extra_primary_is_fatal() -> None:
    run = FakeRun(state=(_FULL,), installed_colons=_COLONS + _COLONS.replace(_PRIMARY, "F" * 40))
    with pytest.raises(PackageError, match=r"exactly one primary key"):
        repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    assert not any("update" in c for c in run.flat)


@pytest.mark.parametrize("half", ["installed\nkey\n", "installed\nsource\n"])
def test_apt_half_state_reinstalls_the_keyring_through_the_bootstrap(half: str) -> None:
    # Keyring installed but its source (or key) gone: a plain install is a no-op
    # that never re-runs the postinst, so the keyring is reinstalled.
    run = FakeRun(state=(half, _FULL))
    repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    reinstall = f"sudo apt-get {_FAST} {_SCOPE} install -y --reinstall vergil-archive-keyring"
    assert reinstall in run.flat
    assert run.flat[-3:] == [f"sudo {_DEB_STALE}", f"bash -c {_DEB_PROBE}", f"rm -rf {_TMP}"]


def test_dnf_half_state_reinstalls_the_keyring_through_the_bootstrap() -> None:
    run = FakeRun(state=("installed\nkey\n", _FULL))
    repo_setup.bootstrap(run, _ORG, "rpm", "el9", sudo=False)
    assert "dnf reinstall -y --repo vergil-bootstrap vergil-archive-keyring" in run.flat
    assert run.flat[-3:] == [_RPM_STALE, f"bash -c {_RPM_PROBE}", f"rm -rf {_TMP}"]


def test_a_stray_permanent_source_without_the_keyring_installs_normally() -> None:
    # The install reads the bootstrap source alone, so a leftover vergil.sources
    # cannot conflict with it; the keyring's postinst then rewrites it.
    run = FakeRun(state=("source\n", _FULL))
    repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=False)
    assert f"apt-get {_FAST} {_SCOPE} install -y vergil-archive-keyring" in run.flat


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_keyring_install_that_leaves_its_setup_incomplete_is_fatal(fmt: str) -> None:
    run = FakeRun(state=("installed\nkey\n", "installed\nkey\n"))
    with pytest.raises(PackageError, match=r"did not leave its setup in place \(missing: source;"):
        repo_setup.bootstrap(run, _ORG, fmt, "noble", sudo=False)
    assert run.flat[-1] == f"rm -rf {_TMP}"
