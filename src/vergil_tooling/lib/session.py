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

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vergil_tooling.lib.session_store import SessionInfo

SLOT_MIN = 1
SLOT_MAX = 99


def make_name(identity: str, slot: int, path: str) -> str:
    """Build a session name ``<identity>:<NN>:<path>`` (slot zero-padded)."""
    return f"{identity}:{slot:02d}:{path}"


_LABEL_CONVENTION_PREFIXES = ("epic-", "adhoc-")


def make_label_name(label: str, workspace: str) -> str:
    """Compose a purpose-named session name ``<label>:<workspace>``.

    The name drops identity entirely — a session is named by *what it is for*
    (its ``label``) scoped to *where it runs* (its ``workspace`` path), joined by
    the same unambiguous colon delimiter the slot scheme used. ``label`` is a
    clean slug (validated by :func:`validate_label`); ``workspace`` is the
    workspace-relative path, e.g. ``vergil-project/vergil-tooling``.
    """
    return f"{label}:{workspace}"


def validate_label(label: str) -> list[str]:
    """Validate a session ``label`` slug, returning non-fatal warnings.

    Two tiers, mirroring the spec's "clean slug required, convention soft":

    - **Hard (raises :class:`ValueError`).** The label must be a clean slug: a
      ``:`` would break the ``label:workspace`` delimiter, and whitespace or an
      empty/blank label is never a valid name. These are structural — the name
      cannot be formed — so they fail loud.
    - **Soft (returned as a warning, never rejected).** A label that is not
      prefixed ``epic-``/``adhoc-`` is off-convention but still usable; it yields
      one warning string so the caller can surface it without blocking creation.

    Returns an empty list for a clean, on-convention label.
    """
    if not label or not label.strip():
        msg = "label must not be empty"
        raise ValueError(msg)
    if ":" in label:
        msg = "label must not contain ':' (the label:workspace delimiter)"
        raise ValueError(msg)
    if any(ch.isspace() for ch in label):
        msg = "label must not contain whitespace"
        raise ValueError(msg)
    warnings: list[str] = []
    if not label.startswith(_LABEL_CONVENTION_PREFIXES):
        warnings.append(
            f"label {label!r} does not follow the epic-/adhoc- naming convention (using it anyway)"
        )
    return warnings


def parse_name(name: str) -> tuple[str, int, str] | None:
    """Parse ``<identity>:<NN>:<path>`` into its fields.

    Returns ``None`` for any string that does not match the scheme (wrong field
    count, empty identity/path, or a slot that is not a two-digit number in
    range). A legacy ``archived@…`` name is an **opaque** string — the archive
    behavior is gone, but such a name must never misparse into a phantom slot
    (its embedded timestamp colons would otherwise read as ``<id>:<NN>:<path>``),
    so it is rejected here too.
    """
    if name.startswith("archived@"):
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


def _displaces(
    existing_active: bool,
    existing_last_active: float | None,
    active: bool,
    last_active: float | None,
) -> bool:
    """True if a new claimant beats the existing holder of a name collision.

    Liveness dominates: a live session always beats a dead one. Between
    claimants of equal liveness the more recently active wins — a ``/clear``
    rotation leaves the abandoned session id still claiming the slot name,
    and recency is what picks the current id over it. Unknown ages and exact
    ties keep the incumbent.
    """
    if active != existing_active:
        return active
    if last_active is None:
        return False
    return existing_last_active is None or last_active > existing_last_active


def _merge_slot(
    slots: dict[int, Slot], slot: int, session_id: str, active: bool, last_active: float | None
) -> None:
    """Insert/replace a slot: liveness wins a collision, then recency."""
    existing = slots.get(slot)
    if existing is None or _displaces(existing.active, existing.last_active, active, last_active):
        slots[slot] = Slot(slot, session_id, active, last_active)


