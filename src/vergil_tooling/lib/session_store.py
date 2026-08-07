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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SessionInfo:
    """One Claude Code session as the seam sees it.

    ``name`` is ``None`` for a live-but-unnamed session (a fresh session that has
    not yet been named); such a row is enumerated but never matches a requested
    name. ``last_active`` is epoch seconds, or ``None`` when the age is unknown.
    """

    session_id: str
    name: str | None
    cwd: str
    active: bool
    last_active: float | None


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

    ``slug`` optionally scopes the transcript read to a single project slug (the
    same scoping ``claude --resume`` uses); ``None`` scans every slug.
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

    def list_sessions(self) -> list[SessionInfo]:
        # Imported lazily to avoid an import cycle: ``vrg_vm_resolve`` imports this
        # module at load time. The indirection keeps the transcript/roster scrape
        # in one place while its low-level readers still live in the bin module
        # (and stay monkeypatchable there).
        from vergil_tooling.bin import vrg_vm_resolve as res

        projects = self._projects_dir
        roster = res.read_roster(self._sessions_dir)
        # Roster names are authoritative for live sessions (and cover sessions with
        # no transcript yet); transcript names cover idle sessions absent from the
        # roster. Union them, letting the live roster name win on overlap.
        names = {**res.name_by_session(projects, self._slug), **res.roster_names(roster)}
        active = res.active_session_ids(roster)
        last_active = res._roster_updated_at(roster)
        for sid in names:
            if sid not in last_active:
                ts = res._last_activity(res.projects_glob(projects, sid))
                if ts is not None:
                    last_active[sid] = ts
        cwds = _roster_cwds(roster)

        # Named sessions first (preserving the union's order), then any other known
        # session id — a live-but-unnamed session, or one carrying only a roster
        # last-activity — as a name=None row so the seam surfaces every session.
        extra = sorted(sid for sid in (active | set(last_active)) if sid not in names)
        return [
            SessionInfo(
                session_id=sid,
                name=names.get(sid),
                cwd=cwds.get(sid, ""),
                active=sid in active,
                last_active=last_active.get(sid),
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


def _roster_cwds(roster: list[dict[str, object]]) -> dict[str, str]:
    """Map session id to its roster ``cwd`` when the roster records one."""
    out: dict[str, str] = {}
    for entry in roster:
        sid = entry.get("sessionId")
        cwd = entry.get("cwd")
        if isinstance(sid, str) and isinstance(cwd, str):
            out[sid] = cwd
    return out
