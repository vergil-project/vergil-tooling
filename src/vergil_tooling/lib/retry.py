"""Retry logic for transient GitHub API and git transport errors.

Shared by ``github.py`` (library wrappers), ``vrg_gh.py`` (CLI wrapper),
``git.py`` and ``vrg_git.py`` (raw git network ops) so every path that
talks to GitHub handles HTTP 401/502/503/504/429, proxy/gateway bodies
("502 Bad Gateway"), ``net/http`` transport failures (TLS handshake, i/o
timeout, connection refused, DNS lookup, EOF) and raw git/SSH transport
drops identically (#2835).

HTTP 401 "Bad credentials" is treated as transient: GitHub's API
(notably the GraphQL endpoint behind ``gh pr checks --watch``)
intermittently rejects a valid token, with the immediately following
call succeeding. Retrying is safe even for write operations because a
401 is rejected at authentication, before any mutation occurs.

Transport-layer failures (TLS handshake timeout, i/o timeout, connection
refused, DNS "no such host", "server misbehaving", EOF) are emitted by
``gh``'s Go ``net/http`` stack when the request never reaches GitHub's
application layer. Like the 401 case they occur before any server-side
mutation, so retrying is safe for writes as well as reads.
"""

from __future__ import annotations

import logging
import random
import subprocess
import time
from typing import Any

log = logging.getLogger(__name__)

MAX_RETRIES = 4
BASE_DELAY_SECS = 2.0
MAX_DELAY_SECS = 60.0
_RETRYABLE_PATTERNS = (
    # HTTP-layer transients
    "http 401",
    "bad credentials",
    "http 502",
    "http 503",
    "http 504",
    "http 429",
    # Proxy/gateway phrasings that carry no "HTTP <code>" token: gh emits
    # "non-200 OK status code: 502 Bad Gateway ..." (and nginx bodies say
    # "502 Bad Gateway"), which the code-only patterns above miss. This gap
    # let a 502 fail `gh pr merge` on the first attempt during the 2026-08-13
    # GitHub incident (#2835).
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "gateway time-out",  # nginx's hyphenated 504 wording
    # net/http transport-layer transients (request never reached the app
    # layer, so retrying is safe for writes too)
    "timed out",
    "timeout",  # "TLS handshake timeout", "i/o timeout", "Client.Timeout"
    "tls handshake",
    "connection reset",
    "connection refused",
    "connection closed",  # SSH transport drop mid-op during an incident
    "kex_exchange_identification",  # SSH handshake aborted by GitHub's frontend
    "no such host",  # DNS resolution failure
    "server misbehaving",  # Go DNS resolver transient
    "eof",  # "unexpected EOF" / "EOF" mid-request
    # raw git transport transients — now reachable because git network ops
    # are wrapped in retry too (#2835), not only the gh/API path.
    "could not read from remote",
    "the remote end hung up",
    # A valid, static SSH key rejected because GitHub's key-lookup backend is
    # degraded presents identically to a real misconfigured key. Per #2835 we
    # retry it, but every attempt is announced (see git.py / vrg_git.py), so a
    # genuine misconfig still surfaces loudly and hard-fails after the last try.
    "permission denied (publickey)",
)


def is_retryable(exc: subprocess.CalledProcessError) -> bool:
    """Return True if the error looks like a transient GitHub API failure."""
    detail = ((exc.stderr or "") + (exc.stdout or "")).lower()
    return any(p in detail for p in _RETRYABLE_PATTERNS)


def compute_delay(attempt: int) -> float:
    """Return a jittered exponential backoff delay for the given attempt."""
    delay: float = min(BASE_DELAY_SECS * (2**attempt), MAX_DELAY_SECS)
    jitter: float = 0.5 + random.random()  # noqa: S311
    return delay * jitter


def run_with_retry(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run ``subprocess.run`` with retry on transient GitHub API errors.

    Requires ``check=True`` and ``capture_output=True`` (or equivalent)
    so that ``CalledProcessError`` carries stderr/stdout for detection.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            return subprocess.run(*args, **kwargs)  # noqa: S603
        except subprocess.CalledProcessError as exc:
            if attempt == MAX_RETRIES or not is_retryable(exc):
                raise
            delay = compute_delay(attempt)
            log.warning(
                "GitHub API error (attempt %d/%d), retrying in %.1fs",
                attempt + 1,
                MAX_RETRIES + 1,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
