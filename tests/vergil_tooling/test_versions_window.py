"""Tests for vergil_tooling.lib.versions.window (epic vergil-project/.github#407)."""

from __future__ import annotations

import copy
import http.client
import json
import urllib.error
from datetime import date
from email.message import Message
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import FIXTURE_WINDOW
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.window import (
    WINDOW_ENV,
    Window,
    WindowError,
    WindowFamily,
    WindowVersion,
    _http_fetch,
    load_window,
    parse_window,
    window_url,
)

if TYPE_CHECKING:
    from pathlib import Path

_URL = (
    "https://raw.githubusercontent.com/vergil-project/vergil-containers/"
    "v2.1/docker/versions/window.json"
)


def _doc() -> dict[str, Any]:
    """The published v2.1 window, as a mutable document."""
    data: dict[str, Any] = json.loads(FIXTURE_WINDOW.read_bytes())
    return data


def _bytes(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode()


def _no_fetch(url: str) -> bytes:
    raise AssertionError(f"unexpected fetch of {url}")


# -- load_window -------------------------------------------------------------


def test_load_window_parses_schema_1() -> None:
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return FIXTURE_WINDOW.read_bytes()

    window = load_window("v2.1", fetch=fetch, env={})

    assert fetched == [_URL]
    assert window.source == _URL
    assert set(window.languages) == {"python", "java", "go", "rust", "cpp", "typescript"}
    (python,) = window.languages["python"]
    assert python == WindowFamily(
        key="python",
        token_prefix="",
        versions=(
            WindowVersion(version="3.14", eol=None, in_policy=True, runtime_published=True),
            WindowVersion(version="3.13", eol=None, in_policy=True, runtime_published=False),
            WindowVersion(version="3.12", eol=None, in_policy=True, runtime_published=False),
        ),
    )
    clang, gcc = window.languages["cpp"]
    assert (clang.key, clang.token_prefix) == ("cpp-clang", "clang-")
    assert [clang.token(v) for v in clang.versions] == ["clang-20", "clang-19"]
    assert (gcc.key, [gcc.token(v) for v in gcc.versions]) == ("cpp-gcc", ["gcc-14"])
    assert window.languages["go"][0].versions[0].runtime_published is None


def test_window_url_names_the_ref() -> None:
    assert window_url("v2.1") == _URL


def test_load_window_env_override_wins(tmp_path: Path) -> None:
    doc = _doc()
    doc["languages"] = {"go": doc["languages"]["go"]}
    path = tmp_path / "window.json"
    path.write_bytes(_bytes(doc))

    window = load_window("v2.1", fetch=_no_fetch, env={WINDOW_ENV: str(path)})

    assert set(window.languages) == {"go"}
    assert window.source == f"{path} ({WINDOW_ENV})"


def test_load_window_missing_at_ref_fails_loud() -> None:
    # A [dependencies].vergil ref from before the window was published has no
    # window.json: a 404 must fail loud, naming the URL and the override, and
    # never fall back to a built-in or cached list (Review Focus item 3).
    def fetch(url: str) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

    with pytest.raises(WindowError) as exc:
        load_window("v2.0", fetch=fetch, env={})

    message = str(exc.value)
    assert _URL.replace("v2.1", "v2.0") in message
    assert "404" in message
    assert f"{WINDOW_ENV}=<path>" in message


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("no route to host"),
        TimeoutError("timed out"),
        http.client.IncompleteRead(b""),
        ValueError("unknown url type"),
    ],
)
def test_load_window_fetch_failures_fail_loud(error: Exception) -> None:
    def fetch(_url: str) -> bytes:
        raise error

    with pytest.raises(WindowError, match=r"cannot fetch the version window https://"):
        load_window("v2.1", fetch=fetch, env={})


def test_window_error_is_a_versions_error() -> None:
    assert issubclass(WindowError, VersionsError)


def test_load_window_empty_override_fails_loud() -> None:
    with pytest.raises(WindowError, match=f"{WINDOW_ENV} is set but empty"):
        load_window("v2.1", fetch=_no_fetch, env={WINDOW_ENV: ""})


