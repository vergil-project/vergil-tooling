from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.session_store import (
    _RESERVATION_TTL_SECONDS,
    AmbiguousSessionError,
    ScrapeStore,
    SessionInfo,
    resolve_over,
)

if TYPE_CHECKING:
    from pathlib import Path


def _s(sid: str, name: str | None, active: bool, last: float | None) -> SessionInfo:
    return SessionInfo(sid, name, "/w", active, last)


# --- pure resolve_over ---


def test_resolve_picks_active_over_idle() -> None:
    rows = [_s("a", "epic-1:w", False, 10.0), _s("b", "epic-1:w", True, 5.0)]
    result = resolve_over(rows, "epic-1:w")
    assert result is not None
    assert result.session_id == "b"


def test_resolve_idle_by_most_recent() -> None:
    rows = [_s("a", "epic-1:w", False, 10.0), _s("b", "epic-1:w", False, 20.0)]
    result = resolve_over(rows, "epic-1:w")
    assert result is not None
    assert result.session_id == "b"


def test_resolve_none_when_absent() -> None:
    assert resolve_over([_s("a", "other:w", True, 1.0)], "epic-1:w") is None


def test_resolve_raises_on_two_active() -> None:
    rows = [_s("a", "epic-1:w", True, 10.0), _s("b", "epic-1:w", True, 20.0)]
    with pytest.raises(AmbiguousSessionError):
        resolve_over(rows, "epic-1:w")


def test_resolve_ignores_unnamed_rows() -> None:
    # A live-but-unnamed session (name is None) never matches a requested name.
    rows = [_s("a", None, True, 10.0), _s("b", "epic-1:w", False, 5.0)]
    result = resolve_over(rows, "epic-1:w")
    assert result is not None
    assert result.session_id == "b"


def test_resolve_idle_unknown_age_loses_to_known() -> None:
    rows = [_s("a", "epic-1:w", False, None), _s("b", "epic-1:w", False, 1.0)]
    result = resolve_over(rows, "epic-1:w")
    assert result is not None
    assert result.session_id == "b"


# --- ScrapeStore over the transcript/roster scrape ---


def _write_title(path: Path, sid: str, name: str) -> None:
    path.write_text(f'{{"type":"custom-title","customTitle":"{name}","sessionId":"{sid}"}}\n')


