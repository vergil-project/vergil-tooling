#!/usr/bin/env python3
"""Empirically probe the installed claude-agent-sdk session-management surface.

Task 0 (Stage 0) of epic vergil-project/.github#230. This is an *informational*
spike: it reports what the installed SDK ACTUALLY exposes so a human can write a
GO/NO-GO verdict for a future ``SdkStore`` backend (T8). It never assumes a
function name exists — it enumerates the real public surface and probes it.

What it checks:

1. ``claude --version`` (the interactive CLI in this environment).
2. Whether ``claude_agent_sdk`` imports at all (a bare finding if it does not).
3. The real public attributes of the module (``dir()``), and which
   session-management capabilities are actually present (enumerate / info /
   resolve-name / rename / tag / delete / fork).
4. If a listing function exists: it is called against the real on-disk session
   store, the returned metadata fields are reported, and — when run where real
   interactive transcripts live — whether interactively-launched sessions
   appear (this is the open question: the SDK's ``query()`` is headless, our
   ``vrg-vm`` sessions are interactive TTYs).
5. Whether name->id resolution is possible from the listed metadata.
6. Whether a *detached* session can be renamed — proven by an actual
   rename round-trip inside a throwaway ``CLAUDE_CONFIG_DIR`` so no real
   transcript is touched.

Run it two ways (see the plan):
    vrg-container-run -- python scripts/dev/probe-claude-sessions.py
    # and in a real VM / host session, where interactive transcripts exist:
    python scripts/dev/probe-claude-sessions.py

The SDK is deliberately NOT a project dependency; the import is dynamic so this
script adds no dependency and degrades to a clean finding when absent.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Candidate capability names to probe for. We do NOT assume these exist; we
# report, per capability, which candidates are actually present on the module.
CAPABILITY_CANDIDATES: dict[str, list[str]] = {
    "enumerate (list sessions)": ["list_sessions", "list_sessions_from_store"],
    "session info (by id)": ["get_session_info", "get_session_info_from_store"],
    "rename (detached)": ["rename_session", "rename_session_via_store"],
    "tag / organize": ["tag_session", "tag_session_via_store"],
    "delete": ["delete_session", "delete_session_via_store"],
    "fork": ["fork_session", "fork_session_via_store"],
    "project-key derivation": ["project_key_for_directory"],
}


def rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def probe_cli_version() -> None:
    rule("1. claude --version (interactive CLI in this environment)")
    exe = shutil.which("claude")
    if exe is None:
        print("  [observed] `claude` is NOT on PATH here.")
        print("  (Expected inside the dev container; present on the host/VM.)")
        return
    try:
        out = subprocess.run(  # noqa: S603
            [exe, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  [observed] could not run `claude --version`: {exc}")
        return
    print(f"  [observed] {(out.stdout or out.stderr).strip()}")


def import_sdk() -> Any | None:
    rule("2. import claude_agent_sdk")
    try:
        sdk = importlib.import_module("claude_agent_sdk")
    except ImportError as exc:
        print("  [observed] claude_agent_sdk is NOT importable in this environment.")
        print(f"             ({exc})")
        print("  It is published on PyPI (`pip install claude-agent-sdk`); it is")
        print("  deliberately NOT a vergil-tooling dependency. Absence here is a")
        print("  finding about THIS environment, not about the SDK's surface.")
        return None
    version = getattr(sdk, "__version__", "<no __version__>")
    print(f"  [observed] imported OK, version = {version}")
    print(f"  [observed] file = {getattr(sdk, '__file__', '?')}")
    return sdk


def probe_public_surface(sdk: Any) -> None:
    rule("3. Public surface (dir) and session-management capabilities")
    public = [n for n in dir(sdk) if not n.startswith("_")]
    session_related = [n for n in public if "session" in n.lower()]
    print(f"  [observed] {len(public)} public attributes; "
          f"{len(session_related)} mention 'session':")
    for name in session_related:
        print(f"      - {name}")
    print("\n  Capability probe (which candidate names actually exist):")
    for capability, candidates in CAPABILITY_CANDIDATES.items():
        present = [c for c in candidates if hasattr(sdk, c)]
        absent = [c for c in candidates if not hasattr(sdk, c)]
        verdict = "PRESENT" if present else "ABSENT"
        print(f"      [{verdict:7}] {capability}")
        if present:
            print(f"                  found:  {', '.join(present)}")
        if absent:
            print(f"                  no:     {', '.join(absent)}")


def _fields_of(obj: Any) -> dict[str, Any]:
    ann = getattr(type(obj), "__annotations__", None)
    if ann:
        return {k: getattr(obj, k, None) for k in ann}
    return {k: v for k, v in vars(obj).items()} if hasattr(obj, "__dict__") else {}


def probe_enumeration(sdk: Any) -> list[Any]:
    rule("4. Enumerate real on-disk sessions (interactive-visibility test)")
    lister = getattr(sdk, "list_sessions", None)
    if lister is None:
        print("  [observed] no list_sessions function — cannot enumerate.")
        return []
    try:
        sessions = list(lister())  # all projects
    except Exception as exc:  # noqa: BLE001 - report, never mask
        print(f"  [observed] list_sessions() raised: {exc!r}")
        return []
    print(f"  [observed] list_sessions() returned {len(sessions)} session(s) "
          f"from the real on-disk store (~/.claude/projects, honoring "
          f"CLAUDE_CONFIG_DIR).")
    if not sessions:
        print("  [observed] Zero sessions here. If run in the empty container this")
        print("             only confirms it reads the real store (which is empty).")
        print("             The interactive-visibility question must be answered by")
        print("             running on the host/VM where real transcripts exist.")
        return sessions

    fields = list(_fields_of(sessions[0]).keys())
    print(f"  [observed] per-session metadata fields: {fields}")

    def last_active(s: Any) -> float:
        return float(getattr(s, "last_modified", 0) or 0)

    newest = sorted(sessions, key=last_active, reverse=True)[:10]
    print("\n  [observed] 10 most-recently-active sessions:")
    now = time.time()
    for s in newest:
        sid = str(getattr(s, "session_id", "?"))[:8]
        title = getattr(s, "custom_title", None)
        tag = getattr(s, "tag", None)
        cwd = getattr(s, "cwd", None)
        lm = last_active(s)
        age_min = (now - lm / 1000.0) / 60.0 if lm > 1e11 else (now - lm) / 60.0
        print(f"      id={sid}  title={title!r}  tag={tag!r}")
        print(f"                cwd={cwd}  age~{age_min:.1f} min")

    named = [s for s in sessions if getattr(s, "custom_title", None)]
    print(f"\n  [observed] {len(named)}/{len(sessions)} sessions carry a "
          f"custom_title (the supported name set via -n / /rename).")
    return sessions


def probe_name_resolution(sessions: list[Any]) -> None:
    rule("5. name -> session_id resolution feasibility")
    if not sessions:
        print("  [inferred] No sessions listed here to demonstrate on, but the")
        print("             metadata schema (custom_title + session_id per row)")
        print("             makes resolution a pure client-side filter.")
        return
    index: dict[str, list[str]] = {}
    for s in sessions:
        title = getattr(s, "custom_title", None)
        if title:
            index.setdefault(title, []).append(str(getattr(s, "session_id", "?")))
    print(f"  [observed] Built a name->id index over {len(index)} distinct "
          f"custom_title value(s) purely from list_sessions() output.")
    collisions = {k: v for k, v in index.items() if len(v) > 1}
    if collisions:
        print(f"  [observed] {len(collisions)} name(s) map to >1 session — exactly "
              f"the ambiguity the seam's resolve_over() must arbitrate.")
    example = next(iter(index.items()), None)
    if example:
        name, ids = example
        print(f"  [observed] e.g. {name!r} -> {[i[:8] for i in ids]}")
    print("  [inferred] name->id resolution is FEASIBLE client-side; no dedicated")
    print("             resolver call is needed (nor does the SDK offer one).")


def probe_detached_rename(sdk: Any) -> None:
    rule("6. Rename a DETACHED session (real round-trip in a temp store)")
    renamer = getattr(sdk, "rename_session", None)
    lister = getattr(sdk, "list_sessions", None)
    if renamer is None or lister is None:
        print("  [observed] rename_session and/or list_sessions absent — "
              "cannot round-trip.")
        return

    # Build a throwaway CLAUDE_CONFIG_DIR with one minimal session transcript so
    # we exercise a real rename without touching any real interactive session.
    tmp = Path(tempfile.mkdtemp(prefix="probe-claude-"))
    saved = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
        importlib.reload(sdk)  # re-read the config dir from env
        work = Path.cwd()
        key_fn = getattr(sdk, "project_key_for_directory", None)
        project_key = key_fn(str(work)) if key_fn else "probe-project"
        proj_dir = tmp / "projects" / project_key
        proj_dir.mkdir(parents=True, exist_ok=True)
        sid = "00000000-0000-4000-8000-000000000001"
        transcript = proj_dir / f"{sid}.jsonl"
        first = {
            "type": "user",
            "sessionId": sid,
            "cwd": str(work),
            "uuid": "u-1",
            "parentUuid": None,
            "timestamp": "2026-08-07T00:00:00.000Z",
            "message": {"role": "user", "content": "probe seed message"},
        }
        transcript.write_text(json.dumps(first) + "\n", encoding="utf-8")

        before = {
            getattr(s, "session_id", None): getattr(s, "custom_title", None)
            for s in lister(directory=str(work))
        }
        print(f"  [observed] seeded 1 detached session; custom_title before = "
              f"{before.get(sid)!r}")

        new_title = "epic-230:probe-detached-rename"
        renamer(sid, new_title, directory=str(work))

        after = {
            getattr(s, "session_id", None): getattr(s, "custom_title", None)
            for s in lister(directory=str(work))
        }
        got = after.get(sid)
        print(f"  [observed] called rename_session(id, {new_title!r}); "
              f"custom_title after = {got!r}")
        if got == new_title:
            print("  [observed] RENAME ROUND-TRIP SUCCEEDED on a detached session "
                  "(no live process needed).")
        else:
            print("  [observed] rename did NOT take effect as expected — see values "
                  "above.")
    except Exception as exc:  # noqa: BLE001 - report, never mask
        print(f"  [observed] rename round-trip raised: {exc!r}")
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = saved
        importlib.reload(sdk)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("Claude session-management surface probe")
    print(f"python={sys.version.split()[0]}  cwd={Path.cwd()}")
    probe_cli_version()
    sdk = import_sdk()
    if sdk is None:
        rule("SUMMARY")
        print("  claude_agent_sdk not installed here — no surface to probe.")
        print("  Re-run where it is installed (or `pip install claude-agent-sdk`)")
        print("  to enumerate the real functions before writing the verdict.")
        return 0
    probe_public_surface(sdk)
    sessions = probe_enumeration(sdk)
    probe_name_resolution(sessions)
    probe_detached_rename(sdk)
    rule("SUMMARY")
    print("  Surface enumerated above. Fold the [observed] lines into")
    print("  docs/claude-session-surface.md and write the GO/NO-GO verdict.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
