"""The release lock's record model and timing constants (epic vergil-project/.github#1664, Task 17)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from vergil_tooling.lib import release_lock
from vergil_tooling.lib.release_lock import model
from vergil_tooling.lib.release_lock.model import LockParseError, LockRecord, from_json, to_json


def _record(**changes: str) -> LockRecord:
    fields = {
        "holder_identity": "human",
        "holder_host": "mac.local",
        "version": "2.1.400",
        "worktree": "",
        "tracking_issue": "",
        "stage": "pre-check",
        "health": "running",
        "broken_stage": "",
        "started_at": "2026-11-27T10:00:00Z",
        "heartbeat_at": "2026-11-27T10:01:00Z",
        "token": "a1b2c3",
    }
    fields.update(changes)
    return LockRecord(**fields)  # type: ignore[arg-type]


def test_timing_constants() -> None:
    assert model.HEARTBEAT_INTERVAL_SECS == 60
    assert model.STALE_AFTER_SECS == 900
    assert model.ARMING_EXPIRY_SECS == 300
    assert model.ARM_CALL_BOUND_SECS == 60
    assert model.ARM_CALL_BOUND_SECS < model.ARMING_EXPIRY_SECS
    assert model.ARMING_WAIT_BOUND_SECS == 900
    assert model.BROKEN_WATCH_POLL_SECS == 300


def test_package_exports_the_errors() -> None:
    assert issubclass(release_lock.RefWriteError, release_lock.ReleaseLockError)
    assert issubclass(release_lock.RefReadError, release_lock.ReleaseLockError)
    assert release_lock.LockParseError is LockParseError


def test_model_round_trip() -> None:
    for rec in (
        _record(),
        _record(
            worktree="/w/.worktrees/release-2.1.400",
            tracking_issue="vergil-project/vergil-tooling#5300",
            stage="merge-release",
            health="broken",
            broken_stage="merge-release",
        ),
    ):
        text = to_json(rec)
        assert from_json(text) == rec
        assert json.loads(text)["health"] == rec.health


def test_model_rejects_unknown_health() -> None:
    text = to_json(_record()).replace('"running"', '"done"')
    with pytest.raises(LockParseError, match="health"):
        from_json(text)
    with pytest.raises(ValueError, match="health"):
        _record(health="done")


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"broken_stage": "cut"}, "broken_stage"),
        ({"health": "broken"}, "broken_stage"),
        ({"tracking_issue": "#12"}, "tracking_issue"),
        ({"started_at": "yesterday"}, "started_at"),
        ({"heartbeat_at": "2026-11-27T10:01:00+02:00"}, "heartbeat_at"),
        ({"heartbeat_at": "2026-11-27T10:01:00"}, "heartbeat_at"),
        ({"token": ""}, "token"),
        ({"version": ""}, "version"),
    ],
)
def test_record_rejects_inconsistent_fields(changes: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _record(**changes)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
        ("null", "not a JSON object"),
    ],
)
def test_from_json_rejects_malformed_text(text: str, match: str) -> None:
    with pytest.raises(LockParseError, match=match):
        from_json(text)


def test_from_json_rejects_missing_unknown_and_non_string_fields() -> None:
    data = dataclasses.asdict(_record())
    missing = {k: v for k, v in data.items() if k != "token"}
    with pytest.raises(LockParseError, match="missing.*token"):
        from_json(json.dumps(missing))
    with pytest.raises(LockParseError, match="unknown.*extra"):
        from_json(json.dumps({**data, "extra": "x"}))
    with pytest.raises(LockParseError, match="stage"):
        from_json(json.dumps({**data, "stage": 3}))


def test_new_token_is_random_hex() -> None:
    first, second = model.new_token(), model.new_token()
    assert first != second
    assert len(first) == 32
    int(first, 16)


def test_utc_now_is_a_valid_timestamp() -> None:
    assert _record(heartbeat_at=model.utc_now()).heartbeat_at.endswith("Z")
