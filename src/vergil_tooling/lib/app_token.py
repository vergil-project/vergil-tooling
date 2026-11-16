"""The App token source: repo-scoped automation tokens minted, renewed and revoked in-process.

A mechanized job that must outlive one installation token (a token lasts one
hour) holds the automation App's client ID and private key and asks an
:class:`AppTokenSource` for a current token whenever it needs one (epic
vergil-project/.github#1168, spec §6.1). The source:

- signs a short-lived App JWT in-process with ``cryptography`` (RS256), never
  by running ``openssl`` or any other child process;
- checks that the key belongs to the App it expects (``GET /app``'s ``slug``);
- looks up the App's installation on the one target repo and mints an
  installation token limited to that repo and to the permissions it was given;
- checks each new token's identity with GraphQL ``viewer { login }``, which
  must be ``<expected_slug>[bot]``, the same rule as
  :func:`vergil_tooling.lib.automation_identity.verify_token_is_app`;
- renews the token once it is older than ``renew_after_secs`` and revokes the
  replaced one at once (``DELETE /installation/token``);
- revokes the current token on :meth:`AppTokenSource.close`, which the context
  manager's exit calls.

**Key hygiene.** The key and every token stay in this process's memory. Every
call that carries the JWT or a token is made here with :mod:`urllib.request`,
never through ``gh`` or ``git``, so neither appears in argv or a child's
environment. No ``repr``, exception message or log line carries the key, the
JWT or a token: messages name the step, the endpoint and GitHub's answer only.
:func:`read_credentials_from_env` pops the two variables from ``os.environ``,
so the process's children start without them.

**Retries.** The lookups and the mint retry a network error, a 401, a 429 and
any 5xx, up to :data:`_ATTEMPTS` attempts: a 401 is retried because GitHub
intermittently rejects valid credentials (#4168), and a repeated mint is
harmless, since a token whose response never arrived was never in anyone's
hands. Revocation is its own path: it retries only network errors and 5xx
responses, and a 401 on a retry means an earlier attempt revoked the token and
its response was lost, so the token counts as revoked. A 401 on the first
attempt, or any other failure left after the last attempt, raises
:class:`AppTokenError` naming the step.

This module is separate from the App mode in :mod:`vergil_tooling.lib.github`
(``_load_app_config``, ``get_installation_token``), which serves the agent VMs
from a key file and mints installation-wide tokens; the two never mix.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import socket
import ssl
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, cast

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)

CLIENT_ID_ENV = "VRG_AUTOMATION_CLIENT_ID"
PRIVATE_KEY_ENV = "VRG_AUTOMATION_PRIVATE_KEY"

UPSTREAM_REFRESH_PERMISSIONS: Mapping[str, str] = types.MappingProxyType(
    {
        "contents": "write",
        "pull_requests": "write",
        "workflows": "write",
        "metadata": "read",
    }
)

_API = "https://api.github.com"
_VIEWER_LOGIN_QUERY = "query { viewer { login } }"

# The JWT is backdated 60 seconds against clock drift and lives 9 minutes,
# inside GitHub's 10-minute ceiling.
_JWT_BACKDATE_SECS = 60
_JWT_LIFETIME_SECS = 9 * 60

_ATTEMPTS = 3
_BACKOFF_BASE_SECS = 1.0
_REQUEST_TIMEOUT_SECS = 30.0
_MESSAGE_MAX = 300
_UNAUTHORIZED = 401
_NO_CONTENT = 204
_SERVER_ERROR = range(500, 600)


class AppTokenError(RuntimeError):
    """The App's credentials, a token, or a call to the App's API failed."""


@dataclass(frozen=True)
class AppCredentials:
    """The automation App's client ID and PEM private key. The ``repr`` never shows the key."""

    client_id: str
    private_key: str = field(repr=False)

    def __repr__(self) -> str:
        return f"AppCredentials(client_id={self.client_id!r}, private_key=<redacted>)"


