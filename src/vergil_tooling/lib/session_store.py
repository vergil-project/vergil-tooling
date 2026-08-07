"""The ``SessionStore`` seam: one boundary for session enumeration/resolution.

Everything the general code path needs about *which* Claude Code sessions exist
and *how a name maps to a session id* goes through this seam. Today the only
backend is :class:`ScrapeStore`, which reads the internal transcript JSONL and
``~/.claude/sessions/<pid>.json`` roster — the formats Claude documents as
unstable. That read is deliberately confined **here**: no other module parses a
transcript or the roster, so when a verified Agent SDK backend arrives it drops
in behind the same interface with no caller changes.

The pure resolution rule (:func:`resolve_over`) lives here too so it is unit
testable without any I/O: given the rows a store lists, resolve a name to at
most one session — a live session wins, ties between two live sessions **fail
loud**, and otherwise the most-recently-active idle session wins.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

# How long a name reservation (see ``ScrapeStore.reserve_name``) stays visible
# before it is ignored/swept. A reservation only has to bridge the gap between a
# ``--label`` create and claude writing that session's transcript/roster name —
# a few seconds. The TTL just bounds a *stale* reservation whose session never
# materialized (claude aborted before registering); it is generous because a
# session that did start is superseded by its real transcript entry long before
# it expires, so the TTL never truncates a live session's visibility.
_RESERVATION_TTL_SECONDS = 600.0


@dataclass(frozen=True)
class SessionInfo:
    """One Claude Code session as the seam sees it.

    ``name`` is ``None`` for a live-but-unnamed session (a fresh session that has
    not yet been named); such a row is enumerated but never matches a requested
    name. ``last_active`` is epoch seconds, or ``None`` when the age is unknown.

    ``materialized`` is ``True`` when claude has actually written a transcript for
    this session id — i.e. ``claude --resume <id>`` will find a conversation. It is
    ``False`` for a **reservation-only** session: one that ``--label`` minted and
    reserved a name for, but whose id claude has not yet persisted a conversation
    to (a created-but-unused session, whose transcript claude writes lazily). The
    resume path re-enters a reservation-only session *at* its id via
    ``--session-id`` rather than ``--resume`` (#2669), so the name→id binding
    stays stable and the resume does not fail with 'No conversation found'. It
    defaults to ``True`` because only the scrape backend can distinguish the two,
    and every other constructor (the host-side ``list`` view) neither knows nor
    consults it; the authoritative value is set by :meth:`ScrapeStore.list_sessions`.
    """

    session_id: str
    name: str | None
    cwd: str
    active: bool
    last_active: float | None
    materialized: bool = True


class AmbiguousSessionError(Exception):
    """A name resolved to two or more co-equal *live* sessions.

    Resolution never silently picks one of several live sessions holding the same
    name — that is the fail-loud rule of the seam.
    """


def resolve_over(rows: list[SessionInfo], name: str) -> SessionInfo | None:
    """Resolve ``name`` against ``rows`` to at most one session.

    The rule: consider only rows whose name matches exactly. **Zero matches →
    ``None``.** **Two or more *active* matches → raise
    :class:`AmbiguousSessionError`** (never guess between live sessions). A single
    active match wins outright. Otherwise (all matches idle) the most recently
    active wins, with an unknown age sorting oldest.
    """
    matches = [row for row in rows if row.name == name]
    if not matches:
        return None
    active = [row for row in matches if row.active]
    if len(active) >= 2:
        raise AmbiguousSessionError(
            f"name {name!r} resolves to {len(active)} live sessions: "
            f"{', '.join(row.session_id for row in active)}"
        )
    if len(active) == 1:
        return active[0]
    return max(matches, key=lambda row: (row.last_active is not None, row.last_active or 0.0))


@runtime_checkable
class SessionStore(Protocol):
    """The seam every session-enumeration/resolution caller depends on.

    Backends: :class:`ScrapeStore` (today) and a future SDK-backed store. Callers
    never branch on the backend; they call these three methods.
    """

    def list_sessions(self) -> list[SessionInfo]:
        """Every session this store can see, as :class:`SessionInfo` rows."""
        ...  # pragma: no cover

    def resolve_name(self, name: str) -> SessionInfo | None:
        """Resolve ``name`` to one session (or ``None``); fails loud on ambiguity."""
        ...  # pragma: no cover

    def rename(self, session_id: str, new_name: str) -> None:
        """Rename a (possibly detached) session — the backend's supported path."""
        ...  # pragma: no cover

    def reserve_name(self, session_id: str, name: str, cwd: str = "") -> None:
        """Record a just-created session's name *synchronously*, before it registers.

        Closes the ``--label`` uniqueness race (#2654): the name is visible to the
        next resolver read immediately, without waiting for claude's asynchronous
        transcript ``custom-title`` / roster ``name`` write. ``session_id`` must be
        the id claude is pinned to (``--session-id``) so the reservation and the
        real session resolve to a single row, never a duplicate.
        """
        ...  # pragma: no cover


class ScrapeStore:
    """``SessionStore`` backed by the transcript/roster scrape.

    Shipped unconditionally: the seam always has a working backend, independent of
    any Agent SDK. It wraps the existing scrape helpers in
    :mod:`vergil_tooling.bin.vrg_vm_resolve` (``name_by_session`` / ``read_roster``
    and friends) — the *only* place transcript/roster parsing is allowed — and
    adapts their output into :class:`SessionInfo` rows.

    ``rename`` appends a ``custom-title`` naming event to the session's transcript.
    Hand-writing a naming event is the isolated hack that is legitimate **only**
    here, inside the seam's scrape backend; every other code path renames via a
    supported interface.

    ``reserve_name`` adds a third, eager name source — a vrg-owned reservation file
    written synchronously at ``--label`` create time — that the union in
    ``list_sessions`` reads at lowest precedence to close the registration-latency
    race (#2654); the authoritative transcript/roster names win on overlap.

    ``slug`` optionally scopes the transcript read to a single project slug (the
    same scoping ``claude --resume`` uses); ``None`` scans every slug. The
    reservation read is unscoped: a name is checked for global uniqueness.
    """

    def __init__(self, claude_dir: Path, slug: str | None = None) -> None:
        self._claude_dir = claude_dir
        self._slug = slug

    @property
    def _projects_dir(self) -> Path:
        return self._claude_dir / "projects"

    @property
    def _sessions_dir(self) -> Path:
        return self._claude_dir / "sessions"

    @property
    def _reservations_dir(self) -> Path:
        # vrg-owned, VM-local, namespaced so claude never touches it. Holds the
        # eager ``--label`` name reservations that bridge the registration-latency
        # window (#2654). Distinct from claude's own ``sessions`` roster.
        return self._claude_dir / "vrg-session-reservations"

    def _read_reservations(self) -> dict[str, dict[str, object]]:
        """Live name reservations by session id, expired ones filtered out.

        A read-only view: it never deletes (that is the write-time sweep's job),
        it only *ignores* a reservation past :data:`_RESERVATION_TTL_SECONDS` so a
        stale one whose session never materialized cannot pin a name forever.
        """
        out: dict[str, dict[str, object]] = {}
        reservations = self._reservations_dir
        if not reservations.is_dir():
            return out
        now = time.time()
        for path in sorted(reservations.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            sid = data.get("sessionId")
            name = data.get("name")
            created = data.get("createdAt")
            if not isinstance(sid, str) or not isinstance(name, str):
                continue
            if (
                isinstance(created, (int, float))
                and now - float(created) > _RESERVATION_TTL_SECONDS
            ):
                continue
            out[sid] = data
        return out

    def _sweep_reservations(self) -> None:
        """Delete reservations that have done their job or gone stale.

        A reservation is removed once (a) its session's transcript exists — the
        authoritative name source has caught up, so the reservation is redundant —
        or (b) it has passed the TTL without ever materializing. Runs at
        reservation-write time so the read path stays side-effect free.
        """
        from vergil_tooling.bin import vrg_vm_resolve as res

        reservations = self._reservations_dir
        if not reservations.is_dir():
            return
        now = time.time()
        for path in reservations.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            sid = data.get("sessionId") if isinstance(data, dict) else None
            created = data.get("createdAt") if isinstance(data, dict) else None
            expired = (
                isinstance(created, (int, float))
                and now - float(created) > _RESERVATION_TTL_SECONDS
            )
            superseded = (
                isinstance(sid, str) and res.projects_glob(self._projects_dir, sid).exists()
            )
            if expired or superseded:
                path.unlink(missing_ok=True)

    def list_sessions(self) -> list[SessionInfo]:
        # Imported lazily to avoid an import cycle: ``vrg_vm_resolve`` imports this
        # module at load time. The indirection keeps the transcript/roster scrape
        # in one place while its low-level readers still live in the bin module
        # (and stay monkeypatchable there).
        from vergil_tooling.bin import vrg_vm_resolve as res

        projects = self._projects_dir
        roster = res.read_roster(self._sessions_dir)
        reservations = self._read_reservations()
        # Name sources, lowest precedence first. Reservations are the eager,
        # synchronous source that closes the ``--label`` registration race (#2654):
        # a just-created session's name is recorded here before claude has written
        # its transcript ``custom-title`` or roster ``name``. Transcript names cover
        # idle sessions absent from the roster; roster names are authoritative for
        # live sessions and win on overlap. Because a reserved id is the one claude
        # adopts (``--session-id``), a reservation that overlaps a real source is the
        # *same* dict key — one row with the authoritative name, never a duplicate.
        reserved_names = {sid: str(data["name"]) for sid, data in reservations.items()}
        names = {
            **reserved_names,
            **res.name_by_session(projects, self._slug),
            **res.roster_names(roster),
        }
        active = res.active_session_ids(roster)
        last_active = res._roster_updated_at(roster)
        for sid in names:
            if sid not in last_active:
                ts = res._last_activity(res.projects_glob(projects, sid))
                if ts is not None:
                    last_active[sid] = ts
        cwds = _roster_cwds(roster)
        reserved_cwds = {
            sid: str(data["cwd"])
            for sid, data in reservations.items()
            if isinstance(data.get("cwd"), str) and data["cwd"]
        }

        # Named sessions first (preserving the union's order), then any other known
        # session id — a live-but-unnamed session, or one carrying only a roster
        # last-activity — as a name=None row so the seam surfaces every session.
        extra = sorted(sid for sid in (active | set(last_active)) if sid not in names)

        def _cwd(sid: str) -> str:
            # The live roster is authoritative; for an idle session absent from it
            # the transcript's recorded cwd stands in, so ``--resume`` can still
            # derive that session's memory slug (#2607). A reservation-only session
            # (created but not yet registered) has neither, so its recorded cwd
            # stands in last. Unknown ⇒ "".
            if sid in cwds:
                return cwds[sid]
            transcript_cwd = res.transcript_cwd(res.projects_glob(projects, sid))
            return transcript_cwd or reserved_cwds.get(sid, "")

        def _materialized(sid: str) -> bool:
            # True iff claude has written a transcript for this id — the exact
            # signal ``--resume`` needs (and the same one ``_sweep_reservations``
            # calls "superseded"). A reservation-only session (``--label`` minted
            # the id and reserved its name, but claude has not yet persisted a
            # conversation there) has no transcript, so ``--resume`` would fail with
            # 'No conversation found'; the resume path re-enters it at its id via
            # ``--session-id`` instead (#2669).
            return res.projects_glob(projects, sid).exists()

        return [
            SessionInfo(
                session_id=sid,
                name=names.get(sid),
                cwd=_cwd(sid),
                active=sid in active,
                last_active=last_active.get(sid),
                materialized=_materialized(sid),
            )
            for sid in [*names, *extra]
        ]

    def resolve_name(self, name: str) -> SessionInfo | None:
        return resolve_over(self.list_sessions(), name)

    def rename(self, session_id: str, new_name: str) -> None:
        from vergil_tooling.bin import vrg_vm_resolve as res

        transcript = res.projects_glob(self._projects_dir, session_id)
        entry = {"type": "custom-title", "customTitle": new_name, "sessionId": session_id}
        with transcript.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def reserve_name(self, session_id: str, name: str, cwd: str = "") -> None:
        """Persist a name reservation keyed by the session id claude will adopt.

        Written by the ``--label`` create path *before* it exec's ``claude
        --session-id <session_id> -n <name>``, so the name is resolvable the instant
        the create decision is made — closing the window in which a rapid second
        ``--label`` would miss the collision (#2654). Sweeps done/stale reservations
        first so the directory never accumulates. Pinning ``session_id`` to the id
        claude adopts is what makes the reservation and the real session one row.
        """
        self._sweep_reservations()
        reservations = self._reservations_dir
        reservations.mkdir(parents=True, exist_ok=True)
        entry = {
            "sessionId": session_id,
            "name": name,
            "cwd": cwd,
            "createdAt": time.time(),
        }
        (reservations / f"{session_id}.json").write_text(json.dumps(entry) + "\n")


def _roster_cwds(roster: list[dict[str, object]]) -> dict[str, str]:
    """Map session id to its roster ``cwd`` when the roster records one."""
    out: dict[str, str] = {}
    for entry in roster:
        sid = entry.get("sessionId")
        cwd = entry.get("cwd")
        if isinstance(sid, str) and isinstance(cwd, str):
            out[sid] = cwd
    return out
