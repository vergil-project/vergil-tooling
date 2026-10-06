"""Tests for vergil_tooling.lib.release.package_index (spec §8.3)."""

from __future__ import annotations

import gzip
import io
import urllib.error
from email.message import Message
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.release import package_index
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_BASE = "https://vergil-project.github.io/packages"

_TOML = """\
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

_PKG = """
[package]
builder = "python"
vendor = "vergil"
summary = "Shared tooling"
smoke = "vrg-whoami --mode"

[package.python]
runtime = "3.14.4"
"""

_PACKAGES = (
    "Package: vergil-tooling\nVersion: 2.1.240-1\nArchitecture: amd64\n\n"
    "Package: other\nVersion: 1-1\n"
)

_NOBLE_AMD64 = f"{_BASE}/deb/dists/noble/main/binary-amd64/Packages"

_REPOMD = b"".join(
    [
        b'<repomd xmlns="http://linux.duke.edu/metadata/repo"><revision>1</revision>',
        b'<data type="other"><location href="repodata/x-other.xml.gz"/></data>',
        b'<data type="primary"><location href="repodata/abc-primary.xml.gz"/></data>',
        b"</repomd>",
    ]
)


def _primary(name: str, ver: str, rel: str) -> bytes:
    xml = (
        '<metadata xmlns="http://linux.duke.edu/metadata/common" packages="2">'
        '<package type="rpm"><name>other</name><arch>x86_64</arch>'
        '<version epoch="0" ver="9" rel="9"/></package>'
        f'<package type="rpm"><name>{name}</name>'
        f'<version epoch="0" ver="{ver}" rel="{rel}"/>'
        "</package></metadata>"
    )
    return gzip.compress(xml.encode())


def _boom(*_a: object) -> NoReturn:
    raise AssertionError("must not be called")


def _ctx(tmp_path: Path, package: str | None) -> ReleaseContext:
    (tmp_path / "vergil.toml").write_text(_TOML + (package or ""))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    # builder = "python" requires a uv.lock next to vergil.toml (spec §5.4, #3077).
    (tmp_path / "uv.lock").write_text("version = 1\n")
    return ReleaseContext(
        repo="vergil-project/vergil-tooling",
        version="2.1.240",
        repo_root=tmp_path,
        version_override=None,
    )


@pytest.fixture
def release_ctx(tmp_path: Path) -> ReleaseContext:
    return _ctx(tmp_path, None)


@pytest.fixture
def release_ctx_with_package(tmp_path: Path) -> ReleaseContext:
    return _ctx(tmp_path, _PKG)


def _with_pkg_lines(*lines: str) -> str:
    return _PKG.replace("[package]\n", "[package]\n" + "".join(f"{ln}\n" for ln in lines), 1)


def _clock() -> Callable[[], int]:
    ticks: Iterator[int] = iter(range(0, 10_000, 30))
    return lambda: next(ticks)


# -- deb_has ----------------------------------------------------------------


def test_deb_has() -> None:
    def fetch(url: str) -> bytes:
        hit = url.endswith("/deb/dists/noble/main/binary-amd64/Packages")
        return _PACKAGES.encode() if hit else b""

    name = "vergil-tooling"
    assert package_index.deb_has("https://b", "noble", "amd64", name, "2.1.240-1", fetch)
    assert not package_index.deb_has("https://b", "noble", "amd64", name, "2.1.241-1", fetch)
    assert not package_index.deb_has("https://b", "noble", "arm64", name, "2.1.240-1", fetch)


def test_deb_has_ignores_continuation_lines_and_name_mismatch() -> None:
    text = (
        "Package: vergil-tooling\nVersion: 2.1.239-1\nDescription: x\n more text: here\n\n"
        "Package: vergil-toolingx\nVersion: 2.1.240-1\n\n\n"
    )
    assert not package_index.deb_has(
        "https://b", "noble", "amd64", "vergil-tooling", "2.1.240-1", lambda _u: text.encode()
    )


def test_deb_has_empty_index() -> None:
    assert not package_index.deb_has("https://b", "noble", "amd64", "t", "1-1", lambda _u: b"")


# -- rpm_has ----------------------------------------------------------------


def test_rpm_has_reads_primary_via_repomd() -> None:
    repomd = (
        b'<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary">'
        b'<location href="repodata/abc-primary.xml.gz"/></data></repomd>'
    )
    primary = gzip.compress(
        b'<metadata><package><name>t</name><version epoch="0" ver="2.1.240" rel="1"/>'
        b"</package></metadata>"
    )

    def fetch(url: str) -> bytes:
        return repomd if url.endswith("repomd.xml") else primary

    assert package_index.rpm_has("https://b", "el9", "x86_64", "t", "2.1.240", "1", fetch)


def test_rpm_has_urls_and_mismatches() -> None:
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return _REPOMD if url.endswith("repomd.xml") else _primary("t", "2.1.240", "1")

    assert not package_index.rpm_has("https://b", "el10", "aarch64", "t", "2.1.241", "1", fetch)
    assert not package_index.rpm_has("https://b", "el10", "aarch64", "t", "2.1.240", "2", fetch)
    assert not package_index.rpm_has("https://b", "el10", "aarch64", "u", "2.1.240", "1", fetch)
    assert package_index.rpm_has("https://b", "el10", "aarch64", "t", "2.1.240", "1", fetch)
    assert seen[:2] == [
        "https://b/rpm/el10/aarch64/repodata/repomd.xml",
        "https://b/rpm/el10/aarch64/repodata/abc-primary.xml.gz",
    ]


def test_rpm_has_missing_repomd_or_primary_is_not_visible() -> None:
    assert not package_index.rpm_has("https://b", "el9", "x86_64", "t", "1", "1", lambda _u: b"")

    def fetch(url: str) -> bytes:
        return _REPOMD if url.endswith("repomd.xml") else b""

    assert not package_index.rpm_has("https://b", "el9", "x86_64", "t", "1", "1", fetch)


def test_rpm_has_repomd_without_primary_is_an_error() -> None:
    repomd = b'<repomd><data type="other"/><data type="primary"/></repomd>'
    with pytest.raises(ReleaseError, match="no primary metadata"):
        package_index.rpm_has("https://b", "el9", "x86_64", "t", "1", "1", lambda _u: repomd)


# -- wait_for_index ---------------------------------------------------------


def test_no_package_section_is_a_noop(release_ctx: ReleaseContext) -> None:
    package_index.wait_for_index(release_ctx, fetch=_boom, sleep=_boom, now=_boom)
    assert release_ctx.package_index_url is None
    assert release_ctx.deferred_publish_failures == []


def test_times_out_and_defers(release_ctx_with_package: ReleaseContext) -> None:
    sleeps: list[float] = []
    with pytest.raises(
        ReleaseError, match=r"vergil-tooling 2\.1\.240-1 not visible in .* after 1200s"
    ) as exc_info:
        package_index.wait_for_index(
            release_ctx_with_package,
            fetch=lambda _u: b"",
            sleep=sleeps.append,
            now=_clock(),
        )
    assert release_ctx_with_package.deferred_publish_failures == ["package-index"]
    assert exc_info.value.phase == "package-index"
    assert exc_info.value.command == "poll package index"
    assert exc_info.value.detail is not None
    assert "vergil-project/packages" in exc_info.value.detail
    assert _NOBLE_AMD64 in str(exc_info.value)
    assert sleeps and all(s == 30 for s in sleeps)


def test_timeout_does_not_duplicate_deferred_entry(
    release_ctx_with_package: ReleaseContext,
) -> None:
    release_ctx_with_package.deferred_publish_failures.append("package-index")
    with pytest.raises(ReleaseError):
        package_index.wait_for_index(
            release_ctx_with_package,
            fetch=lambda _u: b"",
            sleep=lambda _s: None,
            now=_clock(),
            timeout=60,
        )
    assert release_ctx_with_package.deferred_publish_failures == ["package-index"]


def test_appears_after_polling(release_ctx_with_package: ReleaseContext) -> None:
    answers = iter([b"", _PACKAGES.encode()])
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return next(answers)

    package_index.wait_for_index(release_ctx_with_package, fetch=fetch, sleep=lambda _s: None)
    assert urls == [_NOBLE_AMD64, _NOBLE_AMD64]
    assert release_ctx_with_package.package_index_url == _NOBLE_AMD64
    assert release_ctx_with_package.deferred_publish_failures == []


def test_fetch_error_propagates_and_holds_refresh(
    release_ctx_with_package: ReleaseContext,
) -> None:
    def fetch(_url: str) -> bytes:
        raise urllib.error.URLError("network down")

    with pytest.raises(urllib.error.URLError):
        package_index.wait_for_index(release_ctx_with_package, fetch=fetch, sleep=_boom)
    assert release_ctx_with_package.deferred_publish_failures == ["package-index"]


def test_package_version_overrides_release_version(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _with_pkg_lines('version = "3.14.4"'))
    text = b"Package: vergil-tooling\nVersion: 3.14.4-1\n"
    package_index.wait_for_index(ctx, fetch=lambda _u: text, sleep=_boom)
    assert ctx.package_index_url == _NOBLE_AMD64


def test_first_deb_target_is_used(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _with_pkg_lines('targets = ["rhel/*/*", "ubuntu/26.04/arm64"]'))
    url = f"{_BASE}/deb/dists/resolute/main/binary-arm64/Packages"
    package_index.wait_for_index(
        ctx, fetch=lambda u: _PACKAGES.encode() if u == url else b"", sleep=_boom
    )
    assert ctx.package_index_url == url


def test_native_deb_target_uses_suite_revision(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _with_pkg_lines('native = ["ubuntu/24.04/*"]'))
    text = b"Package: vergil-tooling\nVersion: 2.1.240-1~noble\n"
    package_index.wait_for_index(ctx, fetch=lambda _u: text, sleep=_boom)
    assert ctx.package_index_url == _NOBLE_AMD64


def test_rpm_only_uses_first_rpm_target(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _with_pkg_lines('targets = ["rhel/*/*"]'))
    repomd_url = f"{_BASE}/rpm/el10/x86_64/repodata/repomd.xml"

    def fetch(url: str) -> bytes:
        if url == repomd_url:
            return _REPOMD
        if url == f"{_BASE}/rpm/el10/x86_64/repodata/abc-primary.xml.gz":
            return _primary("vergil-tooling", "2.1.240", "1")
        return b""

    package_index.wait_for_index(ctx, fetch=fetch, sleep=_boom)
    assert ctx.package_index_url == repomd_url


def test_native_rpm_target_uses_suite_release(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _with_pkg_lines('targets = ["rhel/*/*"]', 'native = ["rhel/10/*"]'))

    def fetch(url: str) -> bytes:
        if url.endswith("repomd.xml"):
            return _REPOMD
        return _primary("vergil-tooling", "2.1.240", "1.el10")

    package_index.wait_for_index(ctx, fetch=fetch, sleep=_boom)
    assert ctx.package_index_url == f"{_BASE}/rpm/el10/x86_64/repodata/repomd.xml"


def test_rpm_timeout_message_names_version_release(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _with_pkg_lines('targets = ["rhel/9/arm64"]'))
    expected = r"vergil-tooling 2\.1\.240-1 not visible in .*el9/aarch64"
    with pytest.raises(ReleaseError, match=expected):
        package_index.wait_for_index(
            ctx, fetch=lambda _u: b"", sleep=lambda _s: None, now=_clock(), timeout=30
        )
    assert ctx.deferred_publish_failures == ["package-index"]


def test_noarch_polls_a_concrete_arch_directory(tmp_path: Path) -> None:
    """noarch packages are listed in every per-arch index (deb binary-<arch>,
    rpm <el>/<arch>/repodata); there is no binary-all or noarch directory."""
    staged = _with_pkg_lines('name = "vergil-archive-keyring"', "noarch = true").replace(
        'builder = "python"', 'builder = "staged"'
    )
    staged = staged.split("[package.python]")[0]
    ctx = _ctx(tmp_path, staged)
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging" / "nfpm.overlay.yaml").write_text(
        "contents:\n  - src: keys/vergil.asc\n    dst: /usr/share/keyrings/k.asc\n"
    )
    text = b"Package: vergil-archive-keyring\nVersion: 2.1.240-1\nArchitecture: all\n"
    package_index.wait_for_index(ctx, fetch=lambda _u: text, sleep=_boom)
    assert ctx.package_index_url == _NOBLE_AMD64


# -- _fetch -----------------------------------------------------------------


def test_fetch_reads_url() -> None:
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = b"data"
    with patch("urllib.request.urlopen", return_value=resp) as m_open:
        assert package_index._fetch("https://b/x") == b"data"
    m_open.assert_called_once_with("https://b/x", timeout=30)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://b/x", code, "msg", Message(), io.BytesIO(b""))


def test_fetch_404_is_empty() -> None:
    with patch("urllib.request.urlopen", side_effect=_http_error(404)):
        assert package_index._fetch("https://b/x") == b""


def test_fetch_other_http_error_propagates() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(500)),
        pytest.raises(urllib.error.HTTPError),
    ):
        package_index._fetch("https://b/x")
