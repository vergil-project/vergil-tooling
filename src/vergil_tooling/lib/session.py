"""Deterministic Claude Code session naming and slot selection.

Pure logic: given the existing slots for an identity + workspace path and the
caller's ``--slot`` / ``--fork`` choices, decide whether to create, resume, or
fork a session — or refuse. No I/O lives here, so every rule is unit-testable.

Naming scheme::

    <identity>:<slot>:<workspace-relative-path>

The colon delimiter is unambiguous: colons appear in neither identity names nor
workspace paths, even though dashes and slashes are common inside both fields.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

SLOT_MIN = 1
SLOT_MAX = 99

_ARCHIVED_PREFIX = "archived@"


def make_name(identity: str, slot: int, path: str) -> str:
    """Build a session name ``<identity>:<NN>:<path>`` (slot zero-padded)."""
    return f"{identity}:{slot:02d}:{path}"


def make_archived_name(name: str, timestamp: str) -> str:
    """Archived label: ``archived@<timestamp>@<original-name>``."""
    return f"{_ARCHIVED_PREFIX}{timestamp}@{name}"


def parse_archived(name: str) -> tuple[str, str] | None:
    """Parse an archived label into ``(timestamp, original_name)`` or ``None``.

    Splits on the first two ``@`` so a workspace path containing ``@`` is safe.
    """
    if not name.startswith(_ARCHIVED_PREFIX):
        return None
    parts = name.split("@", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def parse_name(name: str) -> tuple[str, int, str] | None:
    """Parse ``<identity>:<NN>:<path>`` into its fields.

    Returns ``None`` for any string that does not match the scheme (an archived
    label, wrong field count, empty identity/path, or a slot that is not a
    two-digit number in range).
    """
    if name.startswith(_ARCHIVED_PREFIX):
        return None
    parts = name.split(":", 2)
    if len(parts) != 3:
        return None
    identity, slot_str, path = parts
    if not identity or not path:
        return None
    if len(slot_str) != 2 or not slot_str.isdigit():
        return None
    slot = int(slot_str)
    if not SLOT_MIN <= slot <= SLOT_MAX:
        return None
    return identity, slot, path


@dataclass(frozen=True)
class Slot:
    """An existing session slot for a given identity + workspace path."""

    slot: int
    session_id: str
    active: bool  # a live Claude client is attached
    last_active: float | None = None  # epoch seconds; None when age is unknown


@dataclass(frozen=True)
class Create:
    """Create a new named session."""

    name: str


@dataclass(frozen=True)
class Resume:
    """Resume an existing session by its session id."""

    session_id: str


@dataclass(frozen=True)
class Fork:
    """Fork an existing session into a new named session."""

    session_id: str
    name: str


@dataclass(frozen=True)
class Refuse:
    """Refuse to act, with a user-facing reason."""

    message: str


Decision = Create | Resume | Fork | Refuse


@dataclass(frozen=True)
class SessionRow:
    """A named session for listing: identity, slot, path, and live state."""

    identity: str
    slot: int
    path: str
    session_id: str
    active: bool
    last_active: float | None = None  # epoch seconds; None when age is unknown


def _merge_slot(
    slots: dict[int, Slot], slot: int, session_id: str, active: bool, last_active: float | None
) -> None:
    """Insert/replace a slot, letting an active session win a slot collision."""
    existing = slots.get(slot)
    if existing is None or (active and not existing.active):
        slots[slot] = Slot(slot, session_id, active, last_active)


def build_slots(
    identity: str,
    path: str,
    name_by_session: dict[str, str],
    active_sessions: set[str],
    last_active: dict[str, float] | None = None,
) -> dict[int, Slot]:
    """Build the slot map for one ``identity`` + ``path``.

    ``name_by_session`` maps session id to its current name (last ``agent-name``
    per transcript). ``active_sessions`` is the set of session ids with a live
    roster entry. ``last_active`` optionally maps session id to epoch seconds.
    Names that do not parse, or that belong to another identity or path, are
    ignored. On a slot collision an active session wins.
    """
    la = last_active or {}
    slots: dict[int, Slot] = {}
    for session_id, name in name_by_session.items():
        parsed = parse_name(name)
        if parsed is None:
            continue
        row_identity, slot, row_path = parsed
        if row_identity != identity or row_path != path:
            continue
        _merge_slot(slots, slot, session_id, session_id in active_sessions, la.get(session_id))
    return slots


def list_rows(
    name_by_session: dict[str, str],
    active_sessions: set[str],
    last_active: dict[str, float] | None = None,
) -> list[SessionRow]:
    """All named sessions as sorted rows, deduped per (identity, slot, path).

    On a duplicate (identity, slot, path) an active session wins. Rows are
    sorted by identity, then slot, then path for stable display.
    """
    la = last_active or {}
    best: dict[tuple[str, int, str], SessionRow] = {}
    for session_id, name in name_by_session.items():
        parsed = parse_name(name)
        if parsed is None:
            continue
        identity, slot, path = parsed
        active = session_id in active_sessions
        key = (identity, slot, path)
        existing = best.get(key)
        if existing is None or (active and not existing.active):
            best[key] = SessionRow(identity, slot, path, session_id, active, la.get(session_id))
    return sorted(best.values(), key=lambda r: (r.identity, r.slot, r.path))


class AgeBand(enum.Enum):
    FRESH = "fresh"
    WARN = "warn"
    STALE = "stale"


def classify_age(
    now: float, last_active: float | None, stale_days: int, archive_days: int
) -> AgeBand:
    """Classify a session's age. Unknown age is treated as FRESH (never swept)."""
    if last_active is None:
        return AgeBand.FRESH
    age_days = (now - last_active) / 86400.0
    if age_days < stale_days:
        return AgeBand.FRESH
    if archive_days != 0 and age_days >= archive_days:
        return AgeBand.STALE
    return AgeBand.WARN


