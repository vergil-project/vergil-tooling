"""The release lock's record, its errors and its timing constants.

Spec §5.2 of epic vergil-project/.github#1664: the lock ref
``refs/vergil/release-lock`` points at a one-file commit whose file is the
JSON form of a :class:`LockRecord`. The timing constants live here and
nowhere else, so every part of the lock uses the same values.
"""

from __future__ import annotations

import dataclasses
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args

#: How often the holder's background writer refreshes the heartbeat.
HEARTBEAT_INTERVAL_SECS = 60
#: A heartbeat older than this, on a lock with no open tracking issue, is stale.
STALE_AFTER_SECS = 900
#: An arming ref the release has seen unchanged this long has expired.
ARMING_EXPIRY_SECS = 300
#: The bound on the arm call, retries included; well below the arming expiry.
ARM_CALL_BOUND_SECS = 60
#: The release's total wait on arming refs before it fails, naming them.
ARMING_WAIT_BOUND_SECS = 900
#: The watch's poll interval while the lock is held by a broken release.
BROKEN_WATCH_POLL_SECS = 300

Health = Literal["running", "broken"]
_HEALTH: tuple[str, ...] = get_args(Health)

#: ``<owner>/<repo>#<n>``, the form of a tracking issue ref.
_ISSUE_REF = re.compile(r"[\w.-]+/[\w.-]+#[1-9][0-9]*")


class ReleaseLockError(Exception):
    """A release-lock ref could not be read or written."""


class RefReadError(ReleaseLockError):
    """A release-lock ref could not be read: whether it is present cannot be told."""


class RefWriteError(ReleaseLockError):
    """A release-lock ref write failed for any reason but a lease mismatch."""


class LockParseError(ValueError):
    """A lock's JSON text is not a valid :class:`LockRecord`."""


def _require_utc(name: str, value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        msg = f"{name} is not an ISO 8601 timestamp: {value!r}"
        raise ValueError(msg) from None
    if parsed.utcoffset() != timedelta(0):
        msg = f"{name} is not in UTC: {value!r}"
        raise ValueError(msg)


@dataclass(frozen=True)
class LockRecord:
    """The lock's contents: who holds it, for which release, and how it is going."""

    holder_identity: str
    holder_host: str
    version: str
    worktree: str  # "" until preflight creates it
    tracking_issue: str  # "" until prepare creates it; "<owner>/<repo>#<n>"
    stage: str
    health: Health
    broken_stage: str  # "" unless health == "broken"
    started_at: str  # ISO 8601 UTC
    heartbeat_at: str
    token: str  # random, written at take, adopt, retake and takeover

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            if not isinstance(getattr(self, field.name), str):
                msg = f"{field.name} is not a string"
                raise ValueError(msg)  # noqa: TRY004 - a malformed record, reported as one
        for name in ("holder_identity", "holder_host", "version", "stage", "token"):
            if not getattr(self, name):
                msg = f"{name} is empty"
                raise ValueError(msg)
        if self.health not in _HEALTH:
            msg = f"health is {self.health!r}, not one of {', '.join(_HEALTH)}"
            raise ValueError(msg)
        if (self.health == "broken") != bool(self.broken_stage):
            msg = "broken_stage is set exactly when health is broken"
            raise ValueError(msg)
        if self.tracking_issue and not _ISSUE_REF.fullmatch(self.tracking_issue):
            msg = f"tracking_issue is not <owner>/<repo>#<n>: {self.tracking_issue!r}"
            raise ValueError(msg)
        _require_utc("started_at", self.started_at)
        _require_utc("heartbeat_at", self.heartbeat_at)


_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(LockRecord))


def to_json(rec: LockRecord) -> str:
    """The lock file's text for *rec*."""
    return json.dumps(dataclasses.asdict(rec), indent=2, sort_keys=True) + "\n"


def from_json(text: str) -> LockRecord:
    """Parse a lock file's text, raising :class:`LockParseError` naming what is wrong."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"the lock is not valid JSON: {exc}"
        raise LockParseError(msg) from None
    if not isinstance(data, dict):
        msg = "the lock is not a JSON object"
        raise LockParseError(msg)
    missing = sorted(_FIELDS - data.keys())
    if missing:
        msg = f"the lock is missing {', '.join(missing)}"
        raise LockParseError(msg)
    unknown = sorted(data.keys() - _FIELDS)
    if unknown:
        msg = f"the lock has unknown fields {', '.join(unknown)}"
        raise LockParseError(msg)
    try:
        return LockRecord(**data)
    except ValueError as exc:
        msg = f"the lock is not valid: {exc}"
        raise LockParseError(msg) from None


def new_token() -> str:
    """A fresh random token for a lock record."""
    return secrets.token_hex(16)


def utc_now() -> str:
    """The current time as an ISO 8601 UTC timestamp, to the second."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
