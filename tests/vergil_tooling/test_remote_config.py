"""Tests for vergil_tooling.lib.remote_config (vergil.toml via the contents API)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from vergil_tooling.lib import github, remote_config
from vergil_tooling.lib.github import GitHubAPIError
from vergil_tooling.lib.remote_config import (
    RemoteConfigError,
    fetch_config_from_ref,
    require_config_at_ref,
)

_VALID_TOML = b"""\
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


def _encoded(payload: bytes) -> dict[str, object]:
    return {"content": base64.b64encode(payload).decode()}


def _api_error(status: str) -> GitHubAPIError:
    return GitHubAPIError(
        1,
        ("gh", "api", "repos/o/r/contents/vergil.toml?ref=cafef00d"),
        f'{{"status":"{status}"}}',
        f"gh: (HTTP {status})",
    )


def _serve(monkeypatch: pytest.MonkeyPatch, response: Any) -> list[tuple[str, ...]]:
    """Answer ``github.read_json`` with *response* (raised when an exception)."""
    calls: list[tuple[str, ...]] = []

    def _read_json(*args: str) -> Any:
        calls.append(args)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(github, "read_json", _read_json)
    return calls


def test_require_config_at_ref_fetches_repo_at_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _serve(monkeypatch, _encoded(_VALID_TOML))

    cfg = require_config_at_ref("o/r", "cafef00d")

    assert cfg.project.primary_language == "python"
    assert calls == [("api", "repos/o/r/contents/vergil.toml?ref=cafef00d")]


def test_require_config_at_ref_missing_file_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _api_error("404"))

    with pytest.raises(RemoteConfigError) as excinfo:
        require_config_at_ref("o/r", "cafef00d")

    message = str(excinfo.value)
    assert "vergil.toml not found in o/r at cafef00d" in message
    assert "Contents: read" in message


@pytest.mark.parametrize(
    "response",
    [
        _encoded(b"[project\nbroken"),  # invalid TOML
        _encoded(b"[project]\nrepository-type = 'nope'\n"),  # invalid config
        _encoded(b"\xff\xfe"),  # not UTF-8
        {"content": "!!!not-base64"},  # not base64
        [],  # not a file response (e.g. a directory listing)
        {"encoding": "base64"},  # no content field
    ],
    ids=["toml", "config", "utf8", "base64", "non-dict", "no-content"],
)
def test_require_config_at_ref_unparseable_is_actionable(
    monkeypatch: pytest.MonkeyPatch, response: Any
) -> None:
    _serve(monkeypatch, response)

    unparseable = r"vergil\.toml of o/r@cafef00d could not be parsed"
    with pytest.raises(RemoteConfigError, match=unparseable):
        require_config_at_ref("o/r", "cafef00d")


def test_require_config_at_ref_other_api_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _api_error("403"))

    with pytest.raises(GitHubAPIError):
        require_config_at_ref("o/r", "cafef00d")


def test_fetch_config_from_ref_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _api_error("404"))

    assert fetch_config_from_ref("o/r", "develop") is None


def test_is_not_found_reads_stdout_and_stderr() -> None:
    assert remote_config.is_not_found(GitHubAPIError(1, ("gh",), "HTTP 404", ""))
    assert remote_config.is_not_found(GitHubAPIError(1, ("gh",), "", "HTTP 404"))
    assert not remote_config.is_not_found(GitHubAPIError(1, ("gh",), "", "HTTP 500"))
