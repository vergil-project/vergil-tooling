"""Tests for vergil_tooling.lib.package.index.site (spec §7.2, §7.3)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.package import PackageError, repo_setup
from vergil_tooling.lib.package.index import apt, collect, retention, rpm, sign, site
from vergil_tooling.lib.package.index.collect import Artifact

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.package.index.config import IndexConfig

_FPR = "0123456789ABCDEF0123456789ABCDEF01234567"
_COLONS = (
    "pub:-:255:22:89ABCDEF01234567:1700000000:::-:::scSC::::::23::0:\n"
    f"fpr:::::::::{_FPR}:\n"
    "uid:-::::1700000000::H::Vergil <k@example.invalid>::::::::::0:\n"
    "sub:-:255:22:FEDCBA9876543210:1700000000::::::s::::::23:\n"
    "fpr:::::::::FFFFFFFFFFFFFFFFFFFFFFFFFEDCBA9876543210:\n"
)


# --- size guard ------------------------------------------------------------------


def test_size_guard(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "a").write_bytes(b"x" * 100)
    assert site.size_guard(tmp_path, warn=50, fail=200) == 100
    assert "WARNING: package site is 100 bytes" in capsys.readouterr().err
    with pytest.raises(PackageError, match=r"package site is 100 bytes, over the 90-byte limit"):
        site.size_guard(tmp_path, warn=50, fail=90)


def test_size_guard_counts_nested_files_and_is_quiet_below_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "e" / "f").write_bytes(b"x" * 30)
    (tmp_path / "g").write_bytes(b"x" * 20)
    assert site.size_guard(tmp_path, warn=50, fail=90) == 50
    out = capsys.readouterr()
    assert out.err == ""
    assert "package site is 50 bytes" in out.out


def test_size_guard_defaults() -> None:
    assert (site.SIZE_WARN, site.SIZE_FAIL) == (750_000_000, 900_000_000)


# --- helpers ---------------------------------------------------------------------


def test_suites_come_from_the_registry() -> None:
    assert site.suites("deb") == ["noble", "resolute"]
    assert site.suites("rpm") == ["el9", "el10"]


def test_default_base_url_from_github_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "Vergil-Project/packages")
    assert site.default_base_url() == "https://vergil-project.github.io/packages"


@pytest.mark.parametrize("value", [None, "", "noslash", "/repo", "owner/"])
def test_default_base_url_needs_github_repository(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", value)
    with pytest.raises(PackageError, match=r"pass --base-url or set GITHUB_REPOSITORY"):
        site.default_base_url()


def _gpg_run(stdout: str) -> repo_setup.Run:
    def run(*a: str) -> subprocess.CompletedProcess[str]:
        assert a[:4] == ("gpg", "--batch", "--with-colons", "--show-keys")
        return subprocess.CompletedProcess(a, 0, stdout, "")

    return run


def test_fingerprint_is_the_primary_keys(tmp_path: Path) -> None:
    assert site.fingerprint(tmp_path / "k.asc", _gpg_run(_COLONS)) == _FPR


def test_fingerprint_ignores_an_fpr_before_any_pub(tmp_path: Path) -> None:
    out = "fpr:::::::::AAAA:\n" + _COLONS
    assert site.fingerprint(tmp_path / "k.asc", _gpg_run(out)) == _FPR


def test_fingerprint_without_a_public_key_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match=r"k\.asc holds no public key"):
        site.fingerprint(tmp_path / "k.asc", _gpg_run("sec:x\n"))


def test_fingerprint_gpg_failure_is_fatal(tmp_path: Path) -> None:
    def run(*a: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(2, a, "", "no valid OpenPGP data found")

    with pytest.raises(PackageError, match=r"reading the key in .*: no valid OpenPGP data"):
        site.fingerprint(tmp_path / "k.asc", run)


def test_wipe_empties_and_recreates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "_site"
    (out / "old").mkdir(parents=True)
    (out / "old" / "f").write_text("x")
    site._wipe(out, tmp_path / "packages.toml")
    assert out.is_dir() and list(out.iterdir()) == []
    fresh = tmp_path / "new" / "_site"
    site._wipe(fresh)
    assert fresh.is_dir()


@pytest.mark.parametrize("inside", ["cwd", "input"])
def test_wipe_refuses_a_directory_holding_the_cwd_or_an_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: str
) -> None:
    repo = tmp_path / "repo"
    (repo / "keys").mkdir(parents=True)
    monkeypatch.chdir(repo if inside == "cwd" else tmp_path / "repo" / "keys")
    with pytest.raises(PackageError, match=r"refusing to wipe .*: it contains"):
        site._wipe(repo if inside == "cwd" else tmp_path / "repo", repo / "keys")
    assert (repo / "keys").is_dir()


def test_wipe_refuses_when_out_holds_an_input_but_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out" / "keys").mkdir(parents=True)
    with pytest.raises(PackageError, match=r"refusing to wipe .*out: it contains .*keys"):
        site._wipe(tmp_path / "out", tmp_path / "out" / "keys")


def test_index_html_has_fingerprint_and_setup_and_escapes() -> None:
    page = site.index_html("v<x>", "https://v.github.io/p", _FPR, ["noble", "resolute"])
    assert page.startswith("<!doctype html>")
    assert f"<code>{_FPR}</code>" in page
    assert "v&lt;x&gt;" in page and "v<x>" not in page
    assert "https://v.github.io/p/deb $(. /etc/os-release; echo $VERSION_CODENAME) main" in page
    assert "baseurl=https://v.github.io/p/rpm/el$releasever/$basearch" in page
    assert "Ubuntu (noble, resolute)" in page


# --- build_site --------------------------------------------------------------------


def _art(tmp_path: Path, fmt: str) -> Artifact:
    p = tmp_path / f"t.{fmt}"
    return Artifact("o/t", "v1.0.0", p, fmt, "t", "1.0.0", "1", "amd64", (), "s")


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "packages.toml"
    config.write_text('vendor = "v"\nproducts = ["o/t"]\n[retention]\nkeep = 2\nlines = 1\n')
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "v.asc").write_text("PUBLIC KEY")
    return config, keys


class _Patched:
    """Monkeypatches every collaborator of ``build_site``; records the call order."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.arts = [_art(tmp_path, "deb"), _art(tmp_path, "rpm")]
        ev = self.events

        def fake_fpr(keyfile: Path, run: repo_setup.Run) -> str:
            ev.append(("fingerprint", keyfile.name))
            return _FPR

        def fake_collect(cfg: IndexConfig, workdir: Path, gh: repo_setup.Run) -> list[Artifact]:
            ev.append(("collect", cfg.vendor, cfg.products, workdir.name))
            return self.arts

        def fake_verify(
            arts: list[Artifact], keyfile: Path, gh: repo_setup.Run, *, rpmdb: Path
        ) -> None:
            ev.append(("verify", len(arts), keyfile.name, rpmdb.name, rpmdb.exists()))

        def fake_select(arts: list[Artifact], keep: int, lines: int) -> list[Artifact]:
            ev.append(("select", keep, lines))
            return arts[:1]

        def fake_apt(
            out: Path, arts: list[Artifact], suites: list[str], control: Any, *, vendor: str
        ) -> list[Path]:
            ev.append(("apt", [a.fmt for a in arts], suites, vendor, sorted(_rel_paths(out))))
            rel = out / "deb/dists/noble/Release"
            rel.parent.mkdir(parents=True)
            rel.write_text("R")
            return [rel]

        def fake_rpm(
            out: Path, arts: list[Artifact], base: str, els: list[str], run: repo_setup.Run
        ) -> list[Path]:
            ev.append(("rpm", base, els))
            repomd = out / "rpm/el9/x86_64/repodata/repomd.xml"
            repomd.parent.mkdir(parents=True)
            repomd.write_text("M")
            return [repomd]

        def fake_import(run: repo_setup.Run, key_env: str, pass_env: str) -> None:
            ev.append(("import_key", key_env, pass_env))

        def fake_clearsign(run: repo_setup.Run, src: Path, dst: Path, *, local_user: str) -> None:
            ev.append(("clearsign", src.name, dst.name, local_user))

        def fake_detach(run: repo_setup.Run, src: Path, dst: Path, *, local_user: str) -> None:
            ev.append(("detach", src.name, dst.name, local_user))

        def fake_guard(out: Path) -> int:
            ev.append(("size_guard", sorted(_rel_paths(out))))
            return 0

        monkeypatch.setattr(site, "fingerprint", fake_fpr)
        monkeypatch.setattr(collect, "collect", fake_collect)
        monkeypatch.setattr(collect, "verify", fake_verify)
        monkeypatch.setattr(retention, "select", fake_select)
        monkeypatch.setattr(apt, "write", fake_apt)
        monkeypatch.setattr(rpm, "write", fake_rpm)
        monkeypatch.setattr(sign, "import_key", fake_import)
        monkeypatch.setattr(sign, "clearsign", fake_clearsign)
        monkeypatch.setattr(sign, "detach", fake_detach)
        monkeypatch.setattr(site, "size_guard", fake_guard)


