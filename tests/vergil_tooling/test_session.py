from __future__ import annotations

from vergil_tooling.lib.session import (
    SLOT_MAX,
    AgeBand,
    Create,
    Fork,
    PromptStale,
    Refuse,
    Resume,
    SessionPlan,
    SessionRow,
    Slot,
    build_slots,
    classify_age,
    list_rows,
    make_archived_name,
    make_name,
    parse_archived,
    parse_name,
    plan_session,
    select,
    select_by_name,
)


def test_make_archived_name() -> None:
    assert (
        make_archived_name("vergil:01:a/b", "2026-05-30T14:23:07Z")
        == "archived@2026-05-30T14:23:07Z@vergil:01:a/b"
    )


def test_parse_name_rejects_archived_prefix() -> None:
    assert parse_name("archived@2026-05-30T14:23:07Z@vergil:01:a/b") is None


def test_parse_archived_roundtrip() -> None:
    label = "archived@2026-05-30T14:23:07Z@vergil:01:a/b"
    assert parse_archived(label) == ("2026-05-30T14:23:07Z", "vergil:01:a/b")


def test_parse_archived_path_with_at_sign() -> None:
    label = "archived@2026-05-30T14:23:07Z@vergil:01:clients/acme@2024"
    assert parse_archived(label) == ("2026-05-30T14:23:07Z", "vergil:01:clients/acme@2024")


def test_parse_archived_returns_none_for_non_archived() -> None:
    assert parse_archived("vergil:01:a/b") is None
    assert parse_archived("archived@only-two-parts") is None


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


# --- default selection (no --slot, no --fork) ---


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


def test_explicit_refuses_active_slot_with_fork_hint() -> None:
    slots = {3: Slot(3, "sid-3", active=True)}
    result = select("vergil", "p", slots, requested_slot=3)
    assert isinstance(result, Refuse)
    assert "--fork" in result.message


def test_explicit_rejects_out_of_range_slot() -> None:
    assert isinstance(select("vergil", "p", {}, requested_slot=0), Refuse)
    assert isinstance(select("vergil", "p", {}, requested_slot=100), Refuse)


# --- --fork ---


def test_fork_requires_slot() -> None:
    result = select("vergil", "p", {}, fork=True)
    assert isinstance(result, Refuse)
    assert "--slot" in result.message


def test_fork_rejects_out_of_range_slot() -> None:
    assert isinstance(select("vergil", "p", {}, requested_slot=0, fork=True), Refuse)


def test_fork_refuses_nonexistent_slot() -> None:
    result = select("vergil", "p", {}, requested_slot=2, fork=True)
    assert isinstance(result, Refuse)
    assert "does not exist" in result.message


def test_fork_branches_active_slot_into_next_free() -> None:
    slots = {1: Slot(1, "sid-1", active=True)}
    assert select("vergil", "p", slots, requested_slot=1, fork=True) == Fork("sid-1", "vergil:02:p")


def test_fork_refuses_when_all_slots_in_use() -> None:
    slots = {n: Slot(n, f"sid-{n}", active=True) for n in range(1, SLOT_MAX + 1)}
    result = select("vergil", "p", slots, requested_slot=1, fork=True)
    assert isinstance(result, Refuse)
    assert "all 99 slots" in result.message


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


def test_classify_age_fresh() -> None:
    assert classify_age(100 * DAY, 99 * DAY, 7, 14) == AgeBand.FRESH


def test_classify_age_warn() -> None:
    assert classify_age(100 * DAY, 90 * DAY, 7, 14) == AgeBand.WARN


def test_classify_age_stale() -> None:
    assert classify_age(100 * DAY, 80 * DAY, 7, 14) == AgeBand.STALE


def test_classify_age_unknown_is_fresh() -> None:
    assert classify_age(100 * DAY, None, 7, 14) == AgeBand.FRESH


def test_classify_age_archive_zero_never_stale() -> None:
    assert classify_age(100 * DAY, 0.0, 7, 0) == AgeBand.WARN


NOW = 100 * DAY


def _slot(n: int, sid: str, active: bool = False, age_days: float = 0.0) -> Slot:
    return Slot(n, sid, active, NOW - age_days * DAY)


