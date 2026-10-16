"""Tests for image-ref parsing and registry tag listing (spec §8.3, epic .github#381)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from email.message import Message
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps import registry
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.registry import ImageRef

if TYPE_CHECKING:
    from collections.abc import Callable

Response = tuple[int, dict[str, str], bytes]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "ghcr.io/aquasecurity/trivy:0.71.0",
            ImageRef(
                "ghcr.io", "aquasecurity/trivy", "0.71.0", "ghcr.io/aquasecurity/trivy:0.71.0"
            ),
        ),
        (
            "python:3.14-slim",
            ImageRef("registry-1.docker.io", "library/python", "3.14-slim", "python:3.14-slim"),
        ),
        (
            "hadolint/hadolint:v2.12.0",
            ImageRef(
                "registry-1.docker.io", "hadolint/hadolint", "v2.12.0", "hadolint/hadolint:v2.12.0"
            ),
        ),
        (
            "localhost:5000/x:1.0.0",
            ImageRef("localhost:5000", "x", "1.0.0", "localhost:5000/x:1.0.0"),
        ),
        (
            "localhost/x:1.0.0",
            ImageRef("localhost", "x", "1.0.0", "localhost/x:1.0.0"),
        ),
        (
            "docker.io/library/python:3.14",
            ImageRef(
                "registry-1.docker.io",
                "library/python",
                "3.14",
                "docker.io/library/python:3.14",
            ),
        ),
        (
            "docker.io/python:3.14",
            ImageRef("registry-1.docker.io", "library/python", "3.14", "docker.io/python:3.14"),
        ),
    ],
)
def test_parse_image_ref(text: str, expected: ImageRef) -> None:
    assert registry.parse_image_ref(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "ubuntu:latest",
        "ubuntu",
        "alpine@sha256:abc",
        "ubuntu:24.04@sha256:abc",
        "ghcr.io/vergil-project/prod-base:1.2.3",
        "${{ matrix.image }}",
        "$BASE_IMAGE",
        "ghcr.io/x/${{ inputs.y }}:1.0.0",
        "localhost:5000/x",
        "Upper/Case:1.0.0",
    ],
)
def test_parse_image_ref_skips(text: str) -> None:
    assert registry.parse_image_ref(text) is None


def _opener(
    responses: list[Response],
) -> tuple[Callable[[urllib.request.Request], Response], list[tuple[str, str | None]]]:
    calls: list[tuple[str, str | None]] = []

    def opener(req: urllib.request.Request) -> Response:
        calls.append((req.full_url, req.get_header("Authorization")))
        return responses.pop(0)

    return opener, calls


_CHALLENGE = (
    'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
    'scope="repository:aquasecurity/trivy:pull"'
)


def test_list_tags_anonymous_token_and_pagination() -> None:
    ref = ImageRef("ghcr.io", "aquasecurity/trivy", "0.71.0", "t")
    opener, calls = _opener(
        [
            (401, {"WWW-Authenticate": _CHALLENGE}, b""),
            (200, {}, json.dumps({"token": "T"}).encode()),
            (
                200,
                {"Link": '</v2/aquasecurity/trivy/tags/list?n=1000&last=0.71.0>; rel="next"'},
                json.dumps({"tags": ["0.70.0", "0.71.0"]}).encode(),
            ),
            (200, {}, json.dumps({"tags": ["0.74.0"]}).encode()),
        ]
    )
    assert registry.list_tags(ref, opener=opener) == ["0.70.0", "0.71.0", "0.74.0"]
    assert calls[0] == ("https://ghcr.io/v2/aquasecurity/trivy/tags/list?n=1000", None)
    assert calls[1][0] == (
        "https://ghcr.io/token?service=ghcr.io&scope=repository%3Aaquasecurity%2Ftrivy%3Apull"
    )
    assert calls[2][1] == "Bearer T"
    assert calls[3] == (
        "https://ghcr.io/v2/aquasecurity/trivy/tags/list?n=1000&last=0.71.0",
        "Bearer T",
    )


def test_list_tags_header_names_are_case_insensitive() -> None:
    ref = ImageRef("ghcr.io", "aquasecurity/trivy", "0.71.0", "t")
    opener, _ = _opener(
        [
            (401, {"www-authenticate": _CHALLENGE}, b""),
            (200, {}, json.dumps({"access_token": "A"}).encode()),
            (200, {}, json.dumps({"tags": None}).encode()),
        ]
    )
    assert registry.list_tags(ref, opener=opener) == []


def test_list_tags_without_auth_and_non_string_tags_dropped() -> None:
    ref = ImageRef("localhost:5000", "x", "1.0.0", "t")
    opener, calls = _opener([(200, {}, json.dumps({"tags": ["1.0.0", 7]}).encode())])
    assert registry.list_tags(ref, opener=opener) == ["1.0.0"]
    assert calls == [("https://localhost:5000/v2/x/tags/list?n=1000", None)]


def test_list_tags_fails_loud_on_error_status() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener([(500, {}, b"boom")])
    with pytest.raises(UpdateDepsError, match=r"ghcr\.io/a/b.*HTTP 500"):
        registry.list_tags(ref, opener=opener)


def test_list_tags_fails_loud_on_second_401() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener(
        [
            (401, {"WWW-Authenticate": _CHALLENGE}, b""),
            (200, {}, json.dumps({"token": "T"}).encode()),
            (401, {"WWW-Authenticate": _CHALLENGE}, b""),
        ]
    )
    with pytest.raises(UpdateDepsError, match="HTTP 401"):
        registry.list_tags(ref, opener=opener)


def test_list_tags_fails_loud_on_unsupported_challenge() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener([(401, {"WWW-Authenticate": "Basic realm=x"}, b"")])
    with pytest.raises(UpdateDepsError, match="Bearer"):
        registry.list_tags(ref, opener=opener)


def test_list_tags_fails_loud_on_missing_challenge() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener([(401, {}, b"")])
    with pytest.raises(UpdateDepsError, match="Bearer"):
        registry.list_tags(ref, opener=opener)


@pytest.mark.parametrize(
    "challenge",
    [
        'Bearer service="ghcr.io"',
        'Bearer realm="file:///etc/passwd",service="x"',
        'Bearer realm="http://ghcr.io/token"',
    ],
)
def test_list_tags_rejects_non_https_realm(challenge: str) -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, calls = _opener([(401, {"WWW-Authenticate": challenge}, b"")])
    with pytest.raises(UpdateDepsError, match="realm"):
        registry.list_tags(ref, opener=opener)
    assert len(calls) == 1


def test_list_tags_fails_loud_on_token_status() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener([(401, {"WWW-Authenticate": _CHALLENGE}, b""), (403, {}, b"")])
    with pytest.raises(UpdateDepsError, match="token endpoint returned HTTP 403"):
        registry.list_tags(ref, opener=opener)


def test_list_tags_fails_loud_on_missing_token() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener([(401, {"WWW-Authenticate": _CHALLENGE}, b""), (200, {}, b"{}")])
    with pytest.raises(UpdateDepsError, match="no token"):
        registry.list_tags(ref, opener=opener)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_list_tags_fails_loud_on_bad_tags_body(body: bytes) -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener([(200, {}, body)])
    with pytest.raises(UpdateDepsError, match="JSON object"):
        registry.list_tags(ref, opener=opener)


def test_list_tags_fails_loud_on_bad_token_body() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    opener, _ = _opener([(401, {"WWW-Authenticate": _CHALLENGE}, b""), (200, {}, b"<html>")])
    with pytest.raises(UpdateDepsError, match="JSON object"):
        registry.list_tags(ref, opener=opener)


def test_list_tags_rejects_non_https_next_link() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")
    link = '<file:///etc/passwd>; rel="next"'
    opener, calls = _opener([(200, {"Link": link}, b'{"tags": []}')])
    with pytest.raises(UpdateDepsError, match="not an https URL"):
        registry.list_tags(ref, opener=opener)
    assert len(calls) == 1


def test_list_tags_wraps_network_errors() -> None:
    ref = ImageRef("ghcr.io", "a/b", "1.0.0", "t")

    def opener(req: urllib.request.Request) -> Response:
        raise urllib.error.URLError("no route")

    with pytest.raises(UpdateDepsError, match="no route") as info:
        registry.list_tags(ref, opener=opener)
    assert info.value.command == "list tags ghcr.io/a/b"


class _FakeResponse:
    status = 200

    def __init__(self) -> None:
        self.headers = Message()
        self.headers["Link"] = "x"

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return b"body"


def test_default_opener_returns_status_headers_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, float]] = []

    def urlopen(req: urllib.request.Request, timeout: float) -> _FakeResponse:
        seen.append((req.full_url, timeout))
        return _FakeResponse()

    monkeypatch.setattr(registry.urllib.request, "urlopen", urlopen)
    status, headers, body = registry._open(urllib.request.Request("https://h/v2/"))
    assert (status, headers, body) == (200, {"Link": "x"}, b"body")
    assert seen == [("https://h/v2/", 30)]


def test_default_opener_returns_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    hdrs = Message()
    hdrs["WWW-Authenticate"] = "Bearer x"

    def urlopen(req: urllib.request.Request, timeout: float) -> _FakeResponse:
        raise urllib.error.HTTPError(req.full_url, 401, "nope", hdrs, io.BytesIO(b"denied"))

    monkeypatch.setattr(registry.urllib.request, "urlopen", urlopen)
    assert registry._open(urllib.request.Request("https://h/v2/")) == (
        401,
        {"WWW-Authenticate": "Bearer x"},
        b"denied",
    )
