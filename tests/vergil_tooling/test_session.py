from __future__ import annotations

import pytest

from vergil_tooling.lib.session import (
    SLOT_MAX,
    Create,
    Refuse,
    Resume,
    SessionRow,
    Slot,
    build_slots,
    filter_recent,
    list_rows,
    make_label_name,
    make_name,
    parse_name,
    plan_session,
    select,
    select_by_name,
    validate_label,
)
from vergil_tooling.lib.session_store import SessionInfo


def test_parse_name_rejects_archived_prefix() -> None:
    # A legacy archived@ name is opaque — never a phantom slot (its timestamp
    # colons would otherwise misparse as <id>:<NN>:<path>).
    assert parse_name("archived@2026-05-30T14:23:07Z@vergil:01:a/b") is None


# --- make_label_name / validate_label (issue #2606) ---


def test_make_label_name_composes() -> None:
    assert (
        make_label_name("epic-213-x", "vergil-project/vergil-tooling")
        == "epic-213-x:vergil-project/vergil-tooling"
    )


def test_make_label_name_drops_identity() -> None:
    # The name is purpose:workspace only — no identity segment.
    assert make_label_name("adhoc-spike", ".") == "adhoc-spike:."


def test_validate_label_clean_epic() -> None:
    assert validate_label("epic-213-x") == []


def test_validate_label_clean_adhoc() -> None:
    assert validate_label("adhoc-spike") == []


def test_validate_label_warns_non_convention() -> None:
    warnings = validate_label("scratch")
    # Non-empty, and every warning names the epic-/adhoc- convention.
    assert warnings
    assert all("epic-" in w or "adhoc-" in w for w in warnings)


def test_validate_label_rejects_colon() -> None:
    with pytest.raises(ValueError, match="':'"):
        validate_label("bad:name")


def test_validate_label_rejects_whitespace() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        validate_label("bad name")


def test_validate_label_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_label("")
    with pytest.raises(ValueError, match="empty"):
        validate_label("   ")


def test_make_name_zero_pads_slot() -> None:
    assert make_name("vergil", 1, "a/b") == "vergil:01:a/b"
    assert make_name("vergil", 12, "a/b") == "vergil:12:a/b"


def test_parse_name_roundtrip() -> None:
    assert parse_name("vergil:01:vergil-project/vergil-vm") == (
        "vergil",
        1,
        "vergil-project/vergil-vm",
    )


def test_parse_name_preserves_path_with_colons_only_in_fields() -> None:
    # split(":", 2) keeps everything after the second colon as the path
    assert parse_name("id:02:a/b/c") == ("id", 2, "a/b/c")


def test_parse_name_rejects_wrong_field_count() -> None:
    assert parse_name("only:one") is None
    assert parse_name("noseparators") is None


def test_parse_name_rejects_empty_identity_or_path() -> None:
    assert parse_name(":01:path") is None
    assert parse_name("id:01:") is None


def test_parse_name_rejects_non_two_digit_slot() -> None:
    assert parse_name("id:1:path") is None  # not zero-padded
    assert parse_name("id:001:path") is None  # too long
    assert parse_name("id:xx:path") is None  # not digits


def test_parse_name_rejects_out_of_range_slot() -> None:
    assert parse_name("id:00:path") is None


# --- default selection (no --slot) ---


def test_default_creates_first_slot_when_none_exist() -> None:
    assert select("vergil", "p", {}) == Create("vergil:01:p")


def test_default_resumes_lowest_idle_slot() -> None:
    slots = {
        1: Slot(1, "sid-1", active=True),
        2: Slot(2, "sid-2", active=False),
        3: Slot(3, "sid-3", active=False),
    }
    assert select("vergil", "p", slots) == Resume("sid-2")


def test_default_creates_lowest_free_when_all_active() -> None:
    slots = {1: Slot(1, "sid-1", active=True)}
    assert select("vergil", "p", slots) == Create("vergil:02:p")


def test_default_refuses_when_all_slots_active() -> None:
    slots = {n: Slot(n, f"sid-{n}", active=True) for n in range(1, SLOT_MAX + 1)}
    result = select("vergil", "p", slots)
    assert isinstance(result, Refuse)
    assert "all 99 slots" in result.message


