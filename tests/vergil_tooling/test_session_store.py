from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.session_store import (
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
