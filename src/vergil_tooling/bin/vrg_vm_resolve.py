"""In-VM resolver for ``vrg-vm session``.

Runs **inside** the identity VM (vergil-tooling is installed there). It reads the
VM-local roster (``~/.claude/sessions/*.json``) and the shared transcripts
(``~/.claude/projects/*/*.jsonl``), classifies the sessions for an identity +
workspace path, applies the slot-selection rules, and execs Claude Code. In
``--list-json`` mode it instead emits every named session's state for the host's
``vrg-vm list --sessions``.

Reading the store from inside the VM is what makes detection correct: the roster
is VM-local, so every ``pid`` belongs to this VM and liveness is checkable here.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib.session import (
    Create,
    Decision,
    Refuse,
    build_slots,
    make_label_name,
    plan_session,
    validate_label,
)
from vergil_tooling.lib.session_store import (
    AmbiguousSessionError,
    ScrapeStore,
    SessionInfo,
    SessionStore,
)


def _claude_dir() -> Path:
    return Path.home() / ".claude"


def _project_slug(cwd: str) -> str:
    """Encode a cwd into Claude's project-slug form.

    Claude stores each session's transcript under
    ``~/.claude/projects/<slug>/`` where ``<slug>`` is the cwd with every
    non-alphanumeric character (slashes, dots, etc.) replaced by ``-`` — the
    same directory ``claude --resume`` searches.
    """
    return "".join(c if c.isalnum() else "-" for c in cwd)


# Transcript event types that carry a session name, mapped to their value
# field. Claude Code originally wrote ``agent-name`` events and renamed them
# to ``custom-title`` (~2.1.16x); both forms must be read.
_NAME_EVENT_FIELDS = {"agent-name": "agentName", "custom-title": "customTitle"}


def _name_from_line(raw: bytes) -> str | None:
    """Extract a session-name value from one transcript line, or ``None``.

    A line names the session only if it is a JSON object whose ``type`` is a
    recognized naming event (``agent-name``/``custom-title``) carrying a string
    value in the matching field. The cheap substring guard skips the JSON parse
    for the overwhelming majority of lines (ordinary turns) that cannot name.
    """
    line = raw.strip()
    if not line or (b'"agent-name"' not in line and b'"custom-title"' not in line):
        return None
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return None
    event_type = entry.get("type")
    if not isinstance(event_type, str):
        return None
    field = _NAME_EVENT_FIELDS.get(event_type)
    if field is None:
        return None
    value = entry.get(field)
    return value if isinstance(value, str) else None


def _last_session_name(transcript: Path) -> str | None:
    """Return the last session-name value in a transcript, or ``None``.

    The last naming event in file order wins, regardless of which of the two
    event types (``agent-name`` or ``custom-title``) carries it. The file is
    read end-first in blocks and the scan stops at the first naming event seen
    from the end — which is the last in file order — so a large transcript need
    not be read in full. This matters because the resume path runs this for
    every transcript in a project slug, and that history accumulates on a
    persistent volume across VM rebuilds; a full forward read of all of it is
    what stalls reconnect before Claude starts.
    """
    try:
        with transcript.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            block = 64 * 1024
            data = b""
            while pos > 0:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
                lines = data.split(b"\n")
                data = lines[0] if pos > 0 else b""
                candidates = lines[1:] if pos > 0 else lines
                for raw in reversed(candidates):
                    name = _name_from_line(raw)
                    if name is not None:
                        return name
    except OSError:
        return None
    return None


def name_by_session(projects_dir: Path, slug: str | None = None) -> dict[str, str]:
    """Map session id (transcript stem) to its current name.

    With ``slug`` set, only that one project slug's transcripts are read — the
    scoping ``claude --resume`` itself uses. Without it, every slug is scanned.
    """
    result: dict[str, str] = {}
    if not projects_dir.is_dir():
        return result
    pattern = f"{slug}/*.jsonl" if slug is not None else "*/*.jsonl"
    for transcript in sorted(projects_dir.glob(pattern)):
        name = _last_session_name(transcript)
        if name is not None:
            result[transcript.stem] = name
    return result


def _parse_ts(value: object) -> float | None:
    """Parse an ISO-8601 (``Z`` or offset) timestamp string to epoch seconds."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _last_activity(transcript: Path) -> float | None:
    """Epoch seconds of the last *timestamped* entry, via a bounded tail read.

    Reads the file end-first in chunks so large transcripts stay cheap, scanning
    backward for the most recent line that carries a ``timestamp``.
    """
    try:
        with transcript.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            block = 64 * 1024
            data = b""
            while pos > 0:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
                lines = data.split(b"\n")
                data = lines[0] if pos > 0 else b""
                candidates = lines[1:] if pos > 0 else lines
                for raw in reversed(candidates):
                    line = raw.strip()
                    if not line or b'"timestamp"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_ts(entry.get("timestamp"))
                    if ts is not None:
                        return ts
    except OSError:
        return None
    return None