def _lowest_free(slots: dict[int, Slot]) -> int | None:
    """Lowest slot number in ``[SLOT_MIN, SLOT_MAX]`` not already taken."""
    for n in range(SLOT_MIN, SLOT_MAX + 1):
        if n not in slots:
            return n
    return None


def _all_in_use(identity: str, path: str) -> Refuse:
    return Refuse(f"all {SLOT_MAX} slots are in use for {identity} {path}")


def _bad_range() -> Refuse:
    return Refuse(f"slot must be between {SLOT_MIN} and {SLOT_MAX}")


def select(
    identity: str,
    path: str,
    slots: dict[int, Slot],
    requested_slot: int | None = None,
    fork: bool = False,
) -> Decision:
    """Decide what to do for ``identity`` + ``path`` given existing ``slots``.

    ``slots`` maps slot number to :class:`Slot`. ``requested_slot`` is the
    explicit ``--slot N`` (or ``None``). ``fork`` is the ``--fork`` flag.
    """
    if fork:
        return _select_fork(identity, path, slots, requested_slot)
    if requested_slot is not None:
        return _select_explicit(identity, path, slots, requested_slot)
    return _select_default(identity, path, slots)


def _select_default(identity: str, path: str, slots: dict[int, Slot]) -> Decision:
    """No ``--slot``: resume lowest idle slot, else create lowest free slot."""
    idle = sorted(n for n, s in slots.items() if not s.active)
    if idle:
        return Resume(slots[idle[0]].session_id)
    free = _lowest_free(slots)
    if free is None:
        return _all_in_use(identity, path)
    return Create(make_name(identity, free, path))


def _select_explicit(identity: str, path: str, slots: dict[int, Slot], slot: int) -> Decision:
    """Explicit ``--slot N``: create if free, resume if idle, refuse if active."""
    if not SLOT_MIN <= slot <= SLOT_MAX:
        return _bad_range()
    info = slots.get(slot)
    if info is None:
        return Create(make_name(identity, slot, path))
    if info.active:
        return Refuse(f"slot {slot:02d} is active; use --fork to branch it into a new session")
    return Resume(info.session_id)