# --- explicit --slot N ---


def test_explicit_creates_nonexistent_slot() -> None:
    assert select("vergil", "p", {}, requested_slot=5) == Create("vergil:05:p")


def test_explicit_resumes_idle_slot() -> None:
    slots = {3: Slot(3, "sid-3", active=False)}
    assert select("vergil", "p", slots, requested_slot=3) == Resume("sid-3")


def test_explicit_refuses_active_slot() -> None:
    slots = {3: Slot(3, "sid-3", active=True)}
    result = select("vergil", "p", slots, requested_slot=3)
    assert isinstance(result, Refuse)
    assert "active" in result.message


def test_explicit_rejects_out_of_range_slot() -> None:
    assert isinstance(select("vergil", "p", {}, requested_slot=0), Refuse)
    assert isinstance(select("vergil", "p", {}, requested_slot=100), Refuse)


# --- build_slots ---


def test_build_slots_filters_by_identity_and_path() -> None:
    name_by_session = {
        "s1": "vergil:01:p",
        "s2": "vergil:02:other",  # wrong path
        "s3": "admin:01:p",  # wrong identity
        "s4": "not a session name",  # unparseable
    }
    slots = build_slots("vergil", "p", name_by_session, active_sessions=set())
    assert set(slots) == {1}
    assert slots[1] == Slot(1, "s1", active=False)


def test_build_slots_marks_active_from_roster() -> None:
    name_by_session = {"s1": "vergil:01:p", "s2": "vergil:02:p"}
    slots = build_slots("vergil", "p", name_by_session, active_sessions={"s1"})
    assert slots[1].active is True
    assert slots[2].active is False


def test_build_slots_active_wins_slot_collision() -> None:
    # two session ids claim slot 01; the active one must win
    name_by_session = {"idle": "vergil:01:p", "live": "vergil:01:p"}
    slots = build_slots("vergil", "p", name_by_session, active_sessions={"live"})
    assert slots[1] == Slot(1, "live", active=True)


def test_build_slots_keeps_first_when_no_active_collision() -> None:
    name_by_session = {"a": "vergil:01:p"}
    slots = build_slots("vergil", "p", name_by_session, active_sessions=set())
    assert slots[1].session_id == "a"


# --- list_rows ---


def test_list_rows_sorted_and_classified() -> None:
    name_by_session = {
        "s2": "vergil:02:tooling",
        "s1": "vergil:01:vm",
        "s3": "admin:01:actions",
        "bad": "garbage",
    }
    rows = list_rows(name_by_session, active_sessions={"s1"})
    assert rows == [
        SessionRow("admin", 1, "actions", "s3", active=False),
        SessionRow("vergil", 1, "vm", "s1", active=True),
        SessionRow("vergil", 2, "tooling", "s2", active=False),
    ]


def test_list_rows_active_wins_duplicate() -> None:
    name_by_session = {"idle": "vergil:01:p", "live": "vergil:01:p"}
    rows = list_rows(name_by_session, active_sessions={"live"})
    assert rows == [SessionRow("vergil", 1, "p", "live", active=True)]


def test_build_slots_keeps_active_when_idle_follows() -> None:
    # active seen first, idle duplicate second -> keep the active one
    name_by_session = {"live": "vergil:01:p", "idle": "vergil:01:p"}
    slots = build_slots("vergil", "p", name_by_session, active_sessions={"live"})
    assert slots[1] == Slot(1, "live", active=True)


def test_list_rows_keeps_active_when_idle_follows() -> None:
    name_by_session = {"live": "vergil:01:p", "idle": "vergil:01:p"}
    rows = list_rows(name_by_session, active_sessions={"live"})
    assert rows == [SessionRow("vergil", 1, "p", "live", active=True)]


def test_build_slots_attaches_last_active() -> None:
    slots = build_slots(
        "vergil", "p", {"s1": "vergil:01:p"}, active_sessions=set(), last_active={"s1": 1000.0}
    )
    assert slots[1].last_active == 1000.0


def test_build_slots_last_active_defaults_none() -> None:
    slots = build_slots("vergil", "p", {"s1": "vergil:01:p"}, active_sessions=set())
    assert slots[1].last_active is None