def transcript_cwd(transcript: Path) -> str | None:
    """The working directory a session ran in, from its transcript, or ``None``.

    Every substantive transcript entry records the ``cwd`` it executed in. The
    first one found (read forward and bounded — a leading ``summary`` entry
    carries none, but the first real turn does) is authoritative and stable for
    the session, so a large transcript need not be read in full. This is what
    lets the seam report a cwd for an *idle* session absent from the live roster,
    so ``--resume`` can derive its memory slug from the resolved session (#2607).
    """
    try:
        with transcript.open("rb") as fh:
            for _ in range(200):
                raw = fh.readline()
                if not raw:
                    break
                line = raw.strip()
                if not line or b'"cwd"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def read_roster(sessions_dir: Path) -> list[dict[str, object]]:
    """Read the VM-local roster files into a list of dicts."""
    entries: list[dict[str, object]] = []
    if not sessions_dir.is_dir():
        return entries
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


def _parse_starttime(stat: str) -> str | None:
    """Extract field 22 (starttime) from a ``/proc/<pid>/stat`` string.

    The comm field (field 2) may contain spaces and parentheses, so everything
    after the final ``)`` is parsed positionally: state is the first token
    (field 3), making starttime index 19 in that tail.
    """
    try:
        tail = stat.rsplit(")", 1)[1].split()
    except IndexError:
        return None
    if len(tail) <= 19:
        return None
    return tail[19]


