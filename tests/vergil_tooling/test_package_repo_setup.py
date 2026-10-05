"""Tests for vergil_tooling.lib.package.repo_setup (spec §7.4, §9)."""

from __future__ import annotations

import shlex
import subprocess
import sys

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


class FakeRun:
    """Records every ``run(*argv)`` call; answers ``mktemp`` and ``gpg --with-colons``."""

    def __init__(
        self, colons: str = _COLONS, *, mktemp: str = f"{_TMP}\n", fail_on: str | None = None
    ) -> None:
        self.colons = colons
        self.mktemp = mktemp
        self.fail_on = fail_on
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if self.fail_on is not None and self.fail_on in " ".join(argv):
            raise subprocess.CalledProcessError(100, argv, "", "E: boom")
        out = ""
        if "--with-colons" in argv:
            out = self.colons
        elif argv[:1] == ("mktemp",):
            out = self.mktemp
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


# --- bootstrap: apt ------------------------------------------------------------


def test_apt_bootstrap_sequence() -> None:
    run = FakeRun()
    repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    cmds = run.flat
    assert cmds[:3] == [
        "sudo apt-get update",
        "sudo apt-get install -y ca-certificates curl gnupg",
        "mktemp -d",
    ]
    key_tmp = f"{_TMP}/vergil.asc"
    assert run.calls[3] == (
        "curl",
        "-fsSL",
        "https://vergil-project.github.io/packages/keys/vergil.asc",
        "-o",
        key_tmp,
    )
    assert run.calls[4] == ("gpg", "--show-keys", "--with-colons", key_tmp)

    write = run.calls[5]
    assert write[:3] == ("sudo", "bash", "-c")
    assert shlex.split(write[3]) == [
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
    assert cmds[6:] == [
        "sudo apt-get update",
        "sudo apt-get install -y vergil-archive-keyring",
        "sudo rm -f /etc/apt/sources.list.d/vergil-bootstrap.sources"
        " /etc/apt/keyrings/vergil-bootstrap.asc",
        f"rm -rf {_TMP}",
    ]


def test_apt_bootstrap_without_sudo_has_no_sudo_prefix() -> None:
    run = FakeRun()
    repo_setup.bootstrap(run, _ORG, "deb", "resolute", sudo=False)
    assert not any(c[0] == "sudo" for c in run.calls)
    assert run.calls[5][:2] == ("bash", "-c")
    assert "Suites: resolute\n" in run.calls[5][2]


# --- bootstrap: dnf ------------------------------------------------------------


def test_dnf_bootstrap_sequence() -> None:
    run = FakeRun()
    repo_setup.bootstrap(run, _ORG, "rpm", "el9", sudo=False)
    cmds = run.flat
    # File provides, not ``curl``: RHEL's preinstalled curl-minimal conflicts
    # with the full ``curl`` package, but satisfies ``/usr/bin/curl``.
    assert cmds[:2] == ["dnf install -y ca-certificates /usr/bin/curl /usr/bin/gpg", "mktemp -d"]
    key_tmp = f"{_TMP}/vergil.asc"
    assert run.calls[3] == ("gpg", "--show-keys", "--with-colons", key_tmp)
    write = run.calls[4]
    assert write[:2] == ("bash", "-c")
    words = shlex.split(write[2])
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
    assert cmds[5:] == [
        "dnf install -y vergil-archive-keyring",
        "rm -f /etc/yum.repos.d/vergil-bootstrap.repo"
        " /etc/pki/rpm-gpg/RPM-GPG-KEY-vergil-bootstrap",
        f"rm -rf {_TMP}",
    ]


def test_dnf_bootstrap_with_sudo() -> None:
    run = FakeRun()
    repo_setup.bootstrap(run, _ORG, "rpm", "el10", sudo=True)
    assert run.flat[0].startswith("sudo dnf install -y ca-certificates")
    assert "sudo dnf install -y vergil-archive-keyring" in run.flat


# --- bootstrap: failures ----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_fingerprint_mismatch_is_fatal_before_any_source_is_written(fmt: str) -> None:
    run = FakeRun(_COLONS.replace(_PRIMARY, "F" * 40))
    with pytest.raises(PackageError, match=r"fingerprint mismatch: published F{40}, pinned"):
        repo_setup.bootstrap(run, _ORG, fmt, "noble", sudo=True)
    assert not any("sources.list.d" in c or "yum.repos.d" in c or "keyring" in c for c in run.flat)
    assert run.flat[-1] == f"rm -rf {_TMP}"


def test_key_with_an_extra_primary_is_fatal_before_any_source_is_written() -> None:
    run = FakeRun(_COLONS + _COLONS.replace(_PRIMARY, "F" * 40))
    with pytest.raises(PackageError, match=r"exactly one primary key"):
        repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    assert not any("sources.list.d" in c for c in run.flat)


def test_key_download_failure_propagates_and_cleans_the_temp_dir() -> None:
    run = FakeRun(fail_on="curl -fsSL")
    with pytest.raises(subprocess.CalledProcessError):
        repo_setup.bootstrap(run, _ORG, "deb", "noble", sudo=True)
    assert not any("sources.list.d" in c for c in run.flat)
    assert run.flat[-1] == f"rm -rf {_TMP}"


@pytest.mark.parametrize(
    ("fmt", "fail_on", "src"),
    [
        ("deb", "install -y vergil-archive-keyring", "/etc/apt/sources.list.d/"),
        ("deb", "bash -c", "/etc/apt/sources.list.d/"),
        ("rpm", "install -y vergil-archive-keyring", "/etc/yum.repos.d/"),
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