def test_load_window_unreadable_override_fails_loud(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(WindowError) as exc:
        load_window("v2.1", fetch=_no_fetch, env={WINDOW_ENV: str(missing)})
    assert str(missing) in str(exc.value)
    assert f"unset {WINDOW_ENV}" in str(exc.value)


def test_load_window_malformed_fetch_names_url_and_override() -> None:
    with pytest.raises(WindowError) as exc:
        load_window("v2.1", fetch=lambda _url: b"<html>", env={})
    message = str(exc.value)
    assert f"invalid version window {_URL}" in message
    assert f"{WINDOW_ENV}=<path>" in message


def test_load_window_rejects_unknown_schema() -> None:
    doc = _doc()
    doc["schema"] = 2
    with pytest.raises(WindowError, match="unsupported schema 2"):
        load_window("v2.1", fetch=lambda _url: _bytes(doc), env={})


# -- parse_window ------------------------------------------------------------


def test_parse_window_sorts_versions_newest_first() -> None:
    doc = _doc()
    doc["languages"]["python"]["families"][0]["versions"].reverse()
    window = parse_window(_bytes(doc), source="s")
    assert [v.version for v in window.languages["python"][0].versions] == ["3.14", "3.13", "3.12"]


def test_parse_window_reads_eol_dates() -> None:
    doc = _doc()
    doc["languages"]["go"]["families"][0]["versions"][0]["eol"] = "2027-08-12"
    window = parse_window(_bytes(doc), source="s")
    assert window.languages["go"][0].versions[0].eol == date(2027, 8, 12)


def test_parse_window_without_hint() -> None:
    with pytest.raises(WindowError) as exc:
        parse_window(b"[]", source="s")
    assert str(exc.value) == "invalid version window s: the document must be a JSON object"


def _mutate(path: list[str | int], value: object) -> dict[str, Any]:
    doc = copy.deepcopy(_doc())
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return doc


_PY_FAMILY: list[str | int] = ["languages", "python", "families", 0]
_PY_VERSION: list[str | int] = [*_PY_FAMILY, "versions", 0]


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (["schema"], True, "unsupported schema True"),
        (["languages"], {}, "'languages' must be a non-empty object"),
        (["languages", "python"], [], "language 'python' must be an object"),
        (["languages", "python", "families"], [], "'families' must be a non-empty list"),
        (_PY_FAMILY, "python", "each family must be an object"),
        ([*_PY_FAMILY, "key"], "", "family 'key' must be a string"),
        ([*_PY_FAMILY, "token_prefix"], None, "'token_prefix' must be a string"),
        ([*_PY_FAMILY, "versions"], [], "'versions' must be a non-empty list"),
        (_PY_VERSION, "3.14", "each version must be an object"),
        ([*_PY_VERSION, "version"], 3.14, "'version' must be a non-empty string"),
        ([*_PY_VERSION, "eol"], 20301031, "'eol' must be null or a date"),
        ([*_PY_VERSION, "eol"], "next year", "'eol' 'next year' is not an ISO date"),
        ([*_PY_VERSION, "in_policy"], None, "'in_policy' must be a boolean"),
        ([*_PY_VERSION, "runtime_published"], "yes", "'runtime_published' must be null or"),
        ([*_PY_VERSION, "version"], "3.13", "a version token is listed twice"),
    ],
)
def test_parse_window_rejects_malformed_documents(
    path: list[str | int], value: object, message: str
) -> None:
    # An empty or malformed window must never read as "no versions": every
    # violation is a loud WindowError naming what is wrong.
    with pytest.raises(WindowError, match=message.replace("(", r"\(")):
        parse_window(_bytes(_mutate(path, value)), source="s")


def test_parse_window_rejects_non_json() -> None:
    with pytest.raises(WindowError, match="invalid version window s"):
        parse_window(b"\xff", source="s")


def test_window_default_source_is_empty() -> None:
    assert Window(languages={}).source == ""


# -- _http_fetch -------------------------------------------------------------


def test_http_fetch_reads_the_body() -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"{}"
    with patch(
        "vergil_tooling.lib.versions.window.urllib.request.urlopen", return_value=response
    ) as urlopen:
        assert _http_fetch(_URL) == b"{}"
    urlopen.assert_called_once_with(_URL, timeout=30)


def test_http_fetch_propagates_404() -> None:
    error = urllib.error.HTTPError(_URL, 404, "Not Found", Message(), None)
    with (
        patch("vergil_tooling.lib.versions.window.urllib.request.urlopen", side_effect=error),
        pytest.raises(urllib.error.HTTPError),
    ):
        _http_fetch(_URL)