def test_scrape_store_lists_named_sessions(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    slug = claude / "projects" / "-w"
    slug.mkdir(parents=True)
    _write_title(slug / "s1.jsonl", "s1", "epic-1:w")
    (claude / "sessions").mkdir()

    rows = ScrapeStore(claude).list_sessions()
    by_id = {r.session_id: r for r in rows}
    assert by_id["s1"].name == "epic-1:w"
    assert by_id["s1"].active is False


def test_scrape_store_rename_then_list_reflects_new_name(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    slug = claude / "projects" / "-w"
    slug.mkdir(parents=True)
    _write_title(slug / "s1.jsonl", "s1", "epic-1:w")
    (claude / "sessions").mkdir()

    store = ScrapeStore(claude)
    store.rename("s1", "epic-1-renamed:w")

    names = {r.session_id: r.name for r in store.list_sessions()}
    assert names["s1"] == "epic-1-renamed:w"


def test_scrape_store_surfaces_roster_cwd_and_active(tmp_path: Path) -> None:
    # A live session named only in the roster (no transcript yet) is surfaced
    # active, with the cwd the roster records.
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    sessions = claude / "sessions"
    sessions.mkdir()
    (sessions / "100.json").write_text(
        '{"pid":100,"sessionId":"s1","name":"epic-9:w","cwd":"/work/repo",'
        '"updatedAt":1748000000000}'
    )

    import vergil_tooling.bin.vrg_vm_resolve as res

    monkey = pytest.MonkeyPatch()
    monkey.setattr(res, "_is_live", lambda _pid, _ps: True)
    try:
        rows = {r.session_id: r for r in ScrapeStore(claude).list_sessions()}
    finally:
        monkey.undo()
    assert rows["s1"].name == "epic-9:w"
    assert rows["s1"].cwd == "/work/repo"
    assert rows["s1"].active is True
    assert rows["s1"].last_active == 1748000000.0


def test_scrape_store_derives_idle_cwd_from_transcript(tmp_path: Path) -> None:
    # An idle session (no live roster entry) still reports its cwd, read from the
    # transcript, so --resume can derive that session's memory slug (#2607).
    claude = tmp_path / ".claude"
    slug = claude / "projects" / "-work-repo"
    slug.mkdir(parents=True)
    (slug / "s1.jsonl").write_text(
        '{"type":"user","cwd":"/work/repo","timestamp":"2026-05-02T00:00:00.000Z"}\n'
        '{"type":"custom-title","customTitle":"epic-1:w","sessionId":"s1"}\n'
    )
    (claude / "sessions").mkdir()

    rows = {r.session_id: r for r in ScrapeStore(claude).list_sessions()}
    assert rows["s1"].name == "epic-1:w"
    assert rows["s1"].active is False
    assert rows["s1"].cwd == "/work/repo"


def test_scrape_store_resolve_name_uses_resolve_over(tmp_path: Path) -> None:
    claude = tmp_path / ".claude"
    slug = claude / "projects" / "-w"
    slug.mkdir(parents=True)
    _write_title(slug / "s1.jsonl", "s1", "epic-1:w")
    (claude / "sessions").mkdir()

    store = ScrapeStore(claude)
    resolved = store.resolve_name("epic-1:w")
    assert resolved is not None
    assert resolved.session_id == "s1"
    assert store.resolve_name("nope:w") is None


# --- reserve_name: the eager name source that closes the --label race (#2654) ---


def _write_reservation(claude: Path, sid: str, name: str, created: float) -> None:
    """Hand-write a reservation file with a chosen createdAt (for TTL tests)."""
    reservations = claude / "vrg-session-reservations"
    reservations.mkdir(parents=True, exist_ok=True)
    (reservations / f"{sid}.json").write_text(
        json.dumps({"sessionId": sid, "name": name, "cwd": "/work/repo", "createdAt": created})
        + "\n"
    )


def test_reserved_name_resolves_before_transcript_or_roster(tmp_path: Path) -> None:
    # The core regression: a session that exists only as a reservation (claude has
    # not yet written its transcript custom-title or roster name) is still resolved,
    # so a rapid second --label sees the collision.
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "sessions").mkdir()

    store = ScrapeStore(claude)
    store.reserve_name("s1", "epic-1:w", "/work/repo")

    resolved = store.resolve_name("epic-1:w")
    assert resolved is not None
    assert resolved.session_id == "s1"
    assert resolved.active is False  # a reservation is never counted as live
    assert resolved.cwd == "/work/repo"  # recorded cwd stands in until registration


def test_reservation_only_session_is_not_materialized(tmp_path: Path) -> None:
    # #2669: a reservation-only session (claude has written no transcript) resolves
    # by name but is reported materialized=False, so the resume path re-enters it at
    # its id instead of `claude --resume`ing an id with no conversation.
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "sessions").mkdir()

    store = ScrapeStore(claude)
    store.reserve_name("s1", "adhoc-recheck:w", "/work/repo")

    resolved = store.resolve_name("adhoc-recheck:w")
    assert resolved is not None
    assert resolved.session_id == "s1"
    assert resolved.materialized is False


def test_session_with_transcript_is_materialized(tmp_path: Path) -> None:
    # The counterpart: a session claude has written a transcript for is reported
    # materialized=True, so the resume path uses `claude --resume <id>` (#2669).
    claude = tmp_path / ".claude"
    slug = claude / "projects" / "-w"
    slug.mkdir(parents=True)
    (claude / "sessions").mkdir()
    _write_title(slug / "s1.jsonl", "s1", "epic-1:w")

    resolved = ScrapeStore(claude).resolve_name("epic-1:w")
    assert resolved is not None
    assert resolved.materialized is True


