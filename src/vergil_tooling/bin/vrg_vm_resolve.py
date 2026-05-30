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
    Refuse,
    Resume,
    build_slots,
    list_rows,
    select,
)


def _claude_dir() -> Path:
    return Path.home() / ".claude"


def _last_agent_name(transcript: Path) -> str | None:
    """Return the last ``agent-name`` value in a transcript, or ``None``."""
    last: str | None = None
    try:
        with transcript.open() as fh:
            for raw in fh:
                line = raw.strip()
                if not line or '"agent-name"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "agent-name":
                    value = entry.get("agentName")
                    if isinstance(value, str):
                        last = value
    except OSError:
        return None
    return last


def name_by_session(projects_dir: Path) -> dict[str, str]:
    """Map session id (transcript stem) to its current name."""
    result: dict[str, str] = {}
    if not projects_dir.is_dir():
        return result
    for transcript in sorted(projects_dir.glob("*/*.jsonl")):
        name = _last_agent_name(transcript)
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


def _read_state() -> tuple[dict[str, str], set[str], dict[str, float]]:
    cdir = _claude_dir()
    projects = cdir / "projects"
    names = name_by_session(projects)
    roster = read_roster(cdir / "sessions")
    active = active_session_ids(roster)
    last_active = _roster_updated_at(roster)
    for sid in names:
        if sid not in last_active:
            ts = _last_activity(projects_glob(projects, sid))
            if ts is not None:
                last_active[sid] = ts
    return names, active, last_active


def _exec_claude(args: list[str]) -> int:
    os.execvp("claude", ["claude", *args])  # noqa: S606, S607
    return 0  # reached only when execvp is stubbed (tests)


def resolve(
    identity: str,
    path: str,
    requested_slot: int | None,
    fork: bool,
    extra: list[str],
) -> int:
    """Classify, decide, and exec Claude for one identity + path."""
    names, active, _last = _read_state()
    slots = build_slots(identity, path, names, active)
    decision = select(identity, path, slots, requested_slot, fork)
    if isinstance(decision, Refuse):
        print(f"ERROR: {decision.message}", file=sys.stderr)
        return 1
    if isinstance(decision, Create):
        return _exec_claude(["-n", decision.name, *extra])
    if isinstance(decision, Resume):
        return _exec_claude(["--resume", decision.session_id, *extra])
    fork_decision: Fork = decision
    return _exec_claude(
        [
            "--resume",
            fork_decision.session_id,
            "--fork-session",
            "-n",
            fork_decision.name,
            *extra,
        ]
    )


def list_json() -> int:
    """Print every named session's state as JSON (for ``list --sessions``)."""
    names, active, _last = _read_state()
    rows = [
        {
            "identity": r.identity,
            "slot": r.slot,
            "path": r.path,
            "sessionId": r.session_id,
            "active": r.active,
        }
        for r in list_rows(names, active)
    ]
    print(json.dumps(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vrg-vm-resolve-session")
    parser.add_argument("--identity")
    parser.add_argument("--path")
    parser.add_argument("--slot", type=int)
    parser.add_argument("--fork", action="store_true")
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
    return resolve(args.identity, args.path, args.slot, args.fork, extra)


if __name__ == "__main__":
    sys.exit(main())