def _proc_start(pid: int) -> str | None:
    """Process start time for ``pid``, or ``None`` if it does not exist."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return _parse_starttime(stat)


def _is_live(pid: int, proc_start: object) -> bool:
    """True if ``pid`` is alive and (when known) matches ``proc_start``."""
    actual = _proc_start(pid)
    if actual is None:
        return False
    if proc_start is None:
        return True
    return str(proc_start) == actual


def active_session_ids(roster: list[dict[str, object]]) -> set[str]:
    """Session ids whose roster ``pid`` is live (PID-reuse safe)."""
    out: set[str] = set()
    for entry in roster:
        pid = entry.get("pid")
        session_id = entry.get("sessionId")
        if not isinstance(pid, int) or not isinstance(session_id, str):
            continue
        if _is_live(pid, entry.get("procStart")):
            out.add(session_id)
    return out


def projects_glob(projects_dir: Path, session_id: str) -> Path:
    """Path to a session's transcript (``<slug>/<sessionId>.jsonl``)."""
    matches = sorted(projects_dir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else projects_dir / f"{session_id}.jsonl"


def _roster_updated_at(roster: list[dict[str, object]]) -> dict[str, float]:
    """Map session id to last-active epoch seconds from roster ``updatedAt`` (ms)."""
    out: dict[str, float] = {}
    for entry in roster:
        sid = entry.get("sessionId")
        upd = entry.get("updatedAt")
        if isinstance(sid, str) and isinstance(upd, (int, float)):
            out[sid] = float(upd) / 1000.0
    return out


def roster_names(roster: list[dict[str, object]]) -> dict[str, str]:
    """Map session id to its roster ``name`` (authoritative for live sessions).

    The roster records each live session's current name directly, so a session
    that has not yet written a naming event to its transcript — e.g. a freshly
    created session with no turns, hence no transcript at all — is still named
    here. Transcript names, by contrast, only cover sessions that have run.
    """
    out: dict[str, str] = {}
    for entry in roster:
        sid = entry.get("sessionId")
        name = entry.get("name")
        if isinstance(sid, str) and isinstance(name, str):
            out[sid] = name
    return out


def _store(slug: str | None = None) -> ScrapeStore:
    """Build the ``SessionStore`` backend the resolver enumerates/resolves through.

    A single seam constructor so every resolver path (state read, label
    uniqueness check) goes through the same backend — and tests can substitute a
    fake store by patching this one function.
    """
    return ScrapeStore(_claude_dir(), slug)


def _read_state(
    slug: str | None = None,
) -> tuple[dict[str, str], set[str], dict[str, float]]:
    """Enumerate sessions through the ``SessionStore`` seam.

    The transcript/roster scrape now lives behind :class:`ScrapeStore`; this
    adapter collapses the seam's :class:`SessionInfo` rows back into the
    ``(name_by_session, active_ids, last_active)`` tuple the slot machinery still
    consumes. A ``name=None`` row (a live-but-unnamed session) contributes to the
    active set and last-activity map but not to the name map — exactly as the
    prior direct read did.
    """
    rows = _store(slug).list_sessions()
    names = {row.session_id: row.name for row in rows if row.name is not None}
    active = {row.session_id for row in rows if row.active}
    last_active = {row.session_id: row.last_active for row in rows if row.last_active is not None}
    return names, active, last_active


def _exec_claude(args: list[str]) -> int:
    os.execvp("claude", ["claude", *args])  # noqa: S606, S607
    return 0  # reached only when execvp is stubbed (tests)


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def _execute(action: Decision, extra: list[str]) -> int:
    """Carry out a ``--fork`` / ``--fresh`` plan action.

    With ``--slot`` and the auto-resume-most-recent default gone (#2607), the only
    actions that still reach here are ``Create`` (``--fresh`` creating a fresh
    session in the target slot) and ``Refuse`` (``--fork`` with no slot to
    target). Exact-name attach (``--resume``) resolves through the seam in
    :func:`_resume_by_name`, not here.
    """
    if isinstance(action, Refuse):
        print(f"ERROR: {action.message}", file=sys.stderr)
        return 1
    if isinstance(action, Create):
        _note(f"Creating session {action.name}")
        return _exec_claude(["-n", action.name, *extra])
    # Resume/Fork were the other slot-selection outcomes; neither is reachable
    # now that selection is by exact name. Guard loudly rather than mis-exec.
    raise RuntimeError(f"unexpected session action {type(action).__name__}")  # pragma: no cover


def plan_resume(store: SessionStore, name: str) -> SessionInfo:
    """Resolve an exact session name to the session to resume, via the seam.

    Delegates to ``store.resolve_name`` and applies the fail-loud contract of
    exact-name attach (#2607): a name that resolves to **no** visible session and
    a name that resolves to **two or more co-equal live** sessions both abort with
    a nonzero exit and an actionable message — the resolver never guesses which
    session the caller meant. On success the resolved :class:`SessionInfo` carries
    both the session id (for ``--resume``) and the cwd (for memory-slug parity).
    """
    try:
        info = store.resolve_name(name)
    except AmbiguousSessionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if info is None:
        print(f"ERROR: no session named {name!r} — create it with --label", file=sys.stderr)
        raise SystemExit(1)
    return info


def _resume_by_name(name: str, extra: list[str]) -> int:
    """Attach to the session named ``name`` exactly, deriving cwd from it.

    The store is unscoped (every slug) so the name resolves globally; the bootstrap
    cwd is then taken from the resolved session's own cwd rather than any positional
    the caller passed, so Claude derives the same memory slug the session was created
    under (#2607). ``-n`` re-asserts the exact name so the prompt-box title is
    restored even when the last transcript naming event is a legacy ``agent-name``.
    """
    info = plan_resume(ScrapeStore(_claude_dir()), name)
    if info.cwd:
        os.chdir(info.cwd)
    _note(f"Resuming session {name}")
    return _exec_claude(["--resume", info.session_id, "-n", name, *extra])


def _list_and_guide(slug: str) -> int:
    """No verb given: show this workspace's sessions by recency, name the two verbs.

    The old no-arg behavior — auto-resume the most-recent idle session, else create
    one — is gone (#2607). A session is now addressed explicitly: ``--label`` to
    create ``label:workspace`` (Task 2) or ``--resume`` to attach by exact name.
    Returns nonzero: no session was launched.
    """
    rows = [row for row in ScrapeStore(_claude_dir(), slug).list_sessions() if row.name is not None]
    rows.sort(key=lambda row: (row.last_active is not None, row.last_active or 0.0), reverse=True)
    if rows:
        print("Sessions for this workspace (most recent first):", file=sys.stderr)
        for row in rows:
            marker = "* " if row.active else "  "
            print(f"  {marker}{row.name}", file=sys.stderr)
    else:
        print("No sessions yet for this workspace.", file=sys.stderr)
    print(
        "\nName the session you want — choose one verb:\n"
        "  --label <label>   start a new session named <label>:<workspace>\n"
        "  --resume <name>   attach to an existing session by its exact name",
        file=sys.stderr,
    )
    return 1


def _resolve_label(path: str, label: str, extra: list[str]) -> int:
    """Create a purpose-named ``label:workspace`` session, enforcing uniqueness.

    The named-creation path (``--label``): validate the label slug (soft-warning
    an off-convention prefix, failing loud on a structurally invalid one), compose
    ``label:workspace``, and refuse if a **visible** session already holds that
    name — creation must never silently shadow an existing session, so a collision
    (one match, or ambiguously many) is an error directing the user to ``--resume``
    it or pick another label. Otherwise exec the existing ``Create`` path.
    """
    try:
        warnings = validate_label(label)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    name = make_label_name(label, path)
    store = _store(_project_slug(str(Path.cwd())))
    try:
        # A single match, or ambiguously many (the seam fails loud), both mean the
        # name is taken — creation must never silently shadow an existing session.
        collision = store.resolve_name(name) is not None
    except AmbiguousSessionError:
        collision = True
    if collision:
        print(
            f"ERROR: a session named {name!r} already exists; --resume it or pick another label",
            file=sys.stderr,
        )
        return 1
    return _execute(Create(name), extra)


def resolve(
    identity: str,
    path: str,
    fork: bool,
    fresh: bool,
    extra: list[str],
    resume_name: str | None = None,
    label: str | None = None,
) -> int:
    """Attach a named session, or (no verb) list this workspace and guide.

    ``label`` short-circuits into the named-creation path (``--label``): it
    composes and uniqueness-checks ``label:workspace`` and creates that session
    (see :func:`_resolve_label`), bypassing the slot machinery entirely.

    ``resume_name`` (``--resume``) resolves an exact session name to a session id
    through the ``SessionStore`` seam and attaches to it, deriving the bootstrap
    cwd from the resolved session (#2607). With no verb the recency list + the two
    verbs are printed and a nonzero code returned — there is no auto-resume-most-
    recent / auto-create default and no ``--slot``. ``fork`` / ``fresh`` retain the
    legacy slot machinery (reconceived by later tasks in the epic); auto-archive
    and the staleness sweep are gone (#2608).
    """
    if label is not None:
        return _resolve_label(path, label, extra)
    if resume_name is not None:
        return _resume_by_name(resume_name, extra)
    slug = _project_slug(str(Path.cwd()))
    if not fork and not fresh:
        return _list_and_guide(slug)
    names, active, last_active = _read_state(slug)
    slots = build_slots(identity, path, names, active, last_active)
    action = plan_session(identity, path, slots, None, fork, fresh)
    return _execute(action, extra)


def list_json() -> int:
    """Print every session the seam sees as JSON (for ``list --sessions``).

    Emits :class:`SessionInfo`-shaped rows straight from the store — session id,
    name (``None`` for a live-but-unnamed session), cwd, liveness, and
    last-activity. A legacy ``archived@…`` name rides through as an opaque string;
    the archive *behavior* is gone (#2608), but the seam still lists and resolves
    such a name. The host applies the recency display-filter; this only enumerates.
    """
    rows = [
        {
            "sessionId": row.session_id,
            "name": row.name,
            "cwd": row.cwd,
            "active": row.active,
            "lastActive": row.last_active,
        }
        for row in _store().list_sessions()
    ]
    print(json.dumps(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vrg-vm-resolve-session")
    parser.add_argument("--identity")
    parser.add_argument("--path")
    parser.add_argument("--fork", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--resume-name", dest="resume_name", default=None)
    parser.add_argument("--label", dest="label", default=None)
    parser.add_argument("--list-json", action="store_true", dest="list_json")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.list_json:
        return list_json()

    if not args.identity or not args.path:
        print("ERROR: --identity and --path are required", file=sys.stderr)
        return 1

    extra = args.extra
    if extra and extra[0] == "--":
        extra = extra[1:]
    return resolve(
        args.identity,
        args.path,
        args.fork,
        args.fresh,
        extra,
        args.resume_name,
        args.label,
    )


if __name__ == "__main__":
    sys.exit(main())
