"""Tests for the App token source (``lib/app_token.py``, epic vergil-project/.github#1168 Task 1).

The App's endpoints are faked at the ``urllib.request.urlopen`` boundary, and
the token source's clock is a fake, so renewal is driven without waiting.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from vergil_tooling.lib import app_token
from vergil_tooling.lib.app_token import (
    UPSTREAM_REFRESH_PERMISSIONS,
    AppCredentials,
    AppTokenError,
    AppTokenSource,
    read_credentials_from_env,
)

SLUG = "vergil-automation"
REPO = "vergil-project/vergil-containers"
INSTALLATION_ID = 4242


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def creds(rsa_key: rsa.RSAPrivateKey) -> AppCredentials:
    return AppCredentials(client_id="Iv23liClientId", private_key=_pem(rsa_key))


def _pem_body_lines(pem: str) -> list[str]:
    """The base64 lines of *pem*: what must never appear in output."""
    return [line for line in pem.splitlines() if line and not line.startswith("-----")]


# --------------------------------------------------------------------------- fake GitHub


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    timeout: float | None


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _UnreadableBody:
    def read(self, *_args: object) -> bytes:
        raise OSError("connection dropped while reading the error body")

    def close(self) -> None:
        return None


def http_error(
    code: int,
    body: object = None,
    *,
    request_id: str | None = None,
    unreadable: bool = False,
    no_headers: bool = False,
) -> urllib.error.HTTPError:
    headers: http.client.HTTPMessage | None = None
    if not no_headers:
        headers = http.client.HTTPMessage()
        if request_id:
            headers["X-GitHub-Request-Id"] = request_id
    if unreadable:
        fp: Any = _UnreadableBody()
    elif isinstance(body, bytes):
        fp = io.BytesIO(body)
    else:
        fp = io.BytesIO(json.dumps(body).encode() if body is not None else b"")
    hdrs = cast("http.client.HTTPMessage", headers)  # GitHub's errors can carry no headers
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", hdrs, fp)


Outcome = BaseException | tuple[int, object] | Callable[[Request], tuple[int, object]]


@dataclass
class FakeGitHub:
    """The App's endpoints. ``script`` queues outcomes per (method, path) ahead of the defaults."""

    app_slug: str = SLUG
    login: Callable[[str], str] = lambda _token: f"{SLUG}[bot]"
    requests: list[Request] = field(default_factory=list)
    script: dict[tuple[str, str], list[Outcome]] = field(default_factory=dict)
    minted: list[str] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)

    def queue(self, method: str, path: str, *outcomes: Outcome) -> None:
        self.script.setdefault((method, path), []).extend(outcomes)

    def calls(self, method: str, path: str) -> list[Request]:
        return [r for r in self.requests if r.method == method and r.path == path]

    def _default(self, req: Request) -> tuple[int, object]:
        key = (req.method, req.path)
        if key == ("GET", "/app"):
            return 200, {"slug": self.app_slug, "id": 99}
        if key == ("GET", f"/repos/{REPO}/installation"):
            return 200, {"id": INSTALLATION_ID}
        if key == ("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens"):
            token = f"ghs_token{len(self.minted) + 1}"
            self.minted.append(token)
            return 201, {"token": token, "expires_at": "2026-11-16T13:00:00Z"}
        if key == ("POST", "/graphql"):
            token = req.headers["Authorization"].split(" ", 1)[1]
            return 200, {"data": {"viewer": {"login": self.login(token)}}}
        if key == ("DELETE", "/installation/token"):
            self.revoked.append(req.headers["Authorization"].split(" ", 1)[1])
            return 204, b""
        raise AssertionError(f"unexpected request {key}")

    def urlopen(self, req: urllib.request.Request, timeout: float | None = None) -> _Response:
        path = req.full_url.removeprefix("https://api.github.com")
        raw = req.data
        body = json.loads(raw) if isinstance(raw, bytes) and raw else None
        recorded = Request(
            req.get_method(), path, {k.title(): v for k, v in req.header_items()}, body, timeout
        )
        self.requests.append(recorded)
        queued = self.script.get((recorded.method, path))
        outcome: Outcome = queued.pop(0) if queued else self._default
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, tuple):
            status, payload = outcome
        else:
            status, payload = outcome(recorded)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return _Response(status, data)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _source(creds: AppCredentials, clock: Clock, **kwargs: Any) -> AppTokenSource:
    return AppTokenSource(
        creds, REPO, UPSTREAM_REFRESH_PERMISSIONS, expected_slug=SLUG, clock=clock, **kwargs
    )