def read_credentials_from_env() -> AppCredentials | None:
    """Pop the App's credentials from ``os.environ``.

    Both ``$VRG_AUTOMATION_CLIENT_ID`` and ``$VRG_AUTOMATION_PRIVATE_KEY`` are
    removed whether or not they are usable, so no child process is started
    with either. Returns ``None`` when neither was set. Raises
    :class:`AppTokenError` when only one was set, or one is set but blank; the
    message names the variables, never their values.
    """
    client_id = os.environ.pop(CLIENT_ID_ENV, None)
    private_key = os.environ.pop(PRIVATE_KEY_ENV, None)
    if client_id is None and private_key is None:
        return None
    client_id = (client_id or "").strip()
    private_key = private_key or ""
    missing = [
        name
        for name, value in ((CLIENT_ID_ENV, client_id), (PRIVATE_KEY_ENV, private_key))
        if not value.strip()
    ]
    if missing:
        msg = (
            f"the automation App's credentials are incomplete: ${' and $'.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} unset or empty; "
            f"set both ${CLIENT_ID_ENV} and ${PRIVATE_KEY_ENV}, or neither"
        )
        raise AppTokenError(msg)
    return AppCredentials(client_id=client_id, private_key=private_key)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_key(pem: str) -> rsa.RSAPrivateKey:
    """The RSA private key in *pem*; an unusable key raises without echoing it."""
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        # ``from None``: the parser's own error is not shown, so nothing about
        # the key's content can reach a message or a traceback.
        msg = f"${PRIVATE_KEY_ENV} is not a usable unencrypted PEM private key"
        raise AppTokenError(msg) from None
    if not isinstance(key, rsa.RSAPrivateKey):
        msg = f"${PRIVATE_KEY_ENV} is not an RSA private key, which GitHub Apps require"
        raise AppTokenError(msg)
    return key


def _sign_jwt(creds: AppCredentials) -> str:
    """An RS256 App JWT for *creds*, signed in-process."""
    key = _load_key(creds.private_key)
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iat": now - _JWT_BACKDATE_SECS,
        "exp": now + _JWT_LIFETIME_SECS,
        "iss": creds.client_id,
    }
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def _is_network_error(exc: BaseException) -> bool:
    """True for a transport failure that may be a blip, judged as ``lib.github`` does (#4186).

    ``urlopen`` wraps a failure while connecting in a ``URLError`` whose
    ``reason`` is the socket error; one raised while reading arrives bare, and
    a lost or truncated response is an ``http.client.HTTPException``. A
    certificate that fails to verify, or a malformed URL, is not a blip.
    """
    if isinstance(exc, http.client.HTTPException):
        return True
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, TimeoutError | ConnectionError | socket.gaierror | ssl.SSLEOFError)


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    """The status and GitHub's ``message``, which carry no credential."""
    base = f"HTTP {exc.code}"
    request_id = exc.headers.get("X-GitHub-Request-Id") if exc.headers else None
    if request_id:
        base += f" (request ID {request_id})"
    try:
        body = exc.read()
    except (OSError, http.client.HTTPException):
        return f"{base} (response body unreadable)"
    finally:
        exc.close()
    try:
        data = json.loads(body)
    except ValueError:
        return base
    message = data.get("message") if isinstance(data, dict) else None
    if isinstance(message, str) and message:
        return f"{base}: GitHub said: {message[:_MESSAGE_MAX]}"
    return base


