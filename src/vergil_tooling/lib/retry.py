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

HTTP 404 is **fatal by default**: a 404 normally means the resource does not
exist, so retrying it would only delay a real error (a mistyped run ID, a PR
number from user input or config). The one exception is a resource whose
existence GitHub itself has *just* established — a run ID read from
``gh run list``, a PR URL returned by ``gh pr create`` or ``gh pr list``, an
issue number from ``gh issue create``/``list``. GitHub's API can list or return
such an ID moments before it can serve it (the 2.1.232 ``confirm-main``
failure, #3137), so there a 404 means "not readable yet", not "missing". Wrap
exactly those reads in :func:`retry_known_resource`, which retries a 404 with
the same backoff as transient errors under a ~60 s time budget and then fails
with a clear :class:`KnownResourceNotFoundError`. Never wrap a read whose ID
came from user input or config — it must keep failing fast.
"""

from __future__ import annotations

import logging
import random
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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


# Total time a just-discovered resource may keep returning 404 before we give
# up. GitHub's list-before-readable lag is seconds (#3137); 60 s is generous
# while still surfacing a real problem promptly.
KNOWN_RESOURCE_BUDGET_SECS = 60.0
# Only consulted by retry_known_resource — never part of _RETRYABLE_PATTERNS.
# gh renders a missing resource as "HTTP 404: Not Found" (REST), "release not
# found", or "Could not resolve to a/an <Type>" (GraphQL: pr/issue view).
_NOT_FOUND_PATTERNS = (
    "http 404",
    "not found",
    "could not resolve to a",
)


def is_not_found(exc: subprocess.CalledProcessError) -> bool:
    """Return True if the error is GitHub reporting the resource as missing."""
    detail = ((exc.stderr or "") + (exc.stdout or "")).lower()
    return any(p in detail for p in _NOT_FOUND_PATTERNS)


class KnownResourceNotFoundError(subprocess.CalledProcessError):
    """A resource GitHub just reported still returned 404 after the budget."""

    def __init__(self, resource: str, budget: float, last: subprocess.CalledProcessError) -> None:
        super().__init__(last.returncode, last.cmd, last.stdout, last.stderr)
        self.resource = resource
        self.budget = budget

    def __str__(self) -> str:
        after = f"{self.budget:g}s"
        message = f"{self.resource} was reported by GitHub but still returns 404 after {after}"
        detail = ((self.stderr or "") + (self.stdout or "")).strip()
        return f"{message}: {detail}" if detail else message


def retry_known_resource[T](
    fn: Callable[[], T],
    *,
    resource: str,
    budget: float = KNOWN_RESOURCE_BUDGET_SECS,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> T:
    """Call *fn*, retrying a 404 on a resource whose existence is established.

    Use only when the ID was **just obtained from GitHub itself** (see the
    module docstring). A not-found error is retried with
    :func:`compute_delay`'s backoff until *budget* seconds have elapsed —
    the last sleep is clamped to land on the deadline, followed by one final
    attempt — and then :class:`KnownResourceNotFoundError` names *resource*.
    Any other error propagates immediately (transient errors are already
    retried inside the ``github``/``run_with_retry`` layer *fn* calls).
    *sleep* and *clock* default to :func:`time.sleep`/:func:`time.monotonic`
    (resolved at call time) and are injectable for tests.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + budget
    attempt = 0
    while True:
        try:
            return fn()
        except subprocess.CalledProcessError as exc:
            if not is_not_found(exc):
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise KnownResourceNotFoundError(resource, budget, exc) from exc
            delay = min(compute_delay(attempt), remaining)
            log.warning(
                "%s not readable yet (404 right after GitHub reported it), retrying in %.1fs",
                resource,
                delay,
            )
            sleep(delay)
            attempt += 1