def test_list_rows_attaches_last_active() -> None:
    rows = list_rows({"s1": "vergil:01:p"}, active_sessions=set(), last_active={"s1": 5.0})
    assert rows[0].last_active == 5.0


# --- recency-aware slot collisions (issue #1493) ---


def test_build_slots_recent_idle_wins_collision() -> None:
    # /clear rotates the session id, so both the abandoned and the current id
    # claim the slot. With neither live, the most recently active must win.
    name_by_session = {"old": "vergil:01:p", "new": "vergil:01:p"}
    slots = build_slots(
        "vergil",
        "p",
        name_by_session,
        active_sessions=set(),
        last_active={"old": 1000.0, "new": 2000.0},
    )
    assert slots[1].session_id == "new"


def test_build_slots_recent_idle_wins_collision_either_order() -> None:
    name_by_session = {"new": "vergil:01:p", "old": "vergil:01:p"}
    slots = build_slots(
        "vergil",
        "p",
        name_by_session,
        active_sessions=set(),
        last_active={"old": 1000.0, "new": 2000.0},
    )
    assert slots[1].session_id == "new"


def test_build_slots_known_age_beats_unknown_in_collision() -> None:
    name_by_session = {"unknown": "vergil:01:p", "known": "vergil:01:p"}
    slots = build_slots(
        "vergil",
        "p",
        name_by_session,
        active_sessions=set(),
        last_active={"known": 1000.0},
    )
    assert slots[1].session_id == "known"


def test_build_slots_unknown_age_keeps_incumbent_in_collision() -> None:
    name_by_session = {"known": "vergil:01:p", "unknown": "vergil:01:p"}
    slots = build_slots(
        "vergil",
        "p",
        name_by_session,
        active_sessions=set(),
        last_active={"known": 1000.0},
    )
    assert slots[1].session_id == "known"


def test_build_slots_active_beats_recent_idle() -> None:
    # Liveness still dominates recency.
    name_by_session = {"idle": "vergil:01:p", "live": "vergil:01:p"}
    slots = build_slots(
        "vergil",
        "p",
        name_by_session,
        active_sessions={"live"},
        last_active={"idle": 9999.0, "live": 1.0},
    )
    assert slots[1] == Slot(1, "live", active=True, last_active=1.0)


def test_list_rows_recent_idle_wins_duplicate() -> None:
    rows = list_rows(
        {"old": "vergil:01:p", "new": "vergil:01:p"},
        active_sessions=set(),
        last_active={"old": 1000.0, "new": 2000.0},
    )
    assert rows == [SessionRow("vergil", 1, "p", "new", active=False, last_active=2000.0)]


DAY = 86400.0
NOW = 100 * DAY


# --- filter_recent (display recency band, issue #2608) ---


def _si(sid: str, active: bool, last: float | None) -> SessionInfo:
    return SessionInfo(sid, f"name-{sid}", "/w", active, last)


def test_filter_recent_keeps_recent_idle_drops_old() -> None:
    rows = [_si("a", False, NOW - 2 * DAY), _si("b", False, NOW - 20 * DAY)]
    out = filter_recent(rows, NOW, 7)
    assert [r.session_id for r in out] == ["a"]


def test_filter_recent_all_returns_everything() -> None:
    rows = [_si("a", False, NOW - 20 * DAY), _si("b", False, NOW - 99 * DAY)]
    assert filter_recent(rows, NOW, 7, all=True) == rows


def test_filter_recent_keeps_active_regardless_of_age() -> None:
    rows = [_si("a", True, NOW - 99 * DAY)]
    assert filter_recent(rows, NOW, 7) == rows


def test_filter_recent_keeps_unknown_age() -> None:
    rows = [_si("a", False, None)]
    assert filter_recent(rows, NOW, 7) == rows


def test_filter_recent_boundary_is_inclusive() -> None:
    # Exactly recent_days old is still within the window (>= cutoff).
    rows = [_si("a", False, NOW - 7 * DAY)]
    assert filter_recent(rows, NOW, 7) == rows


# --- plan_session (legacy --fresh slot machinery; archive removed) ---


def _slot(n: int, sid: str, active: bool = False, age_days: float = 0.0) -> Slot:
    return Slot(n, sid, active, NOW - age_days * DAY)