def _mint_path() -> str:
    return f"/app/installations/{INSTALLATION_ID}/access_tokens"


# --------------------------------------------------------------------------- minting


def test_first_token_is_scoped_to_repo_and_permissions(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    with _source(creds, clock) as source:
        assert source.token() == "ghs_token1"
    (mint,) = gh.calls("POST", _mint_path())
    assert mint.body == {
        "repositories": ["vergil-containers"],
        "permissions": {
            "contents": "write",
            "pull_requests": "write",
            "workflows": "write",
            "metadata": "read",
        },
    }
    assert dict(UPSTREAM_REFRESH_PERMISSIONS) == mint.body["permissions"]
    assert mint.headers["Authorization"].startswith("Bearer ")
    assert all(r.timeout is not None for r in gh.requests)


def test_jwt_signed_in_process_rs256(
    gh: FakeGitHub,
    creds: AppCredentials,
    clock: Clock,
    rsa_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_subprocess(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the token source must not start a subprocess")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    before = int(time.time())
    with _source(creds, clock) as source:
        source.token()
    after = int(time.time())

    jwt = gh.calls("GET", "/app")[0].headers["Authorization"].removeprefix("Bearer ")
    header_b64, claims_b64, sig_b64 = jwt.split(".")

    def decode(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    rsa_key.public_key().verify(
        decode(sig_b64),
        f"{header_b64}.{claims_b64}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert json.loads(decode(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(decode(claims_b64))
    assert claims["iss"] == "Iv23liClientId"
    assert before - 60 <= claims["iat"] <= after - 60
    assert before + 540 <= claims["exp"] <= after + 540
    # Every App call carrying the JWT goes through the same in-process path.
    assert (
        gh.calls("GET", f"/repos/{REPO}/installation")[0].headers["Authorization"]
        == f"Bearer {jwt}"
    )


def test_app_slug_mismatch_fails(gh: FakeGitHub, creds: AppCredentials, clock: Clock) -> None:
    gh.app_slug = "someone-elses-app"
    source = _source(creds, clock)
    with pytest.raises(AppTokenError) as info:
        source.token()
    assert "'someone-elses-app'" in str(info.value)
    assert f"'{SLUG}'" in str(info.value)
    assert gh.calls("POST", _mint_path()) == []


def test_identity_mismatch_revokes_the_new_token_and_fails(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    gh.login = lambda _token: "other-app[bot]"
    source = _source(creds, clock)
    with pytest.raises(AppTokenError, match=r"belongs to 'other-app\[bot\]'"):
        source.token()
    assert gh.revoked == ["ghs_token1"]
    source.close()
    assert gh.revoked == ["ghs_token1"]


def test_identity_check_reports_graphql_errors(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    gh.queue("POST", "/graphql", (200, {"data": None, "errors": [{"message": "boom"}]}))
    with pytest.raises(AppTokenError, match="GraphQL errors: .*boom"):
        _source(creds, clock).token()


def test_identity_check_without_viewer_fails(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    gh.queue("POST", "/graphql", (200, {"data": {"viewer": None}}))
    with pytest.raises(AppTokenError, match="belongs to None"):
        _source(creds, clock).token()


def test_identity_check_uses_the_new_token(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    with _source(creds, clock) as source:
        source.token()
    (check,) = gh.calls("POST", "/graphql")
    assert check.headers["Authorization"] == "bearer ghs_token1"
    assert check.body == {"query": "query { viewer { login } }"}


def test_installation_without_id_fails(gh: FakeGitHub, creds: AppCredentials, clock: Clock) -> None:
    gh.queue("GET", f"/repos/{REPO}/installation", (200, {"id": True}))
    with pytest.raises(AppTokenError, match="has no numeric id"):
        _source(creds, clock).token()


def test_mint_without_token_fails_without_echoing_the_response(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    gh.queue("POST", _mint_path(), (201, {"token": "", "secret": "ghs_leak"}))
    with pytest.raises(AppTokenError, match="returned no token") as info:
        _source(creds, clock).token()
    assert "ghs_leak" not in str(info.value)


# --------------------------------------------------------------------------- renewal


def test_renews_after_interval_and_revokes_old(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=app_token.__name__)
    with _source(creds, clock) as source:
        first = source.token()
        clock.now = 1801.0
        second = source.token()
        assert second != first
        assert gh.revoked == [first]
        (delete,) = gh.calls("DELETE", "/installation/token")
        assert delete.headers["Authorization"] == f"token {first}"
        assert source.token() == second
    assert gh.revoked == [first, second]
    text = caplog.text
    assert "renewed the automation token" in text
    assert "revoked the replaced automation token" in text
    assert "on exit" in text
    assert "ghs_token" not in text


def test_no_renewal_before_interval(gh: FakeGitHub, creds: AppCredentials, clock: Clock) -> None:
    source = _source(creds, clock)
    first = source.token()
    clock.now = 1800.0
    assert source.token() == first
    assert len(gh.calls("POST", _mint_path())) == 1
    assert gh.revoked == []


def test_renewal_interval_is_configurable(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    source = _source(creds, clock, renew_after_secs=10)
    source.token()
    clock.now = 11.0
    assert source.token() == "ghs_token2"


def test_failed_revocation_of_replaced_token_fails_but_close_revokes_new(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    source = _source(creds, clock)
    source.token()
    clock.now = 1801.0
    gh.queue("DELETE", "/installation/token", http_error(403, {"message": "nope"}))
    with pytest.raises(AppTokenError, match="revoking the replaced automation token"):
        source.token()
    source.close()
    assert gh.revoked == ["ghs_token2"]


def test_failed_renewal_keeps_the_current_token(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    source = _source(creds, clock)
    source.token()
    clock.now = 1801.0
    gh.queue("POST", _mint_path(), http_error(403, {"message": "suspended"}))
    with pytest.raises(AppTokenError, match="suspended"):
        source.token()
    source.close()
    assert gh.revoked == ["ghs_token1"]


# --------------------------------------------------------------------------- revocation


def test_revoke_retry_401_counts_as_revoked(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue(
        "DELETE",
        "/installation/token",
        ConnectionResetError("connection reset by peer"),
        http_error(401, {"message": "Bad credentials"}),
    )
    source.close()
    assert len(gh.calls("DELETE", "/installation/token")) == 2
    assert len(sleeps) == 1


def test_revoke_401_on_first_attempt_fails(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue("DELETE", "/installation/token", http_error(401, {"message": "Bad credentials"}))
    with pytest.raises(AppTokenError, match="on exit failed .*HTTP 401"):
        source.close()
    assert len(gh.calls("DELETE", "/installation/token")) == 1


def test_revoke_5xx_retried_then_fails_naming_step(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue(
        "DELETE",
        "/installation/token",
        http_error(502),
        http_error(503),
        http_error(500, request_id="ABCD:1234"),
    )
    with pytest.raises(AppTokenError) as info:
        source.close()
    message = str(info.value)
    assert f"revoking the automation token for {REPO} on exit" in message
    assert "DELETE /installation/token" in message
    assert "HTTP 500 (request ID ABCD:1234)" in message
    assert len(gh.calls("DELETE", "/installation/token")) == 3
    assert sleeps == [1.0, 2.0]


def test_revoke_network_error_then_success(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue(
        "DELETE",
        "/installation/token",
        urllib.error.URLError(TimeoutError("timed out")),
        http.client.IncompleteRead(b""),
    )
    source.close()
    assert gh.revoked == ["ghs_token1"]
    assert len(sleeps) == 2


def test_revoke_network_error_exhausted_fails(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue("DELETE", "/installation/token", *[ConnectionRefusedError("refused")] * 3)
    with pytest.raises(AppTokenError, match="network error .ConnectionRefusedError"):
        source.close()


def test_revoke_certificate_error_is_not_retried(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue(
        "DELETE", "/installation/token", urllib.error.URLError(ssl.SSLCertVerificationError("bad"))
    )
    with pytest.raises(AppTokenError, match="SSLCertVerificationError"):
        source.close()
    assert sleeps == []


def test_revoke_unexpected_status_fails(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue("DELETE", "/installation/token", (200, {}))
    with pytest.raises(AppTokenError, match="unexpected HTTP 200, expected 204"):
        source.close()


def test_close_is_idempotent(gh: FakeGitHub, creds: AppCredentials, clock: Clock) -> None:
    source = _source(creds, clock)
    source.token()
    source.close()
    source.close()
    with source:
        pass
    assert gh.revoked == ["ghs_token1"]


def test_close_before_first_token_revokes_nothing(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    with _source(creds, clock):
        pass
    assert gh.requests == []


def test_failed_close_is_not_retried_by_a_second_close(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    source = _source(creds, clock)
    source.token()
    gh.queue("DELETE", "/installation/token", http_error(404))
    with pytest.raises(AppTokenError):
        source.close()
    source.close()
    assert len(gh.calls("DELETE", "/installation/token")) == 1


def test_context_exit_revokes_on_failure(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock
) -> None:
    msg = "the work failed"
    with pytest.raises(ValueError, match=msg), _source(creds, clock) as source:
        source.token()
        raise ValueError(msg)
    assert gh.revoked == ["ghs_token1"]


def test_token_after_close_fails(gh: FakeGitHub, creds: AppCredentials, clock: Clock) -> None:
    source = _source(creds, clock)
    source.close()
    with pytest.raises(AppTokenError, match="is closed"):
        source.token()


# --------------------------------------------------------------------------- the lookups' retries


def test_lookup_retries_transient_answers(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    gh.queue("GET", "/app", http_error(401), http_error(503))
    gh.queue("POST", _mint_path(), ConnectionResetError("reset"))
    with _source(creds, clock) as source:
        assert source.token() == "ghs_token1"
    assert len(gh.calls("GET", "/app")) == 3
    assert sleeps == [1.0, 2.0, 1.0]


def test_lookup_answer_fails_at_once(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    gh.queue(
        "GET",
        f"/repos/{REPO}/installation",
        http_error(404, {"message": "Not Found"}, request_id="R1"),
    )
    with pytest.raises(AppTokenError) as info:
        _source(creds, clock).token()
    assert str(info.value) == (
        f"looking up the App's installation on {REPO} failed "
        f"(GET /repos/{REPO}/installation): HTTP 404 (request ID R1): GitHub said: Not Found"
    )
    assert sleeps == []


def test_lookup_retries_exhausted_fail(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    gh.queue("GET", "/app", http_error(429), http_error(429), http_error(429))
    with pytest.raises(AppTokenError, match="reading the App behind the key failed .* HTTP 429"):
        _source(creds, clock).token()


def test_lookup_network_errors(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, sleeps: list[float]
) -> None:
    gh.queue("GET", "/app", *[urllib.error.URLError("name resolution")] * 1)
    with pytest.raises(AppTokenError, match=r"network error \(str: name resolution\)"):
        _source(creds, clock).token()
    gh.queue("GET", "/app", *[TimeoutError("slow")] * 3)
    with pytest.raises(AppTokenError, match="TimeoutError: slow"):
        _source(creds, clock).token()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [(b"<html>", "was not JSON"), ([1, 2], "was not a JSON object")],
)
def test_lookup_bad_payload_fails(
    gh: FakeGitHub, creds: AppCredentials, clock: Clock, payload: object, reason: str
) -> None:
    gh.queue("GET", "/app", (200, payload))
    with pytest.raises(AppTokenError, match=reason):
        _source(creds, clock).token()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(403, unreadable=True), "HTTP 403 (response body unreadable)"),
        (http_error(403, b"not json"), "HTTP 403"),
        (http_error(403, {"documentation_url": "x"}), "HTTP 403"),
        (http_error(403, no_headers=True), "HTTP 403"),
    ],
)
def test_http_error_text_variants(
    gh: FakeGitHub,
    creds: AppCredentials,
    clock: Clock,
    error: urllib.error.HTTPError,
    expected: str,
) -> None:
    gh.queue("GET", "/app", error)
    with pytest.raises(AppTokenError) as info:
        _source(creds, clock).token()
    assert str(info.value).endswith(f"): {expected}")


# ------------------------------------------------------------------ the key and credentials


def test_key_absent_from_repr_errors_and_logs(
    gh: FakeGitHub,
    creds: AppCredentials,
    clock: Clock,
    sleeps: list[float],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    body_lines = _pem_body_lines(creds.private_key)
    source = _source(creds, clock)
    reprs = [repr(creds), str(creds), repr(source)]
    messages: list[str] = []

    gh.queue("GET", "/app", http_error(500), http_error(500), http_error(500))
    with pytest.raises(AppTokenError) as info:
        source.token()
    messages.append(str(info.value))
    source.token()
    reprs.append(repr(source))
    gh.queue("DELETE", "/installation/token", http_error(502), http_error(401))
    source.close()
    reprs.append(repr(source))

    bad = AppCredentials(client_id="x", private_key=creds.private_key.replace("A", "B", 5))
    with pytest.raises(AppTokenError) as bad_info:
        _source(bad, clock).token()
    messages.append(str(bad_info.value))
    assert bad_info.value.__cause__ is None
    assert bad_info.value.__suppress_context__

    out = capsys.readouterr()
    haystack = "\n".join([*reprs, *messages, caplog.text, out.out, out.err])
    assert "<redacted>" in reprs[0]
    for line in body_lines:
        assert line not in haystack
    for token in gh.minted:
        assert token not in haystack
    assert "Bearer" not in haystack
    assert reprs[2:] == [
        f"AppTokenSource(repo='{REPO}', app='{SLUG}', unminted)",
        f"AppTokenSource(repo='{REPO}', app='{SLUG}', live)",
        f"AppTokenSource(repo='{REPO}', app='{SLUG}', closed)",
    ]


def test_non_rsa_key_fails(gh: FakeGitHub, clock: Clock) -> None:
    ec_pem = _pem(ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(AppTokenError, match="not an RSA private key"):
        _source(AppCredentials(client_id="x", private_key=ec_pem), clock).token()
    assert gh.requests == []


def test_garbage_key_fails(gh: FakeGitHub, clock: Clock) -> None:
    with pytest.raises(AppTokenError, match="not a usable unencrypted PEM private key"):
        _source(AppCredentials(client_id="x", private_key="hunter2"), clock).token()


def test_read_credentials_pops_both_vars(
    monkeypatch: pytest.MonkeyPatch, creds: AppCredentials
) -> None:
    monkeypatch.setenv("VRG_AUTOMATION_CLIENT_ID", " Iv23liClientId \n")
    monkeypatch.setenv("VRG_AUTOMATION_PRIVATE_KEY", creds.private_key)
    got = read_credentials_from_env()
    assert got == creds
    assert "VRG_AUTOMATION_CLIENT_ID" not in os.environ
    assert "VRG_AUTOMATION_PRIVATE_KEY" not in os.environ


def test_read_credentials_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VRG_AUTOMATION_CLIENT_ID", raising=False)
    monkeypatch.delenv("VRG_AUTOMATION_PRIVATE_KEY", raising=False)
    assert read_credentials_from_env() is None


@pytest.mark.parametrize(
    ("client_id", "key", "named"),
    [
        ("Iv23li", None, "$VRG_AUTOMATION_PRIVATE_KEY is unset"),
        (None, "-----BEGIN PRIVATE KEY-----", "$VRG_AUTOMATION_CLIENT_ID is unset"),
        ("  ", "-----BEGIN PRIVATE KEY-----", "$VRG_AUTOMATION_CLIENT_ID is unset"),
        ("", " ", "$VRG_AUTOMATION_CLIENT_ID and $VRG_AUTOMATION_PRIVATE_KEY are unset"),
    ],
)
def test_read_credentials_one_var_only_raises(
    monkeypatch: pytest.MonkeyPatch, client_id: str | None, key: str | None, named: str
) -> None:
    for name, value in (
        ("VRG_AUTOMATION_CLIENT_ID", client_id),
        ("VRG_AUTOMATION_PRIVATE_KEY", key),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(AppTokenError, match=named.replace("$", r"\$")) as info:
        read_credentials_from_env()
    assert "BEGIN PRIVATE KEY" not in str(info.value)
    assert "VRG_AUTOMATION_CLIENT_ID" not in os.environ
    assert "VRG_AUTOMATION_PRIVATE_KEY" not in os.environ


# --------------------------------------------------------------------------- construction


@pytest.mark.parametrize(
    ("repo", "slug", "permissions", "renew", "reason"),
    [
        ("no-slash", SLUG, {"contents": "read"}, 1800, "must be <owner>/<name>"),
        ("a/b/c", SLUG, {"contents": "read"}, 1800, "must be <owner>/<name>"),
        ("/b", SLUG, {"contents": "read"}, 1800, "must be <owner>/<name>"),
        ("a/", SLUG, {"contents": "read"}, 1800, "must be <owner>/<name>"),
        ("a/b", " ", {"contents": "read"}, 1800, "expected_slug is empty"),
        ("a/b", SLUG, {}, 1800, "at least one permission"),
        ("a/b", SLUG, {"contents": "read"}, 0, "must be positive"),
    ],
)
def test_constructor_refuses_bad_arguments(
    creds: AppCredentials,
    repo: str,
    slug: str,
    permissions: dict[str, str],
    renew: int,
    reason: str,
) -> None:
    with pytest.raises(AppTokenError, match=reason):
        AppTokenSource(creds, repo, permissions, expected_slug=slug, renew_after_secs=renew)


def test_defaults_use_monotonic_clock_and_half_hour(creds: AppCredentials) -> None:
    source = AppTokenSource(creds, REPO, UPSTREAM_REFRESH_PERMISSIONS, expected_slug=SLUG)
    assert source.repo == REPO
    assert source._clock is time.monotonic
    assert source._renew_after == 1800


def test_permissions_constant_is_read_only() -> None:
    with pytest.raises(TypeError):
        cast("dict[str, str]", UPSTREAM_REFRESH_PERMISSIONS)["administration"] = "write"
