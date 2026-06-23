"""Retry logic for transient GitHub API errors.

Shared by ``github.py`` (library wrappers) and ``vrg_gh.py`` (CLI wrapper)
so both paths handle HTTP 401/502/503/504/429 and network timeouts
identically.

HTTP 401 "Bad credentials" is treated as transient: GitHub's API
(notably the GraphQL endpoint behind ``gh pr checks --watch``)
intermittently rejects a valid token, with the immediately following
call succeeding. Retrying is safe even for write operations because a
401 is rejected at authentication, before any mutation occurs.
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
    "http 401",
    "bad credentials",
    "http 502",
    "http 503",
    "http 504",
    "http 429",
    "timed out",
    "connection reset",
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