def _plan(slots: dict[int, Slot], **kw: object) -> object:
    defaults: dict[str, object] = {
        "requested_slot": None,
        "fresh": False,
    }
    defaults.update(kw)
    return plan_session("vergil", "p", slots, **defaults)  # type: ignore[arg-type]


def test_plan_default_resumes_lowest_idle() -> None:
    slots = {1: _slot(1, "s1", active=True), 2: _slot(2, "s2"), 3: _slot(3, "s3")}
    assert _plan(slots) == Resume("s2")


def test_plan_explicit_slot_resumes() -> None:
    slots = {1: _slot(1, "s1"), 2: _slot(2, "s2")}
    assert _plan(slots, requested_slot=1) == Resume("s1")


def test_plan_fresh_with_slot_creates_no_archive() -> None:
    # --fresh no longer archives (issue #2608): it just creates in the slot.
    slots = {1: _slot(1, "s1", age_days=1)}
    assert _plan(slots, requested_slot=1, fresh=True) == Create("vergil:01:p")


def test_plan_fresh_no_slot_picks_most_recent_idle_slot() -> None:
    slots = {1: _slot(1, "s1", age_days=5), 2: _slot(2, "s2", age_days=1)}
    assert _plan(slots, fresh=True) == Create("vergil:02:p")


def test_plan_fresh_no_idle_creates_lowest_free() -> None:
    assert _plan({}, fresh=True) == Create("vergil:01:p")


def test_plan_fresh_active_slot_refused() -> None:
    plan = _plan({1: _slot(1, "s1", active=True, age_days=1)}, requested_slot=1, fresh=True)
    assert isinstance(plan, Refuse)


def test_plan_fresh_bad_range_refused() -> None:
    assert isinstance(_plan({}, requested_slot=0, fresh=True), Refuse)


def test_plan_fresh_all_slots_in_use() -> None:
    slots = {n: _slot(n, f"s{n}", active=True, age_days=1) for n in range(1, SLOT_MAX + 1)}
    assert isinstance(_plan(slots, fresh=True), Refuse)


def test_plan_no_slots_creates_first() -> None:
    assert _plan({}) == Create("vergil:01:p")


def test_plan_all_active_creates_next_free() -> None:
    slots = {1: _slot(1, "s1", active=True), 2: _slot(2, "s2", active=True)}
    assert _plan(slots) == Create("vergil:03:p")


def test_plan_all_slots_active_refused() -> None:
    slots = {n: _slot(n, f"s{n}", active=True) for n in range(1, SLOT_MAX + 1)}
    assert isinstance(_plan(slots), Refuse)


# --- select_by_name (resume a session by its exact display name) ---


def test_select_by_name_resumes_exact_match() -> None:
    names = {"s1": "epic-85-centralize-epics-adhoc", "s2": "vergil:01:p"}
    assert select_by_name("epic-85-centralize-epics-adhoc", names, set()) == Resume("s1")


def test_select_by_name_refuses_when_no_match() -> None:
    result = select_by_name("no-such-session", {"s1": "vergil:01:p"}, set())
    assert isinstance(result, Refuse)
    assert "no-such-session" in result.message


def test_select_by_name_live_beats_dead_on_collision() -> None:
    # A /clear rotation leaves the abandoned id still carrying the title; the
    # live claimant of the same name wins.
    names = {"dead": "epic-85", "live": "epic-85"}
    assert select_by_name("epic-85", names, {"live"}) == Resume("live")


def test_select_by_name_recency_breaks_tie_between_idle() -> None:
    names = {"old": "epic-85", "new": "epic-85"}
    last_active = {"old": 100.0, "new": 200.0}
    assert select_by_name("epic-85", names, set(), last_active) == Resume("new")


def test_select_by_name_keeps_incumbent_when_later_does_not_displace() -> None:
    # The live incumbent is seen first; a later dead session sharing the name
    # must not displace it (the _displaces-returns-False path).
    names = {"live": "epic-85", "dead": "epic-85"}
    assert select_by_name("epic-85", names, {"live"}) == Resume("live")


def test_select_by_name_match_is_exact_not_substring() -> None:
    result = select_by_name("epic-85", {"s1": "epic-850-other"}, set())
    assert isinstance(result, Refuse)