def test_reservation_and_real_transcript_dedup_to_one_row(tmp_path: Path) -> None:
    # Because the reservation is keyed by the id claude adopts (--session-id), once
    # the real transcript appears the two are the SAME row — one session, the
    # authoritative transcript name winning — never a phantom duplicate.
    claude = tmp_path / ".claude"
    slug = claude / "projects" / "-w"
    slug.mkdir(parents=True)
    (claude / "sessions").mkdir()

    store = ScrapeStore(claude)
    store.reserve_name("s1", "epic-1:w", "/work/repo")
    _write_title(slug / "s1.jsonl", "s1", "epic-1:w")  # claude has now registered

    rows = [row for row in store.list_sessions() if row.session_id == "s1"]
    assert len(rows) == 1
    assert rows[0].name == "epic-1:w"


def test_reservation_swept_once_transcript_exists(tmp_path: Path) -> None:
    # A reservation is redundant once its transcript exists; the next reserve_name
    # sweeps it, so the directory never accumulates superseded reservations.
    claude = tmp_path / ".claude"
    slug = claude / "projects" / "-w"
    slug.mkdir(parents=True)
    (claude / "sessions").mkdir()

    store = ScrapeStore(claude)
    store.reserve_name("s1", "epic-1:w", "/work/repo")
    _write_title(slug / "s1.jsonl", "s1", "epic-1:w")
    store.reserve_name("s2", "epic-2:w", "/work/repo")  # triggers the sweep

    reservations = claude / "vrg-session-reservations"
    assert not (reservations / "s1.json").exists()  # superseded -> swept
    assert (reservations / "s2.json").exists()  # still bridging -> kept


def test_expired_reservation_is_ignored(tmp_path: Path) -> None:
    # A stale reservation whose session never materialized cannot pin a name
    # forever: past the TTL it is ignored by the read.
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "sessions").mkdir()
    _write_reservation(claude, "s1", "epic-1:w", created=0.0)  # epoch 0 -> long expired

    store = ScrapeStore(claude)
    assert store.resolve_name("epic-1:w") is None


def test_fresh_reservation_within_ttl_is_honored(tmp_path: Path) -> None:
    import time

    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "sessions").mkdir()
    _write_reservation(claude, "s1", "epic-1:w", created=time.time() - _RESERVATION_TTL_SECONDS / 2)

    store = ScrapeStore(claude)
    resolved = store.resolve_name("epic-1:w")
    assert resolved is not None
    assert resolved.session_id == "s1"


def test_two_reservations_same_name_do_not_falsely_conflict_as_live(tmp_path: Path) -> None:
    # Reservations are idle rows, so two of them holding one name resolve to a
    # single session (still a collision) rather than raising the fail-loud
    # two-live-sessions error that is reserved for genuinely live duplicates.
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "sessions").mkdir()

    store = ScrapeStore(claude)
    store.reserve_name("s1", "epic-1:w", "/work/repo")
    store.reserve_name("s2", "epic-1:w", "/work/repo")

    resolved = store.resolve_name("epic-1:w")  # must not raise AmbiguousSessionError
    assert resolved is not None
    assert resolved.session_id in {"s1", "s2"}


def test_malformed_reservations_are_ignored_and_unreadable_ones_swept(tmp_path: Path) -> None:
    # The reservation reader must be robust to junk in its own directory: unreadable
    # JSON, a valid-but-non-dict payload, and a dict missing the required fields all
    # contribute no rows. A later reserve sweeps the unreadable file (pure cruft);
    # the odd-but-parseable ones are harmless and left to age out.
    claude = tmp_path / ".claude"
    (claude / "projects").mkdir(parents=True)
    (claude / "sessions").mkdir()
    reservations = claude / "vrg-session-reservations"
    reservations.mkdir()
    (reservations / "bad.json").write_text("{ not json")
    (reservations / "list.json").write_text("[1, 2, 3]")
    (reservations / "nofields.json").write_text('{"sessionId": 5}')

    store = ScrapeStore(claude)
    assert store.list_sessions() == []  # nothing usable -> no rows
    assert store.resolve_name("epic-1:w") is None

    store.reserve_name("s1", "epic-1:w", "/w")  # triggers the sweep
    assert not (reservations / "bad.json").exists()  # unreadable -> swept
    assert (reservations / "s1.json").exists()
