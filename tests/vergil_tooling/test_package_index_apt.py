"""Tests for vergil_tooling.lib.package.index.apt (spec §7.2, §7.3)."""

from __future__ import annotations

import gzip
import hashlib
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import apt
from vergil_tooling.lib.package.index.collect import Artifact

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


def _deb(
    tmp_path: Path,
    name: str = "t",
    version: str = "2.1.0",
    release: str = "1",
    arch: str = "amd64",
    data: bytes = b"abc",
) -> Artifact:
    path = tmp_path / "in" / f"{name}_{version}-{release}_{arch}.deb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    return Artifact("o/t", f"v{version}", path, "deb", name, version, release, arch, (), sha)


def _control(p: Path) -> str:
    name, ver, arch = p.name.removesuffix(".deb").split("_")
    return f"Package: {name}\nVersion: {ver}\nArchitecture: {arch}\n"


def _packages(site: Path, suite: str, arch: str) -> str:
    return (site / f"deb/dists/{suite}/main/binary-{arch}/Packages").read_text()


def _names(site: Path, suite: str, arch: str) -> list[str]:
    return [
        line.removeprefix("Filename: pool/").split("/")[0] + ":" + line.rsplit("_", 2)[1]
        for line in _packages(site, suite, arch).splitlines()
        if line.startswith("Filename: ")
    ]


def test_packages_stanza_has_hashes_and_filename(tmp_path: Path) -> None:
    art = _deb(tmp_path)
    site = tmp_path / "site"
    releases = apt.write(site, [art], ["noble", "resolute"], _control, vendor="v", now=_NOW)
    pk = _packages(site, "noble", "amd64")
    assert pk == (
        "Package: t\nVersion: 2.1.0-1\nArchitecture: amd64\n"
        "Filename: pool/t/t_2.1.0-1_amd64.deb\n"
        "Size: 3\n"
        f"MD5sum: {hashlib.md5(b'abc', usedforsecurity=False).hexdigest()}\n"
        f"SHA1: {hashlib.sha1(b'abc', usedforsecurity=False).hexdigest()}\n"
        f"SHA256: {hashlib.sha256(b'abc').hexdigest()}\n"
    )
    assert (site / "deb/pool/t/t_2.1.0-1_amd64.deb").read_bytes() == b"abc"
    assert _packages(site, "resolute", "amd64") == pk
    assert _packages(site, "noble", "arm64") == ""
    assert releases == [site / "deb/dists/noble/Release", site / "deb/dists/resolute/Release"]
    rel = releases[0].read_text()
    assert "Suite: noble\n" in rel and "Codename: noble\n" in rel
    assert "Origin: v\nLabel: v\n" in rel
    assert "Architectures: amd64 arm64\n" in rel and "Components: main\n" in rel
    assert "Date: Mon, 05 Oct 2026 12:00:00 GMT\n" in rel
    assert "main/binary-amd64/Packages.gz" in rel


