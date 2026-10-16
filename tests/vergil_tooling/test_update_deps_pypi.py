from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.update_deps import pypi
from vergil_tooling.lib.update_deps.context import UpdateDepsError


def test_latest_version_reads_info_version() -> None:
    urls: list[str] = []

    def fetch(url: str) -> bytes:
        urls.append(url)
        return json.dumps({"info": {"version": "2.1.0"}}).encode()

    assert pypi.latest_version("Example_Pkg", fetch=fetch) == "2.1.0"
    assert urls == ["https://pypi.org/pypi/example-pkg/json"]


def test_latest_version_fails_loud_on_http_error() -> None:
    def fetch(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b""))

    with pytest.raises(UpdateDepsError, match="example-pkg") as exc_info:
        pypi.latest_version("example-pkg", fetch=fetch)
    assert exc_info.value.command == "GET https://pypi.org/pypi/example-pkg/json"
    assert exc_info.value.phase == "update"


def test_latest_version_fails_loud_on_invalid_json() -> None:
    with pytest.raises(UpdateDepsError, match="PyPI lookup failed for p"):
        pypi.latest_version("p", fetch=lambda url: b"not json")


def test_latest_version_fails_loud_on_bad_payload() -> None:
    with pytest.raises(UpdateDepsError, match="no info.version"):
        pypi.latest_version("p", fetch=lambda url: b"{}")


@pytest.mark.parametrize(
    "payload",
    [b"[]", b'{"info": []}', b'{"info": {"version": ""}}', b'{"info": {"version": 3}}'],
)
def test_latest_version_fails_loud_on_malformed_shapes(payload: bytes) -> None:
    with pytest.raises(UpdateDepsError, match="no info.version"):
        pypi.latest_version("p", fetch=lambda url: payload)


def test_fetch_reads_url() -> None:
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = b"data"
    with patch("urllib.request.urlopen", return_value=resp) as m_open:
        assert pypi._fetch("https://pypi.org/pypi/x/json") == b"data"
    m_open.assert_called_once_with("https://pypi.org/pypi/x/json", timeout=30)