def build_slots(
    identity: str,
    path: str,
    name_by_session: dict[str, str],
    active_sessions: set[str],
    last_active: dict[str, float] | None = None,
) -> dict[int, Slot]:
    """Build the slot map for one ``identity`` + ``path``.

    ``name_by_session`` maps session id to its current name (last naming event
    per transcript). ``active_sessions`` is the set of session ids with a live
    roster entry. ``last_active`` optionally maps session id to epoch seconds.
    Names that do not parse, or that belong to another identity or path, are
    ignored. On a slot collision an active session wins, then the most
    recently active.
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

    On a duplicate (identity, slot, path) an active session wins, then the
    most recently active. Rows are sorted by identity, then slot, then path
    for stable display.
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
        if existing is None or _displaces(
            existing.active, existing.last_active, active, la.get(session_id)
        ):
            best[key] = SessionRow(identity, slot, path, session_id, active, la.get(session_id))
    return sorted(best.values(), key=lambda r: (r.identity, r.slot, r.path))


def filter_recent(
    rows: Iterable[SessionInfo],
    now: float,
    recent_days: int,
    all: bool = False,  # noqa: A002 — the CLI flag this mirrors is literally ``--all``
) -> list[SessionInfo]:
    """Keep only sessions active within ``recent_days`` (the display recency band).

    This is the replacement for the deleted staleness bands: it does not touch,
    rename, or delete anything — it purely decides which rows a listing shows.

    - ``all=True`` returns every row unchanged (the ``--all`` escape hatch).
    - A **live** session is always kept (a session in use is always relevant,
      regardless of its last-activity timestamp).
    - An **unknown** ``last_active`` is kept (age unknown ⇒ shown, never hidden —
      the same "unknown is fresh" bias the old bands used).
    - Otherwise the row is kept iff it was last active no more than
      ``recent_days`` days before ``now``.
    """
    if all:
        return list(rows)
    cutoff = now - recent_days * 86400.0
    return [
        row for row in rows if row.active or row.last_active is None or row.last_active >= cutoff
    ]


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


def select_by_name(
    requested_name: str,
    name_by_session: dict[str, str],
    active_sessions: set[str],
    last_active: dict[str, float] | None = None,
) -> Decision:
    """Resume the session whose current name equals ``requested_name`` exactly.

    Sessions renamed to a human-facing title — e.g. an epic session renamed to
    ``epic-85-centralize-epics-adhoc`` — do not fit the ``<identity>:<NN>:<path>``
    slot scheme and so fall out of the slot map entirely; the only way to address
    them is by their exact display name. ``name_by_session`` maps session id to
    its current name (see the resolver's state read); the match is exact, so an
    ``archived@…`` label never collides with a live title.

    A name may be held by more than one session id — a ``/clear`` rotation leaves
    the abandoned id still carrying the title in its transcript — so ties resolve
    by the module's standard collision rule (:func:`_displaces`): a live session
    beats a dead one, then the more recently active wins. No match refuses.
    """
    la = last_active or {}
    best: tuple[str, bool, float | None] | None = None
    for session_id, name in name_by_session.items():
        if name != requested_name:
            continue
        active = session_id in active_sessions
        if best is None or _displaces(best[1], best[2], active, la.get(session_id)):
            best = (session_id, active, la.get(session_id))
    if best is None:
        return Refuse(f"no session named {requested_name!r} for this workspace")
    return Resume(best[0])


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
    requested_slot: int | None = None,
    fork: bool = False,
    fresh: bool = False,
) -> Decision:
    """Plan a ``--fork`` / ``--fresh`` session launch (legacy slot machinery).

    Auto-archive and the staleness bands are gone (issue #2608), so a plan is now
    a single :class:`Decision` — there is no set of cold slots to sweep. The only
    remaining callers are ``--fork`` and ``--fresh`` (create/resume-by-name is the
    supported path via the seam); the no-verb default is handled by the resolver.
    ``--fresh`` here simply creates a fresh session in the target slot — the
    supported *retire-rename* of the prior session is a separate change (Task 5).
    """
    if fork:
        return _select_fork(identity, path, slots, requested_slot)
    if fresh:
        return _plan_fresh(identity, path, slots, requested_slot)
    if requested_slot is not None:
        return _select_explicit(identity, path, slots, requested_slot)
    return _select_default(identity, path, slots)


def _plan_fresh(
    identity: str, path: str, slots: dict[int, Slot], requested_slot: int | None
) -> Decision:
    """``--fresh``: create a fresh session in the (cold) target slot.

    Picks the target slot (explicit, else the most-recent idle, else the lowest
    free) and creates a new session there. The prior session is left untouched —
    reclaiming its name via a supported rename is Task 5's retire-rename, not this
    interim create.
    """
    if requested_slot is not None:
        if not SLOT_MIN <= requested_slot <= SLOT_MAX:
            return _bad_range()
        target = slots.get(requested_slot)
        slot_no = requested_slot
    else:
        idle = _idle_by_recency(slots)
        target = idle[0] if idle else None
        slot_no = target.slot if target else (_lowest_free(slots) or 0)
    if slot_no == 0:
        return _all_in_use(identity, path)
    if target is not None and target.active:
        return Refuse(f"slot {slot_no:02d} is active; cannot start fresh over a live session")
    return Create(make_name(identity, slot_no, path))