def _rel_paths(out: Path) -> list[str]:
    return [p.relative_to(out).as_posix() for p in out.rglob("*")]


def test_build_site_runs_every_step_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config, keys = _inputs(tmp_path)
    out, work = tmp_path / "_site", tmp_path / "work"
    (out / "stale").mkdir(parents=True)
    (work / "rpmdb").mkdir(parents=True)
    p = _Patched(tmp_path, monkeypatch)
    site.build_site(config, keys, out, work, base_url="https://v.github.io/p/")
    assert p.events == [
        ("fingerprint", "v.asc"),
        ("collect", "v", ["o/t"], "work"),
        ("verify", 2, "v.asc", "rpmdb", False),
        ("select", 2, 1),
        ("apt", ["deb"], ["noble", "resolute"], "v", []),
        ("rpm", "https://v.github.io/p/", ["el9", "el10"]),
        ("import_key", "PACKAGE_SIGNING_KEY", "PACKAGE_SIGNING_PASSPHRASE"),
        ("clearsign", "Release", "InRelease", _FPR),
        ("detach", "Release", "Release.gpg", _FPR),
        ("detach", "repomd.xml", "repomd.xml.asc", _FPR),
        (
            "size_guard",
            [
                "deb",
                "deb/dists",
                "deb/dists/noble",
                "deb/dists/noble/Release",
                "index.html",
                "keys",
                "keys/v.asc",
                "rpm",
                "rpm/el9",
                "rpm/el9/x86_64",
                "rpm/el9/x86_64/repodata",
                "rpm/el9/x86_64/repodata/repomd.xml",
            ],
        ),
    ]
    assert (out / "keys/v.asc").read_text() == "PUBLIC KEY"
    page = (out / "index.html").read_text()
    assert _FPR in page and "https://v.github.io/p/keys/v.asc" in page
    assert "https://v.github.io/p//" not in page