def _plan(slots: dict[int, Slot], **kw: object) -> SessionPlan:
    defaults: dict[str, object] = {
        "now": NOW,
        "stale_days": 7,
        "archive_days": 14,
        "requested_slot": None,
        "fork": False,
        "fresh": False,
    }
    defaults.update(kw)
    return plan_session("vergil", "p", slots, **defaults)  # type: ignore[arg-type]


def test_plan_resume_most_recent_idle() -> None:
    slots = {1: _slot(1, "old", age_days=3), 2: _slot(2, "new", age_days=0.1)}
    plan = _plan(slots)
    assert plan.auto_archive == []
    assert plan.action == Resume("new")


def test_plan_warn_band_prompts() -> None:
    plan = _plan({1: _slot(1, "s1", age_days=9)})
    assert plan.auto_archive == []
    assert plan.action == PromptStale("s1", "vergil:01:p", 9)


def test_plan_stale_is_swept_then_fresh() -> None:
    slots = {1: _slot(1, "s1", age_days=20)}
    plan = _plan(slots)
    assert plan.auto_archive == [slots[1]]
    assert plan.action == Create("vergil:01:p")


def test_plan_sweep_only_stale_keeps_fresh() -> None:
    slots = {1: _slot(1, "s1", age_days=20), 2: _slot(2, "s2", age_days=0.1)}
    plan = _plan(slots)
    assert plan.auto_archive == [slots[1]]
    assert plan.action == Resume("s2")


def test_plan_never_sweeps_active() -> None:
    plan = _plan({1: _slot(1, "s1", active=True, age_days=20)})
    assert plan.auto_archive == []
    assert plan.action == Create("vergil:02:p")


def test_plan_explicit_slot_no_sweep_no_prompt() -> None:
    slots = {1: _slot(1, "s1", age_days=20), 2: _slot(2, "s2", age_days=20)}
    plan = _plan(slots, requested_slot=1)
    assert plan.auto_archive == []
    assert plan.action == Resume("s1")


def test_plan_fresh_with_slot_archives_then_creates() -> None:
    slots = {1: _slot(1, "s1", age_days=1)}
    plan = _plan(slots, requested_slot=1, fresh=True)
    assert plan.auto_archive == [slots[1]]
    assert plan.action == Create("vergil:01:p")


def test_plan_fresh_no_slot_archives_most_recent_idle() -> None:
    slots = {1: _slot(1, "s1", age_days=5), 2: _slot(2, "s2", age_days=1)}
    plan = _plan(slots, fresh=True)
    assert plan.auto_archive == [slots[2]]
    assert plan.action == Create("vergil:02:p")


def test_plan_fresh_no_idle_creates_lowest_free() -> None:
    plan = _plan({}, fresh=True)
    assert plan.auto_archive == []
    assert plan.action == Create("vergil:01:p")


def test_plan_fresh_active_slot_refused() -> None:
    plan = _plan({1: _slot(1, "s1", active=True, age_days=1)}, requested_slot=1, fresh=True)
    assert plan.auto_archive == []
    assert isinstance(plan.action, Refuse)


def test_plan_fresh_bad_range_refused() -> None:
    plan = _plan({}, requested_slot=0, fresh=True)
    assert isinstance(plan.action, Refuse)


def test_plan_fresh_all_slots_in_use() -> None:
    slots = {n: _slot(n, f"s{n}", active=True, age_days=1) for n in range(1, SLOT_MAX + 1)}
    plan = _plan(slots, fresh=True)
    assert isinstance(plan.action, Refuse)


def test_plan_fork_unchanged() -> None:
    plan = _plan({1: _slot(1, "s1", active=True, age_days=1)}, requested_slot=1, fork=True)
    assert plan.auto_archive == []
    assert plan.action == Fork("s1", "vergil:02:p")


def test_plan_no_slots_creates_first() -> None:
    plan = _plan({})
    assert plan.action == Create("vergil:01:p")


def test_plan_all_active_creates_next_free() -> None:
    slots = {1: _slot(1, "s1", active=True), 2: _slot(2, "s2", active=True)}
    plan = _plan(slots)
    assert plan.action == Create("vergil:03:p")


def test_plan_all_slots_active_refused() -> None:
    slots = {n: _slot(n, f"s{n}", active=True) for n in range(1, SLOT_MAX + 1)}
    plan = _plan(slots)
    assert isinstance(plan.action, Refuse)


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
