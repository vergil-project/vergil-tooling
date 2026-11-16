from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.update_deps import sparse_index
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.sparse_index import IndexVersion, SparseIndexError


def _line(vers: str, *, yanked: bool = False, rust_version: str | None = None) -> str:
    data: dict[str, object] = {"name": "x", "vers": vers, "deps": [], "yanked": yanked}
    if rust_version is not None:
        data["rust_version"] = rust_version
    return json.dumps(data)


def _body(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


@pytest.mark.parametrize(
    ("crate", "path"),
    [
        ("a", "1/a"),
        ("ab", "2/ab"),
        ("abc", "3/a/abc"),
        ("serde", "se/rd/serde"),
        ("Inflector", "in/fl/inflector"),
    ],
)
def test_index_path_rule(crate: str, path: str) -> None:
    assert sparse_index.index_path(crate) == path
    assert sparse_index.index_url(crate) == f"https://index.crates.io/{path}"


def test_versions_parses_every_line() -> None:
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return _body(_line("1.0.0"), "", _line("1.1.0", yanked=True, rust_version="1.85"))

    assert sparse_index.versions("serde", fetch) == [
        IndexVersion(vers="1.0.0", yanked=False, rust_version=None),
        IndexVersion(vers="1.1.0", yanked=True, rust_version="1.85"),
    ]
    assert urls == ["https://index.crates.io/se/rd/serde"]


def test_newest_stable_skips_yanked_and_prerelease() -> None:
    entries = [
        IndexVersion("1.0.0", yanked=False, rust_version=None),
        IndexVersion("1.10.0", yanked=False, rust_version=None),
        IndexVersion("1.9.0", yanked=False, rust_version=None),
        IndexVersion("1.11.0", yanked=True, rust_version=None),
        IndexVersion("2.0.0-rc.1", yanked=False, rust_version=None),
    ]
    newest = sparse_index.newest_stable(entries)
    assert newest is not None
    assert newest.vers == "1.10.0"


def test_newest_stable_none_when_nothing_stable() -> None:
    entries = [
        IndexVersion("1.0.0", yanked=True, rust_version=None),
        IndexVersion("2.0.0-alpha", yanked=False, rust_version=None),
    ]
    assert sparse_index.newest_stable(entries) is None


def test_index_failure_raises() -> None:
    def fetch(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b""))

    with pytest.raises(SparseIndexError, match="Sparse index read failed for serde") as exc_info:
        sparse_index.versions("serde", fetch)
    err = exc_info.value
    assert isinstance(err, UpdateDepsError)
    assert err.phase == "update"
    assert err.command == "https://index.crates.io/se/rd/serde"


def test_index_failure_on_undecodable_body() -> None:
    with pytest.raises(SparseIndexError, match="read failed"):
        sparse_index.versions("serde", lambda url: b"\xff\xfe")


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
        (json.dumps({"vers": "1.0", "yanked": False}), "'vers' is not a version"),
        (json.dumps({"vers": 1, "yanked": False}), "'vers' is not a version"),
        (json.dumps({"vers": "1.0.0"}), "'yanked' is not a boolean"),
        (json.dumps({"vers": "1.0.0", "yanked": False, "rust_version": 1}), "rust-version"),
        (
            json.dumps({"vers": "1.0.0", "yanked": False, "rust_version": "1.x"}),
            "rust-version",
        ),
    ],
)
def test_malformed_index_line_raises(line: str, why: str) -> None:
    with pytest.raises(SparseIndexError, match=why) as exc_info:
        sparse_index.versions("serde", lambda url: _body(_line("0.9.0"), line))
    assert "line 2" in str(exc_info.value)


def test_index_with_no_versions_raises() -> None:
    with pytest.raises(SparseIndexError, match="lists no versions"):
        sparse_index.versions("serde", lambda url: b"\n")


def test_version_key_orders_by_precedence() -> None:
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0",
        "1.0.1+build",
    ]
    shuffled = [ordered[i] for i in (5, 2, 6, 0, 4, 1, 3)]
    assert sorted(shuffled, key=sparse_index.version_key) == ordered


def test_version_key_rejects_non_semver() -> None:
    with pytest.raises(ValueError, match="not a semver version"):
        sparse_index.version_key("1.0")


def test_rust_version_key_pads_missing_parts() -> None:
    assert sparse_index.rust_version_key("1") == (1, 0, 0)
    assert sparse_index.rust_version_key("1.85") == (1, 85, 0)
    assert sparse_index.rust_version_key("1.85.1") == (1, 85, 1)
    with pytest.raises(ValueError, match="not a rust-version"):
        sparse_index.rust_version_key("1.85.0.1")


def test_fetch_url_reads_url() -> None:
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = b"data"
    with patch("urllib.request.urlopen", return_value=resp) as m_open:
        assert sparse_index.fetch_url("https://index.crates.io/1/a") == b"data"
    m_open.assert_called_once_with("https://index.crates.io/1/a", timeout=30)