def test_build_site_control_reads_through_the_given_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config, keys = _inputs(tmp_path)
    _Patched(tmp_path, monkeypatch)
    seen: list[Any] = []

    def fake_apt(
        out: Path, arts: list[Artifact], suites: list[str], control: Any, *, vendor: str
    ) -> list[Path]:
        seen.append(control(tmp_path / "t.deb"))
        return []

    monkeypatch.setattr(apt, "write", fake_apt)

    def run(*a: str) -> subprocess.CompletedProcess[str]:
        assert a == ("dpkg-deb", "-f", str(tmp_path / "t.deb"))
        return subprocess.CompletedProcess(a, 0, "Package: t\n", "")

    site.build_site(config, keys, tmp_path / "_site", tmp_path / "w", run, base_url="u")
    assert seen == ["Package: t\n"]


def test_build_site_missing_public_key_is_fatal_before_any_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, keys = _inputs(tmp_path)
    (keys / "v.asc").unlink()
    p = _Patched(tmp_path, monkeypatch)
    with pytest.raises(PackageError, match=r"the v public key is missing: .*keys/v\.asc"):
        site.build_site(config, keys, tmp_path / "_site", tmp_path / "w", base_url="u")
    assert p.events == []


def test_build_site_size_guard_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config, keys = _inputs(tmp_path)
    _Patched(tmp_path, monkeypatch)

    def too_big(out: Path) -> int:
        msg = "package site is 950000000 bytes, over the 900000000-byte limit"
        raise PackageError(msg)

    monkeypatch.setattr(site, "size_guard", too_big)
    with pytest.raises(PackageError, match=r"over the 900000000-byte limit"):
        site.build_site(config, keys, tmp_path / "_site", tmp_path / "w", base_url="u")