def _select_fork(identity: str, path: str, slots: dict[int, Slot], slot: int | None) -> Decision:
    """``--fork``: copy the targeted slot's conversation into a new slot."""
    if slot is None:
        return Refuse("--fork requires --slot N to identify the session to fork")
    if not SLOT_MIN <= slot <= SLOT_MAX:
        return _bad_range()
    info = slots.get(slot)
    if info is None:
        return Refuse(f"slot {slot:02d} does not exist; nothing to fork")
    free = _lowest_free(slots)
    if free is None:
        return _all_in_use(identity, path)
    return Fork(info.session_id, make_name(identity, free, path))


@dataclass(frozen=True)
class PromptStale:
    """Warn-band: the resolver must prompt resume/fresh/cancel, then act."""

    session_id: str  # resume this on [r]
    name: str  # reclaim this name on [f] (archive session_id, then create name)
    age_days: int


PlanAction = Create | Resume | Fork | Refuse | PromptStale


@dataclass(frozen=True)
class SessionPlan:
    """A full session decision: cold slots to auto-archive, then an action."""

    auto_archive: list[Slot]
    action: PlanAction


def _idle_by_recency(slots: dict[int, Slot]) -> list[Slot]:
    """Idle slots, most-recently-active first (unknown age sorts oldest)."""
    idle = [s for s in slots.values() if not s.active]
    return sorted(
        idle,
        key=lambda s: (s.last_active is not None, s.last_active or 0.0),
        reverse=True,
    )


def plan_session(
    identity: str,
    path: str,
    slots: dict[int, Slot],
    now: float,
    stale_days: int,
    archive_days: int,
    requested_slot: int | None = None,
    fork: bool = False,
    fresh: bool = False,
) -> SessionPlan:
    """Plan a session launch: which cold slots to archive, then what to do."""
    if fork:
        return SessionPlan([], _select_fork(identity, path, slots, requested_slot))
    if fresh:
        return _plan_fresh(identity, path, slots, requested_slot)
    if requested_slot is not None:
        return SessionPlan([], _select_explicit(identity, path, slots, requested_slot))

    sweep = [
        s
        for s in slots.values()
        if not s.active
        and classify_age(now, s.last_active, stale_days, archive_days) == AgeBand.STALE
    ]
    swept = {s.slot for s in sweep}
    remaining = {n: s for n, s in slots.items() if n not in swept}
    return SessionPlan(
        sweep, _select_auto(identity, path, remaining, now, stale_days, archive_days)
    )


def _select_auto(
    identity: str,
    path: str,
    slots: dict[int, Slot],
    now: float,
    stale_days: int,
    archive_days: int,
) -> PlanAction:
    """Most-recent idle in FRESH resumes; in WARN prompts; else create free."""
    for slot in _idle_by_recency(slots):
        band = classify_age(now, slot.last_active, stale_days, archive_days)
        if band == AgeBand.FRESH:
            return Resume(slot.session_id)
        age_days = int((now - (slot.last_active or now)) / 86400.0)
        return PromptStale(slot.session_id, make_name(identity, slot.slot, path), age_days)
    free = _lowest_free(slots)
    if free is None:
        return _all_in_use(identity, path)
    return Create(make_name(identity, free, path))


def _plan_fresh(
    identity: str, path: str, slots: dict[int, Slot], requested_slot: int | None
) -> SessionPlan:
    """``--fresh``: archive the (cold) target and create fresh in that slot."""
    if requested_slot is not None:
        if not SLOT_MIN <= requested_slot <= SLOT_MAX:
            return SessionPlan([], _bad_range())
        target = slots.get(requested_slot)
        slot_no = requested_slot
    else:
        idle = _idle_by_recency(slots)
        target = idle[0] if idle else None
        slot_no = target.slot if target else (_lowest_free(slots) or 0)
    if slot_no == 0:
        return SessionPlan([], _all_in_use(identity, path))
    if target is not None and target.active:
        return SessionPlan(
            [], Refuse(f"slot {slot_no:02d} is active; cannot start fresh over a live session")
        )
    archive = [target] if target is not None else []
    return SessionPlan(archive, Create(make_name(identity, slot_no, path)))