def test_release_hashes_every_packages_file(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (release,) = apt.write(site, [_deb(tmp_path)], ["noble"], _control, vendor="v", now=_NOW)
    text = release.read_text()
    md5_part, sha_part = text.split("MD5Sum:\n")[1].split("SHA256:\n")
    dist = site / "deb/dists/noble"
    rels = [
        "main/binary-amd64/Packages",
        "main/binary-amd64/Packages.gz",
        "main/binary-arm64/Packages",
        "main/binary-arm64/Packages.gz",
    ]
    for algo, part in (("md5", md5_part), ("sha256", sha_part)):
        lines = part.splitlines()
        assert [ln.split()[2] for ln in lines] == rels
        for ln in lines:
            digest, size, rel = ln.split()
            data = (dist / rel).read_bytes()
            assert digest == hashlib.new(algo, data).hexdigest()
            assert int(size) == len(data)


def test_packages_gz_is_deterministic_and_matches(tmp_path: Path) -> None:
    art = _deb(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    apt.write(one, [art], ["noble"], _control, vendor="v", now=_NOW)
    apt.write(two, [art], ["noble"], _control, vendor="v", now=_NOW)
    gz = "deb/dists/noble/main/binary-amd64/Packages.gz"
    assert (one / gz).read_bytes() == (two / gz).read_bytes()
    assert gzip.decompress((one / gz).read_bytes()).decode() == _packages(one, "noble", "amd64")
    assert (one / "deb/dists/noble/Release").read_bytes() == (
        two / "deb/dists/noble/Release"
    ).read_bytes()


def test_native_suffixed_deb_only_in_its_suite(tmp_path: Path) -> None:
    native = _deb(tmp_path, "n", release="1~resolute")
    shared = _deb(tmp_path, "s")
    site = tmp_path / "site"
    apt.write(site, [native, shared], ["noble", "resolute"], _control, vendor="v", now=_NOW)
    assert _names(site, "noble", "amd64") == ["s:2.1.0-1"]
    assert _names(site, "resolute", "amd64") == ["n:2.1.0-1~resolute", "s:2.1.0-1"]
    assert (site / "deb/pool/n/n_2.1.0-1~resolute_amd64.deb").is_file()


def test_native_deb_for_an_unindexed_suite_is_fatal(tmp_path: Path) -> None:
    art = _deb(tmp_path, release="1~jammy")
    with pytest.raises(
        PackageError,
        match=r"t_2\.1\.0-1~jammy_amd64\.deb \(o/t@v2\.1\.0\) is a native build for suite "
        r"'jammy', which is not an indexed suite \(noble, resolute\)",
    ):
        apt.write(tmp_path / "site", [art], ["noble", "resolute"], _control, vendor="v")


def test_all_arch_listed_in_every_binary_dir(tmp_path: Path) -> None:
    site = tmp_path / "site"
    arts = [_deb(tmp_path, "k", arch="all"), _deb(tmp_path, "t", arch="arm64")]
    apt.write(site, arts, ["noble"], _control, vendor="v", now=_NOW)
    assert _names(site, "noble", "amd64") == ["k:2.1.0-1"]
    assert _names(site, "noble", "arm64") == ["k:2.1.0-1", "t:2.1.0-1"]


def test_stanzas_sorted_by_name_then_numeric_version_and_blank_separated(
    tmp_path: Path,
) -> None:
    arts = [
        _deb(tmp_path, "b", "1.10.0"),
        _deb(tmp_path, "b", "1.9.0"),
        _deb(tmp_path, "a", "3.0.0"),
    ]
    site = tmp_path / "site"
    apt.write(site, arts, ["noble"], _control, vendor="v", now=_NOW)
    assert _names(site, "noble", "amd64") == ["a:3.0.0-1", "b:1.9.0-1", "b:1.10.0-1"]
    stanzas = _packages(site, "noble", "amd64").split("\n\n")
    assert len(stanzas) == 3
    assert all(s.startswith("Package: ") for s in stanzas)


def test_rpms_are_ignored(tmp_path: Path) -> None:
    rpm = tmp_path / "t-2.1.0-1.x86_64.rpm"
    rpm.write_bytes(b"r")
    art = Artifact("o/t", "v2.1.0", rpm, "rpm", "t", "2.1.0", "1", "amd64", (), "s")
    site = tmp_path / "site"
    apt.write(site, [art], ["noble"], _control, vendor="v", now=_NOW)
    assert _packages(site, "noble", "amd64") == ""
    assert not (site / "deb/pool").exists()


def test_control_text_is_normalised(tmp_path: Path) -> None:
    site = tmp_path / "site"
    apt.write(
        site,
        [_deb(tmp_path)],
        ["noble"],
        lambda _p: "\nPackage: t\n\nDescription: x\n more\n\n",
        vendor="v",
        now=_NOW,
    )
    assert _packages(site, "noble", "amd64").startswith(
        "Package: t\nDescription: x\n more\nFilename: "
    )


def test_date_defaults_to_now(tmp_path: Path) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    (release,) = apt.write(tmp_path / "site", [], ["noble"], _control, vendor="v")
    after = datetime.now(UTC)
    date = next(ln for ln in release.read_text().splitlines() if ln.startswith("Date: "))
    stamp = datetime.strptime(date, "Date: %a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
    assert before <= stamp <= after


def test_deb_control_runs_dpkg_deb_without_a_field_list(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def run(*a: str) -> subprocess.CompletedProcess[str]:
        calls.append(a)
        return subprocess.CompletedProcess(a, 0, "Package: t\n", "")

    assert apt.deb_control(tmp_path / "t.deb", run) == "Package: t\n"
    assert calls == [("dpkg-deb", "-f", str(tmp_path / "t.deb"))]


def test_deb_control_failure_is_fatal(tmp_path: Path) -> None:
    def run(*a: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(2, a, "", "not a debian archive")

    with pytest.raises(
        PackageError, match=r"reading the control data of t\.deb failed: not a debian archive"
    ):
        apt.deb_control(tmp_path / "t.deb", run)
