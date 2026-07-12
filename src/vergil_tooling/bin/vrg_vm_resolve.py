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
    Fork,
    PlanAction,
    PromptStale,
    Refuse,
    Resume,
    Slot,
    build_slots,
    list_rows,
    make_archived_name,
    parse_archived,
    parse_name,
    plan_session,
    select_by_name,
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


def _read_state(
    slug: str | None = None,
) -> tuple[dict[str, str], set[str], dict[str, float]]:
    cdir = _claude_dir()
    projects = cdir / "projects"
    roster = read_roster(cdir / "sessions")
    # Roster names are authoritative for live sessions and cover ones with no
    # transcript yet; transcript names cover dead/archived sessions absent from
    # the roster. Union them, letting the live roster name win on overlap.
    names = {**name_by_session(projects, slug), **roster_names(roster)}
    active = active_session_ids(roster)
    last_active = _roster_updated_at(roster)
    for sid in names:
        if sid not in last_active:
            ts = _last_activity(projects_glob(projects, sid))
            if ts is not None:
                last_active[sid] = ts
    return names, active, last_active


def _archive_session(session_id: str, timestamp: str) -> None:
    """Relabel a cold session by appending an archived ``custom-title`` entry.

    ``custom-title`` is the event type current Claude Code writes, so the
    archived label also shows up in Claude's own resume picker.
    """
    transcript = projects_glob(_claude_dir() / "projects", session_id)
    current = _last_session_name(transcript)
    if current is None:
        return
    entry = {
        "type": "custom-title",
        "customTitle": make_archived_name(current, timestamp),
        "sessionId": session_id,
    }
    try:
        with transcript.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        return


def _exec_claude(args: list[str]) -> int:
    os.execvp("claude", ["claude", *args])  # noqa: S606, S607
    return 0  # reached only when execvp is stubbed (tests)


def _now() -> float:
    return datetime.datetime.now(tz=datetime.UTC).timestamp()


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prompt_stale(path: str, slot: int, age_days: int) -> str:
    """Return ``'r'`` (resume), ``'f'`` (fresh), or ``'c'`` (cancel). Non-TTY -> ``'r'``."""
    if not sys.stdin.isatty():
        return "r"
    print(
        f"Slot {slot:02d} for {path} was last active {age_days} days ago.",
        file=sys.stderr,
    )
    answer = input("[r]esume / [f]resh / [c]ancel? ").strip().lower()
    return {"r": "r", "f": "f", "c": "c"}.get(answer[:1], "c")


def _run_sweep(slots: list[Slot]) -> None:
    timestamp = _now_iso()
    for slot in slots:
        print(f"auto-archiving slot {slot.slot:02d} ({slot.session_id})…", file=sys.stderr)
        _archive_session(slot.session_id, timestamp)


def _slot_num(name: str) -> int:
    parsed = parse_name(name)
    return parsed[1] if parsed else 0


def _note(message: str) -> None:
    print(message, file=sys.stderr)


def _execute(path: str, action: PlanAction, extra: list[str], names: dict[str, str]) -> int:
    if isinstance(action, Refuse):
        print(f"ERROR: {action.message}", file=sys.stderr)
        return 1
    if isinstance(action, Create):
        _note(f"Creating session {action.name}")
        return _exec_claude(["-n", action.name, *extra])
    if isinstance(action, Resume):
        name = names.get(action.session_id)
        _note(f"Resuming session {name or action.session_id}")
        # Re-assert -n on every resume so Claude restores the prompt-box
        # title. Claude derives the displayed title from the last naming
        # event in the transcript; sessions last named by a transitional
        # Claude version end on a legacy ``agent-name`` event that current
        # Claude no longer recognizes, leaving the label blank. Passing -n
        # sets the live title directly, independent of transcript history.
        rename = ["-n", name] if name else []
        return _exec_claude(["--resume", action.session_id, *rename, *extra])
    if isinstance(action, Fork):
        _note(f"Forking session {names.get(action.session_id, action.session_id)} -> {action.name}")
        return _exec_claude(
            ["--resume", action.session_id, "--fork-session", "-n", action.name, *extra]
        )
    prompt: PromptStale = action
    choice = _prompt_stale(path, _slot_num(prompt.name), prompt.age_days)
    if choice == "c":
        return 0
    if choice == "f":
        _note(f"Archiving session {prompt.name}")
        _archive_session(prompt.session_id, _now_iso())
        _note(f"Creating session {prompt.name}")
        return _exec_claude(["-n", prompt.name, *extra])
    _note(f"Resuming session {prompt.name}")
    # Re-assert -n on resume (see the Resume branch above).
    return _exec_claude(["--resume", prompt.session_id, "-n", prompt.name, *extra])


def resolve(
    identity: str,
    path: str,
    requested_slot: int | None,
    fork: bool,
    fresh: bool,
    extra: list[str],
    stale_days: int,
    archive_days: int,
    resume_name: str | None = None,
) -> int:
    """Plan, auto-archive stale cold slots, then exec Claude for one identity + path.

    ``resume_name`` short-circuits the slot machinery: it resumes the session with
    that exact display name (an epic-renamed title that does not fit the slot
    scheme), with no staleness sweep or prompt — the caller named the session
    explicitly, so it is resumed as-is.
    """
    names, active, last_active = _read_state(_project_slug(str(Path.cwd())))
    if resume_name is not None:
        return _execute(path, select_by_name(resume_name, names, active, last_active), extra, names)
    slots = build_slots(identity, path, names, active, last_active)
    plan = plan_session(
        identity, path, slots, _now(), stale_days, archive_days, requested_slot, fork, fresh
    )
    _run_sweep(plan.auto_archive)
    return _execute(path, plan.action, extra, names)


def _archived_rows(names: dict[str, str], last_active: dict[str, float]) -> list[dict[str, object]]:
    """Rows for archived sessions, parsed from ``archived@`` labels."""
    out: list[dict[str, object]] = []
    for session_id, name in names.items():
        parsed = parse_archived(name)
        if parsed is None:
            continue
        timestamp, original = parsed
        slot = parse_name(original)
        if slot is None:
            continue
        identity, num, path = slot
        out.append(
            {
                "identity": identity,
                "slot": num,
                "path": path,
                "sessionId": session_id,
                "state": "archived",
                "archivedAt": timestamp,
                "lastActive": last_active.get(session_id),
            }
        )
    return out


def list_json() -> int:
    """Print every named session's state as JSON (for ``list --sessions``)."""
    names, active, last_active = _read_state()
    rows: list[dict[str, object]] = [
        {
            "identity": row.identity,
            "slot": row.slot,
            "path": row.path,
            "sessionId": row.session_id,
            "state": "active" if row.active else "idle",
            "lastActive": row.last_active,
        }
        for row in list_rows(names, active, last_active)
    ]
    rows.extend(_archived_rows(names, last_active))
    print(json.dumps(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vrg-vm-resolve-session")
    parser.add_argument("--identity")
    parser.add_argument("--path")
    parser.add_argument("--slot", type=int)
    parser.add_argument("--fork", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--resume-name", dest="resume_name", default=None)
    parser.add_argument("--stale-days", type=int, default=7, dest="stale_days")
    parser.add_argument("--archive-days", type=int, default=14, dest="archive_days")
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
        args.slot,
        args.fork,
        args.fresh,
        extra,
        args.stale_days,
        args.archive_days,
        args.resume_name,
    )


if __name__ == "__main__":
    sys.exit(main())
