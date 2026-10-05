"""Tests for vergil_tooling.lib.package.index.rpm (spec §7.2, §7.3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import rpm
from vergil_tooling.lib.package.index.collect import Artifact

_BASE = "https://v.github.io/packages"
_RPM_ARCH = {"amd64": "x86_64", "arm64": "aarch64", "all": "noarch"}


def _rpm(
    tmp_path: Path, name: str = "t", release: str = "1", arch: str = "amd64", fmt: str = "rpm"
) -> Artifact:
    path = tmp_path / "in" / f"{name}-2.1.0-{release}.{_RPM_ARCH[arch]}.{fmt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(name.encode())
    return Artifact("o/t", "v2.1.0", path, fmt, name, "2.1.0", release, arch, (), "s")


class _FakeCreaterepo:
    """Records each call and the files (symlink targets) in its input directory."""

    def __init__(self, *, write_repomd: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.listed: dict[str, list[str]] = {}
        self.write_repomd = write_repomd

    def __call__(self, *argv: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        indir = Path(argv[-1])
        outdir = Path(argv[argv.index("--outputdir") + 1])
        key = indir.name.split("-", 3)[1] + "/" + indir.name.split("-", 3)[2]
        self.listed[key] = sorted(p.name for p in indir.iterdir() if p.is_symlink())
        for p in indir.iterdir():
            assert p.resolve().parent.name == "pool"
        (outdir / "repodata").mkdir(parents=True, exist_ok=True)
        if self.write_repomd:
            (outdir / "repodata" / "repomd.xml").write_text(f"<repomd {key}/>")
            (outdir / "repodata" / "primary.xml.gz").write_bytes(b"p")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_rpm_repodata_per_el_and_arch_with_pool_baseurl(tmp_path: Path) -> None:
    art = _rpm(tmp_path)
    run = _FakeCreaterepo()
    site = tmp_path / "site"
    out = rpm.write(site, [art], _BASE, ["el9", "el10"], run)
    assert (site / "rpm/pool/t-2.1.0-1.x86_64.rpm").read_bytes() == b"t"
    assert [p.relative_to(site).as_posix() for p in out] == [
        "rpm/el9/x86_64/repodata/repomd.xml",
        "rpm/el9/aarch64/repodata/repomd.xml",
        "rpm/el10/x86_64/repodata/repomd.xml",
        "rpm/el10/aarch64/repodata/repomd.xml",
    ]
    assert (site / "rpm/el10/aarch64/repodata/repomd.xml").read_text() == "<repomd el10/aarch64/>"
    assert (site / "rpm/el9/x86_64/repodata/primary.xml.gz").read_bytes() == b"p"
    for c in run.calls:
        assert c[0] == "createrepo_c"
        assert c[c.index("--baseurl") + 1] == "https://v.github.io/packages/rpm/pool/"
        assert c[c.index("--general-compress-type") + 1] == "gz"
        assert c[c.index("--outputdir") + 1] == c[-1]
    assert run.listed == {
        "el9/x86_64": ["t-2.1.0-1.x86_64.rpm"],
        "el9/aarch64": [],
        "el10/x86_64": ["t-2.1.0-1.x86_64.rpm"],
        "el10/aarch64": [],
    }


def test_base_url_trailing_slash_is_normalised(tmp_path: Path) -> None:
    run = _FakeCreaterepo()
    rpm.write(tmp_path / "site", [], _BASE + "/", ["el9"], run)
    assert all(c[c.index("--baseurl") + 1] == f"{_BASE}/rpm/pool/" for c in run.calls)


def test_noarch_listed_in_every_arch(tmp_path: Path) -> None:
    run = _FakeCreaterepo()
    arts = [_rpm(tmp_path, "k", arch="all"), _rpm(tmp_path, "t", arch="arm64")]
    rpm.write(tmp_path / "site", arts, _BASE, ["el9"], run)
    assert run.listed == {
        "el9/x86_64": ["k-2.1.0-1.noarch.rpm"],
        "el9/aarch64": ["k-2.1.0-1.noarch.rpm", "t-2.1.0-1.aarch64.rpm"],
    }


def test_native_rpm_only_in_its_el(tmp_path: Path) -> None:
    run = _FakeCreaterepo()
    arts = [_rpm(tmp_path, "n", release="1.el10"), _rpm(tmp_path, "s")]
    rpm.write(tmp_path / "site", arts, _BASE, ["el9", "el10"], run)
    assert run.listed["el9/x86_64"] == ["s-2.1.0-1.x86_64.rpm"]
    assert run.listed["el10/x86_64"] == ["n-2.1.0-1.el10.x86_64.rpm", "s-2.1.0-1.x86_64.rpm"]


def test_native_rpm_for_an_unindexed_el_is_fatal(tmp_path: Path) -> None:
    art = _rpm(tmp_path, release="1.el8")
    with pytest.raises(
        PackageError,
        match=r"t-2\.1\.0-1\.el8\.x86_64\.rpm \(o/t@v2\.1\.0\) is a native build for 'el8', "
        r"which is not an indexed EL release \(el9, el10\)",
    ):
        rpm.write(tmp_path / "site", [art], _BASE, ["el9", "el10"], _FakeCreaterepo())


def test_debs_are_ignored_and_empty_repos_still_get_repodata(tmp_path: Path) -> None:
    run = _FakeCreaterepo()
    site = tmp_path / "site"
    out = rpm.write(site, [_rpm(tmp_path, fmt="deb")], _BASE, ["el9"], run)
    assert len(out) == 2 and all(p.is_file() for p in out)
    assert run.listed == {"el9/x86_64": [], "el9/aarch64": []}
    assert list((site / "rpm/pool").iterdir()) == []


def test_createrepo_failure_is_fatal(tmp_path: Path) -> None:
    def run(*a: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, a, "", "cannot open rpm")

    with pytest.raises(PackageError, match=r"createrepo_c for el9/x86_64 failed: cannot open rpm"):
        rpm.write(tmp_path / "site", [], _BASE, ["el9"], run)


def test_missing_repomd_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(
        PackageError, match=r"createrepo_c for el9/x86_64 produced no .*/repodata/repomd\.xml"
    ):
        rpm.write(tmp_path / "site", [], _BASE, ["el9"], _FakeCreaterepo(write_repomd=False))
