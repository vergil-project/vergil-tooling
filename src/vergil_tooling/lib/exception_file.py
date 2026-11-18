"""The time-boxed exception rules the npm gates share.

The audit gate's ``.npm-audit-exceptions`` (:mod:`vergil_tooling.lib.npm_audit`)
and the license gate's ``.npm-license-exceptions`` follow the same rules: one
entry per line, ``#`` comments and blank lines ignored, an ``exp:<YYYY-MM-DD>``
expiry, a tracking issue, and an expiry window counted from the day the gate
runs. Each gate parses its own entry shape on top of these rules and reports
every bad line in one :class:`ExceptionFileError`.
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_EXPIRY = re.compile(r"^exp:(\d{4}-\d{2}-\d{2})$")
_TRACKING = re.compile(
    r"^(?:(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+"
    r"|https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+)$"
)


class ExceptionFileError(ValueError):
    """The exceptions file has entries the gate refuses; ``problems`` names each."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"{source}: " + "; ".join(problems))


def content_lines(text: str) -> Iterator[tuple[int, str]]:
    """Each entry line, numbered from 1 and stripped, skipping blanks and ``#`` comments."""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield number, line


def parse_expiry(token: str) -> datetime.date | str:
    """The date of an ``exp:YYYY-MM-DD`` token, or the reason it is not one."""
    match = _EXPIRY.match(token)
    if match is None:
        return f"{token!r} is not an expiry (exp:YYYY-MM-DD)"
    try:
        return datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return f"{token!r} is not a real date"


def is_tracking_ref(token: str) -> bool:
    """Whether the token is ``#N``, ``owner/repo#N`` or a GitHub issue URL."""
    return _TRACKING.match(token) is not None


def window_problem(
    *,
    key: str,
    expires: datetime.date,
    tracking: str,
    today: datetime.date,
    max_days: int,
) -> str | None:
    """Why an entry's expiry is outside its window, or ``None`` when it is live.

    An entry is live through its expiry date and may expire at most
    ``max_days`` days after ``today``. ``key`` names the entry in the message.
    """
    if expires < today:
        return (
            f"the exception for {key} expired on {expires.isoformat()} (tracked in "
            f"{tracking}): fix the dependency, or review the risk again and set a "
            "new expiry"
        )
    latest = today + datetime.timedelta(days=max_days)
    if expires > latest:
        return (
            f"the exception for {key} expires on {expires.isoformat()}, more than "
            f"{max_days} days ahead (latest allowed: {latest.isoformat()})"
        )
    return None
