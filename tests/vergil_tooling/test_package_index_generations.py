"""Tests for vergil_tooling.lib.package.index.generations (#4230)."""

from __future__ import annotations

import hashlib
import json
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index import apt, generations
from vergil_tooling.lib.package.index.collect import Artifact
from vergil_tooling.lib.package.index.generations import Generation

if TYPE_CHECKING:
    from pathlib import Path

_BASE = "https://v.github.io/p"
_T1 = datetime(2026, 11, 11, 19, 0, 0, tzinfo=UTC)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _deb_path(data: bytes, suite: str = "noble", arch: str = "amd64") -> str:
    return f"deb/dists/{suite}/main/binary-{arch}/by-hash/SHA256/{_sha(data)}"


def _rpm_path(data: bytes, kind: str = "primary") -> str:
    return f"rpm/el9/x86_64/repodata/{_sha(data)}-{kind}.xml.gz"


def _put(site: Path, rel: str, data: bytes) -> None:
    (site / rel).parent.mkdir(parents=True, exist_ok=True)
    (site / rel).write_bytes(data)


class _Live:
    """A fake published site: serves files under ``root`` at ``_BASE``; records each URL."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes | None:
        self.urls.append(url)
        path, sep, query = url.partition("?")
        assert sep and query.startswith("generation="), url
        assert path.startswith(_BASE + "/"), url
        f = self.root / path.removeprefix(_BASE + "/")
        return f.read_bytes() if f.is_file() else None


def _manifest(site: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((site / generations.MANIFEST).read_text())
    return data


# --- current -----------------------------------------------------------------------


def test_current_lists_by_hash_and_repodata_files_but_not_repomd(tmp_path: Path) -> None:
    a, b, c = b"packages", b"primary", b"other"
    for rel, data in ((_deb_path(a), a), (_rpm_path(b), b), (_rpm_path(c, "other"), c)):
        _put(tmp_path, rel, data)
    _put(tmp_path, "deb/dists/noble/main/binary-amd64/Packages", a)
    _put(tmp_path, "deb/dists/noble/InRelease", b"signed")
    _put(tmp_path, "rpm/el9/x86_64/repodata/repomd.xml", b"<repomd/>")
    _put(tmp_path, "rpm/el9/x86_64/repodata/repomd.xml.asc", b"sig")
    gen = generations.current(tmp_path, _T1)
    assert gen == Generation(
        "2026-11-11T19:00:00+00:00",
        tuple(sorted([_deb_path(a), _rpm_path(b), _rpm_path(c, "other")])),
    )


def test_current_of_a_real_apt_tree_is_every_by_hash_file(tmp_path: Path) -> None:
    deb = tmp_path / "in/t_1.0.0-1_amd64.deb"
    deb.parent.mkdir()
    deb.write_bytes(b"abc")
    art = Artifact("o/t", "v1.0.0", deb, "deb", "t", "1.0.0", "1", "amd64", (), _sha(b"abc"))
    site = tmp_path / "site"
    apt.write(site, [art], ["noble"], lambda p: "Package: t\n", vendor="v", now=_T1)
    files = generations.current(site, _T1).files
    expected = []
    for arch in ("amd64", "arm64"):
        for name in ("Packages", "Packages.gz"):
            data = (site / f"deb/dists/noble/main/binary-{arch}/{name}").read_bytes()
            expected.append(_deb_path(data, arch=arch))
    assert files == tuple(sorted(expected))


@pytest.mark.parametrize(
    ("rel", "match"),
    [
        ("rpm/el9/x86_64/repodata/primary.xml.gz", r"is not a content-addressed index file"),
        ("deb/dists/noble/main/binary-amd64/by-hash/MD5Sum/abc", r"not a content-addressed"),
    ],
)
def test_current_refuses_an_unexpected_file(tmp_path: Path, rel: str, match: str) -> None:
    _put(tmp_path, rel, b"x")
    with pytest.raises(PackageError, match=match):
        generations.current(tmp_path, _T1)


def test_current_refuses_a_file_that_does_not_match_its_name(tmp_path: Path) -> None:
    _put(tmp_path, _deb_path(b"one"), b"two")
    with pytest.raises(PackageError, match=r"does not match the SHA-256 its name carries"):
        generations.current(tmp_path, _T1)


# --- load_previous -------------------------------------------------------------------


def test_load_previous_without_a_manifest_keeps_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    live = _Live(tmp_path)
    assert generations.load_previous(_BASE + "/", live) == []
    assert live.urls[0].startswith(f"{_BASE}/index-generations.json?generation=")
    assert "no index-generations.json at" in capsys.readouterr().out


def test_load_previous_reads_the_generations_newest_first(tmp_path: Path) -> None:
    gens = [
        {"date": "2", "files": [_deb_path(b"new")]},
        {"date": "1", "files": [_deb_path(b"old"), _rpm_path(b"old")]},
    ]
    _put(tmp_path, generations.MANIFEST, json.dumps({"schema": 1, "generations": gens}).encode())
    assert generations.load_previous(_BASE, _Live(tmp_path)) == [
        Generation("2", (_deb_path(b"new"),)),
        Generation("1", (_deb_path(b"old"), _rpm_path(b"old"))),
    ]


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (b"{", r"index-generations\.json is not valid JSON"),
        (b"\xff", r"is not valid JSON"),
        (b"[]", r"is not a schema-1 index generations manifest"),
        (b'{"schema": 2, "generations": []}', r"is not a schema-1"),
        (b'{"schema": 1}', r"generations must be a list"),
        (b'{"schema": 1, "generations": ["x"]}', r"generation 0 needs a date string"),
        (b'{"schema": 1, "generations": [{"files": []}]}', r"generation 0 needs a date"),
        (
            b'{"schema": 1, "generations": [{"date": "1", "files": ["../../etc/passwd"]}]}',
            r"generation 0 lists '\.\./\.\./etc/passwd', not a content-addressed index file",
        ),
        (
            b'{"schema": 1, "generations": [{"date": "1", "files": [3]}]}',
            r"generation 0 lists 3",
        ),
    ],
)
def test_load_previous_refuses_a_malformed_manifest(tmp_path: Path, raw: bytes, match: str) -> None:
    _put(tmp_path, generations.MANIFEST, raw)
    with pytest.raises(PackageError, match=match):
        generations.load_previous(_BASE, _Live(tmp_path))


# --- carry ---------------------------------------------------------------------------


def test_carry_keeps_the_newest_generations_and_prunes_the_rest(tmp_path: Path) -> None:
    live_root, site = tmp_path / "live", tmp_path / "site"
    blobs = {name: name.encode() for name in ("g1", "g2", "g3", "shared")}
    for data in blobs.values():
        _put(live_root, _deb_path(data), data)
    previous = [
        Generation("3", (_deb_path(blobs["g3"]), _deb_path(blobs["shared"]))),
        Generation("2", (_deb_path(blobs["g2"]), _deb_path(blobs["shared"]))),
        Generation("1", (_deb_path(blobs["g1"]),)),
    ]
    live = _Live(live_root)
    kept = generations.carry(site, _BASE, previous, 3, live)
    assert kept == previous[:2]
    for name in ("g3", "g2", "shared"):
        assert (site / _deb_path(blobs[name])).read_bytes() == blobs[name]
    assert not (site / _deb_path(blobs["g1"])).exists()
    # Each file is fetched once, even when two generations share it.
    assert len(live.urls) == 3


def test_carry_does_not_refetch_a_file_the_build_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = tmp_path / "site"
    _put(site, _rpm_path(b"same"), b"same")
    live = _Live(tmp_path / "live")
    generations.carry(site, _BASE, [Generation("1", (_rpm_path(b"same"),))], 2, live)
    assert live.urls == []
    assert "keeping 1 earlier index generation(s) of 1: 0 file(s) carried over" in (
        capsys.readouterr().out
    )


def test_carry_refuses_a_listed_file_that_is_missing(tmp_path: Path) -> None:
    previous = [Generation("1", (_deb_path(b"gone"),))]
    with pytest.raises(PackageError, match=r"listed in the live index-generations\.json, is miss"):
        generations.carry(tmp_path / "site", _BASE, previous, 2, _Live(tmp_path / "live"))


def test_carry_refuses_a_file_that_does_not_match_its_name(tmp_path: Path) -> None:
    _put(tmp_path / "live", _deb_path(b"want"), b"got")
    previous = [Generation("1", (_deb_path(b"want"),))]
    with pytest.raises(PackageError, match=r"does not match the SHA-256 its name carries"):
        generations.carry(tmp_path / "site", _BASE, previous, 2, _Live(tmp_path / "live"))
    assert not (tmp_path / "site" / _deb_path(b"want")).exists()


# --- write_manifest and two publishes end to end --------------------------------------


def _publish(site: Path, live: Path, data: bytes, now: datetime, keep: int) -> None:
    """One index build: write an apt tree for ``data``, carry, write the manifest."""
    deb = site.parent / f"in-{now.hour}/t_1.0.0-1_amd64.deb"
    deb.parent.mkdir(parents=True, exist_ok=True)
    deb.write_bytes(data)
    art = Artifact("o/t", "v1.0.0", deb, "deb", "t", "1.0.0", "1", "amd64", (), _sha(data))
    previous = generations.load_previous(_BASE, _Live(live))
    apt.write(site, [art], ["noble"], lambda p: "Package: t\n", vendor="v", now=now)
    built = generations.current(site, now)
    kept = generations.carry(site, _BASE, previous, keep, _Live(live))
    generations.write_manifest(site, built, kept)


def _amd64_gz_digest(site: Path) -> str:
    release = (site / "deb/dists/noble/Release").read_text()
    (line,) = [ln for ln in release.splitlines() if ln.endswith("binary-amd64/Packages.gz")]
    return line.split()[0]


def test_each_publish_keeps_the_previous_generations_by_hash_files(tmp_path: Path) -> None:
    sites = [tmp_path / f"site{i}" for i in range(4)]
    digests = []
    for i, site in enumerate(sites):
        live = sites[i - 1] if i else tmp_path / "empty"
        _publish(site, live, f"deb {i}".encode(), _T1.replace(hour=i), keep=3)
        digests.append(_amd64_gz_digest(site))
    assert len(set(digests)) == 4
    by_hash = "deb/dists/noble/main/binary-amd64/by-hash/SHA256"
    last = sites[-1]
    # The newest site serves its own generation and the two before it, not the first.
    for d in digests[1:]:
        assert (last / by_hash / d).is_file()
    assert not (last / by_hash / digests[0]).exists()
    # And every one it serves still matches the Release that named it.
    for d, site in zip(digests[1:], sites[1:], strict=True):
        index = site / "deb/dists/noble/main/binary-amd64/Packages.gz"
        assert (last / by_hash / d).read_bytes() == index.read_bytes()
    manifest = _manifest(last)
    assert manifest["schema"] == 1
    assert [g["date"] for g in manifest["generations"]] == [
        _T1.replace(hour=h).isoformat() for h in (3, 2, 1)
    ]
    own = [
        _deb_path((sites[3] / f"deb/dists/noble/main/binary-{arch}/{name}").read_bytes(), arch=arch)
        for arch in ("amd64", "arm64")
        for name in ("Packages", "Packages.gz")
    ]
    assert manifest["generations"][0]["files"] == sorted(own)


def test_two_generations_keep_only_the_previous_one(tmp_path: Path) -> None:
    sites = [tmp_path / f"site{i}" for i in range(3)]
    digests = []
    for i, site in enumerate(sites):
        live = sites[i - 1] if i else tmp_path / "empty"
        _publish(site, live, f"deb {i}".encode(), _T1.replace(hour=i), keep=2)
        digests.append(_amd64_gz_digest(site))
    by_hash = sites[-1] / "deb/dists/noble/main/binary-amd64/by-hash/SHA256"
    assert sorted((by_hash / d).exists() for d in digests) == [False, True, True]
    assert not (by_hash / digests[0]).exists()
    assert len(_manifest(sites[-1])["generations"]) == 2


# --- http_fetch -------------------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("u", code, "x", Message(), None)


def _urlopen(outcomes: list[Any], calls: list[str]) -> Any:
    def urlopen(url: str, timeout: int) -> _Resp:
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Resp(outcome)

    return urlopen


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(generations.time, "sleep", slept.append)
    return slept


def test_http_fetch_returns_the_body(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(generations.urllib.request, "urlopen", _urlopen([b"body"], calls))
    assert generations.http_fetch("https://h/x") == b"body"
    assert calls == ["https://h/x"] and no_sleep == []


def test_http_fetch_404_is_none(monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]) -> None:
    monkeypatch.setattr(generations.urllib.request, "urlopen", _urlopen([_http_error(404)], []))
    assert generations.http_fetch("https://h/x") is None


def test_http_fetch_retries_a_5xx_and_a_network_error(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    outcomes = [_http_error(503), urllib.error.URLError("reset"), TimeoutError(), b"ok"]
    monkeypatch.setattr(generations.urllib.request, "urlopen", _urlopen(outcomes, []))
    assert generations.http_fetch("https://h/x") == b"ok"
    assert len(no_sleep) == 3


def test_http_fetch_other_4xx_is_fatal_at_once(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    monkeypatch.setattr(generations.urllib.request, "urlopen", _urlopen([_http_error(403)], []))
    with pytest.raises(PackageError, match=r"fetching https://h/x: HTTP 403"):
        generations.http_fetch("https://h/x")
    assert no_sleep == []


@pytest.mark.parametrize(
    ("error", "match"),
    [(_http_error(502), r"HTTP 502"), (urllib.error.URLError("down"), r"down")],
)
def test_http_fetch_gives_up_after_the_retries(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float], error: BaseException, match: str
) -> None:
    outcomes = [error] * (generations.retry.MAX_RETRIES + 1)
    monkeypatch.setattr(generations.urllib.request, "urlopen", _urlopen(outcomes, []))
    with pytest.raises(PackageError, match=rf"fetching https://h/x: .*{match}"):
        generations.http_fetch("https://h/x")
    assert len(no_sleep) == generations.retry.MAX_RETRIES
