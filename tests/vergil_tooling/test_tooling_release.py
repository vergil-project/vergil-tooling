"""Tests for vergil_tooling.lib.tooling_release (#4550)."""

from __future__ import annotations

import urllib.error
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import fixture_package_index
from vergil_tooling.lib.tooling_release import (
    ToolingResolveError,
    _http_fetch,
    deb_suites,
    index_url,
    releases_on_line,
    resolve_tooling_tag,
)

_BASE = "https://vergil-project.github.io/packages/deb/dists"


def _index(*releases: str, other: str = "") -> bytes:
    return fixture_package_index(releases) + other.encode()


class _Fetch:
    """Serve a ``Packages`` index per URL, recording each URL read."""

    def __init__(self, by_suite: dict[str, bytes]) -> None:
        self.by_suite = by_suite
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        suite = url.removeprefix(f"{_BASE}/").split("/")[0]
        return self.by_suite[suite]


def _every_suite(data: bytes) -> _Fetch:
    return _Fetch(dict.fromkeys(deb_suites(), data))


def test_deb_suites_are_the_registrys_ubuntu_suites() -> None:
    assert deb_suites() == ["noble", "resolute"]


def test_index_url() -> None:
    assert index_url("noble", "arm64") == f"{_BASE}/noble/main/binary-arm64/Packages"


def test_releases_on_line_keeps_only_releases_on_that_line() -> None:
    versions = {"2.1.9", "2.1.10", "2.10.1", "2.0.3", "2.1", "2.1.4rc1"}
    assert releases_on_line("v2.1", versions) == {"2.1.9", "2.1.10"}


def test_a_line_resolves_to_its_newest_release_numerically() -> None:
    fetch = _every_suite(_index("2.1.9", "2.1.10", "2.1.320", "2.2.0", "2.10.0"))
    assert resolve_tooling_tag("v2.1", arch="arm64", fetch=fetch) == "v2.1.320"
    assert fetch.urls == [index_url(suite, "arm64") for suite in deb_suites()]


def test_only_vergil_tooling_stanzas_count() -> None:
    other = "Package: vergil-python3.15\nVersion: 2.1.999-1\n\n"
    fetch = _every_suite(_index("2.1.4", other=other))
    assert resolve_tooling_tag("v2.1", arch="amd64", fetch=fetch) == "v2.1.4"


def test_a_release_missing_from_one_suite_is_not_chosen() -> None:
    # The newest release every suite lists: the base image may use either.
    fetch = _Fetch({"noble": _index("2.1.4", "2.1.5"), "resolute": _index("2.1.4")})
    assert resolve_tooling_tag("v2.1", arch="amd64", fetch=fetch) == "v2.1.4"


def test_no_release_common_to_every_suite_is_an_error() -> None:
    fetch = _Fetch({"noble": _index("2.1.5"), "resolute": _index("2.1.4")})
    with pytest.raises(ToolingResolveError, match="every suite"):
        resolve_tooling_tag("v2.1", arch="amd64", fetch=fetch)


def test_a_line_with_no_release_is_an_error() -> None:
    fetch = _every_suite(_index("2.0.7"))
    with pytest.raises(ToolingResolveError, match=r"no vergil-tooling release on the v2\.1 line"):
        resolve_tooling_tag("v2.1", arch="amd64", fetch=fetch)


def test_an_unreadable_index_is_an_error_naming_it() -> None:
    def offline(url: str) -> bytes:
        raise urllib.error.URLError("no route to host")

    with pytest.raises(ToolingResolveError) as info:
        resolve_tooling_tag("v2.1", arch="arm64", fetch=offline)
    assert index_url("noble", "arm64") in str(info.value)
    assert "no route to host" in str(info.value)


def test_a_404_is_an_error() -> None:
    def missing(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

    with pytest.raises(ToolingResolveError, match="404"):
        resolve_tooling_tag("v2.1", arch="arm64", fetch=missing)


def test_an_undecodable_index_is_an_error() -> None:
    with pytest.raises(ToolingResolveError, match="cannot read"):
        resolve_tooling_tag("v2.1", arch="arm64", fetch=_every_suite(b"\xff\xfe"))


@pytest.mark.parametrize("tag", ["v2.1.320", "develop", "feature/4550-x"])
def test_an_exact_release_or_dev_ref_needs_no_lookup(tag: str) -> None:
    def no_network(url: str) -> bytes:
        msg = f"unexpected index fetch: {url}"
        raise AssertionError(msg)

    assert resolve_tooling_tag(tag, arch="arm64", fetch=no_network) == tag


def test_the_default_fetch_is_the_hermetic_index() -> None:
    # conftest replaces the HTTPS GET; the real one is never reached in tests.
    assert resolve_tooling_tag("v2.0", arch="amd64") == "v2.0.7"


def test_http_fetch_reads_the_url_with_a_timeout() -> None:
    # _http_fetch was imported before conftest replaced the module attribute.
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = b"index"
    url = index_url("noble", "amd64")
    with patch("urllib.request.urlopen", return_value=resp) as urlopen:
        assert _http_fetch(url) == b"index"
    urlopen.assert_called_once_with(url, timeout=30)