def _network_error_text(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return f"network error ({type(reason).__name__}: {reason})"


def _backoff(attempt: int) -> None:
    time.sleep(_BACKOFF_BASE_SECS * 2**attempt)


class _Endpoint:
    """One HTTP call to the GitHub API, with its step name for messages."""

    def __init__(
        self,
        step: str,
        method: str,
        path: str,
        *,
        auth: str,
        body: Mapping[str, object] | None = None,
    ) -> None:
        self.step = step
        self.method = method
        self.path = path
        self._auth = auth
        self._body = body

    def _request(self) -> urllib.request.Request:
        data = None if self._body is None else json.dumps(self._body).encode()
        req = urllib.request.Request(  # noqa: S310 -- a fixed https:// API base
            f"{_API}{self.path}", method=self.method, data=data
        )
        req.add_header("Authorization", self._auth)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return req

    def send(self) -> tuple[int, bytes]:
        """One attempt: the status and body, or the ``urllib`` error unchanged."""
        with urllib.request.urlopen(  # noqa: S310 -- a fixed https:// API base
            self._request(), timeout=_REQUEST_TIMEOUT_SECS
        ) as resp:
            return int(resp.status), resp.read()

    def fail(self, detail: str) -> AppTokenError:
        return AppTokenError(f"{self.step} failed ({self.method} {self.path}): {detail}")


def _call_json(endpoint: _Endpoint) -> dict[str, Any]:
    """*endpoint*'s JSON object answer, retrying a network error, 401, 429 or 5xx."""
    for attempt in range(_ATTEMPTS):
        last = attempt == _ATTEMPTS - 1
        try:
            _status, body = endpoint.send()
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {_UNAUTHORIZED, 429} or exc.code in _SERVER_ERROR
            detail = _http_error_text(exc)
            if last or not retryable:
                raise endpoint.fail(detail) from None
            log.warning(
                "%s: %s (attempt %d/%d), retrying", endpoint.step, detail, attempt + 1, _ATTEMPTS
            )
        except (OSError, http.client.HTTPException) as exc:
            detail = _network_error_text(exc)
            if last or not _is_network_error(exc):
                raise endpoint.fail(detail) from None
            log.warning(
                "%s: %s (attempt %d/%d), retrying", endpoint.step, detail, attempt + 1, _ATTEMPTS
            )
        else:
            try:
                payload = json.loads(body)
            except ValueError:
                raise endpoint.fail("the response was not JSON") from None
            if not isinstance(payload, dict):
                raise endpoint.fail("the response was not a JSON object") from None
            return cast("dict[str, Any]", payload)
        _backoff(attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _revoke(token: str, step: str) -> None:
    """Revoke *token* (``DELETE /installation/token``), with revocation's own retry rule.

    Retries network errors and 5xx only. A 401 on a retry counts as revoked:
    an earlier attempt's response may have been lost after GitHub acted on it.
    A 401 on the first attempt, or any failure left after the last attempt,
    raises :class:`AppTokenError` naming *step*.
    """
    endpoint = _Endpoint(step, "DELETE", "/installation/token", auth=f"token {token}")
    for attempt in range(_ATTEMPTS):
        last = attempt == _ATTEMPTS - 1
        try:
            status, _body = endpoint.send()
        except urllib.error.HTTPError as exc:
            detail = _http_error_text(exc)
            if exc.code == _UNAUTHORIZED and attempt > 0:
                log.info(
                    "%s: GitHub answered 401 on attempt %d, so an earlier attempt "
                    "revoked the token and its response was lost; counted as revoked",
                    step,
                    attempt + 1,
                )
                return
            if last or exc.code not in _SERVER_ERROR:
                raise endpoint.fail(detail) from None
            log.warning("%s: %s (attempt %d/%d), retrying", step, detail, attempt + 1, _ATTEMPTS)
        except (OSError, http.client.HTTPException) as exc:
            detail = _network_error_text(exc)
            if last or not _is_network_error(exc):
                raise endpoint.fail(detail) from None
            log.warning("%s: %s (attempt %d/%d), retrying", step, detail, attempt + 1, _ATTEMPTS)
        else:
            if status != _NO_CONTENT:
                raise endpoint.fail(f"unexpected HTTP {status}, expected 204")
            return
        _backoff(attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _log_event(event: str) -> None:
    """Log one token-lifecycle *event* with the wall-clock time (spec §6.2, Logging).

    Every renewal and revocation goes through here, and *event* is built only
    from the repo name and the token's age, never from a token, the JWT or
    the key: one place to check that nothing secret reaches the log.
    """
    log.info("%s: %s", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), event)


class AppTokenSource:
    """Installation tokens for one repo, minted on first use, renewed and revoked.

    Use it as a context manager: the exit revokes the current token, on success
    and on failure. :meth:`token` returns the current token, minting the first
    one on first use and renewing it once it is older than
    *renew_after_secs* (measured with *clock*); the replaced token is revoked
    at once. Every new token's identity is checked against
    ``<expected_slug>[bot]``.
    """

    def __init__(
        self,
        creds: AppCredentials,
        repo: str,
        permissions: Mapping[str, str],
        *,
        expected_slug: str,
        renew_after_secs: int = 1800,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        owner, sep, name = repo.partition("/")
        if not sep or not owner or not name or "/" in name:
            msg = f"the token source's repo must be <owner>/<name>, got {repo!r}"
            raise AppTokenError(msg)
        if not expected_slug.strip():
            msg = "the token source needs the automation App's slug (expected_slug is empty)"
            raise AppTokenError(msg)
        if not permissions:
            msg = "the token source needs at least one permission"
            raise AppTokenError(msg)
        if renew_after_secs <= 0:
            msg = f"renew_after_secs must be positive, got {renew_after_secs}"
            raise AppTokenError(msg)
        self._creds = creds
        self._owner = owner
        self._name = name
        self._permissions = dict(permissions)
        self._expected_slug = expected_slug.strip()
        self._renew_after = renew_after_secs
        self._clock = clock
        self._token: str | None = None
        self._minted_at = 0.0
        self._closed = False

    @property
    def repo(self) -> str:
        return f"{self._owner}/{self._name}"

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("live" if self._token else "unminted")
        return f"AppTokenSource(repo={self.repo!r}, app={self._expected_slug!r}, {state})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def token(self) -> str:
        """The current token: minted on first use, renewed once older than the interval."""
        if self._closed:
            msg = f"the token source for {self.repo} is closed"
            raise AppTokenError(msg)
        if self._token is None:
            self._token = self._mint()
            self._minted_at = self._clock()
            _log_event(f"minted the automation token for {self.repo}")
            return self._token
        age = self._clock() - self._minted_at
        if age > self._renew_after:
            old = self._token
            self._token = self._mint()
            self._minted_at = self._clock()
            _log_event(
                f"renewed the automation token for {self.repo} (the old one was {int(age)}s old)"
            )
            _revoke(old, f"revoking the replaced automation token for {self.repo}")
            _log_event(f"revoked the replaced automation token for {self.repo}")
        return self._token

    def close(self) -> None:
        """Revoke the current token, if any. Idempotent: a second call does nothing."""
        if self._closed:
            return
        self._closed = True
        token, self._token = self._token, None
        if token is None:
            return
        _revoke(token, f"revoking the automation token for {self.repo} on exit")
        _log_event(f"revoked the automation token for {self.repo} on exit")

    def _mint(self) -> str:
        """A new token for the repo, its App and identity checked."""
        auth = f"Bearer {_sign_jwt(self._creds)}"
        app = _call_json(_Endpoint("reading the App behind the key", "GET", "/app", auth=auth))
        slug = app.get("slug")
        if slug != self._expected_slug:
            msg = (
                f"the App key belongs to the App {slug!r}, not the automation App "
                f"{self._expected_slug!r}"
            )
            raise AppTokenError(msg)
        repo_path = (
            f"/repos/{urllib.parse.quote(self._owner, safe='')}/"
            f"{urllib.parse.quote(self._name, safe='')}/installation"
        )
        installation = _call_json(
            _Endpoint(
                f"looking up the App's installation on {self.repo}", "GET", repo_path, auth=auth
            )
        )
        installation_id = installation.get("id")
        if not isinstance(installation_id, int) or isinstance(installation_id, bool):
            msg = f"the App's installation on {self.repo} has no numeric id"
            raise AppTokenError(msg)
        minted = _call_json(
            _Endpoint(
                f"minting an installation token for {self.repo}",
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                auth=auth,
                body={"repositories": [self._name], "permissions": self._permissions},
            )
        )
        token = minted.get("token")
        if not isinstance(token, str) or not token:
            # Never echo the response: it may carry a token.
            msg = f"minting an installation token for {self.repo} returned no token"
            raise AppTokenError(msg)
        try:
            self._check_identity(token)
        except AppTokenError:
            _revoke(token, f"revoking the rejected automation token for {self.repo}")
            raise
        return token

    def _check_identity(self, token: str) -> None:
        expected = f"{self._expected_slug}[bot]"
        data = _call_json(
            _Endpoint(
                f"checking the new token's identity for {self.repo}",
                "POST",
                "/graphql",
                auth=f"bearer {token}",
                body={"query": _VIEWER_LOGIN_QUERY},
            )
        )
        result = data.get("data")
        viewer = result.get("viewer") if isinstance(result, dict) else None
        login = viewer.get("login") if isinstance(viewer, dict) else None
        if login != expected:
            errors = data.get("errors")
            detail = f"; GraphQL errors: {str(errors)[:_MESSAGE_MAX]}" if errors else ""
            msg = f"the new token for {self.repo} belongs to {login!r}, not {expected!r}{detail}"
            raise AppTokenError(msg)
