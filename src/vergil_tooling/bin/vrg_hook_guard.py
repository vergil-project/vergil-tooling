"""Claude Code PreToolUse hook guard for Vergil-managed repos.

Blocks raw ``git`` and ``gh`` commands, directing agents to use
``vrg-git`` and ``vrg-gh`` wrappers instead.  Reads Claude Code
hook JSON from stdin and outputs a deny decision on stdout when
a raw invocation is detected.

Gated on managed-repo detection: no-op in repos without
``vergil.toml``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_RAW_GIT_RE = re.compile(
    r"(?<![a-zA-Z0-9_-])"
    r"git"
    r"(?:\s|$)",
)

_RAW_GH_RE = re.compile(
    r"(?<![a-zA-Z0-9_-])"
    r"gh"
    r"(?:\s|$)",
)

_QUOTED_STR_RE = re.compile(r""""[^"]*"|'[^']*'""")

_SH_EXEC_RE = re.compile(r"(?:^|\s)(?:bash|sh)\s+-c\s")


def _find_vergil_toml(start: Path) -> Path | None:
    current = start.resolve()
    for parent in (current, *current.parents):
        candidate = parent / "vergil.toml"
        if candidate.exists():
            return candidate
    return None


def _deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        },
        sys.stdout,
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    cwd = data.get("tool_input", {}).get("cwd") or data.get("cwd", ".")
    if _find_vergil_toml(Path(cwd)) is None:
        return 0

    tool_input = data.get("tool_input")
    if not tool_input:
        return 0

    command = tool_input.get("command", "")
    if not command:
        return 0

    stripped = _QUOTED_STR_RE.sub('""', command)

    if _RAW_GIT_RE.search(stripped):
        _deny(
            "Raw git is blocked in Vergil-managed repos. "
            "Use vrg-git instead. All git operations must go "
            "through the vrg-git wrapper."
        )
        return 0

    if _RAW_GH_RE.search(stripped):
        _deny(
            "Raw gh is blocked in Vergil-managed repos. "
            "Use vrg-gh instead. All GitHub CLI operations must "
            "go through the vrg-gh wrapper."
        )
        return 0

    if _SH_EXEC_RE.search(stripped):
        if _RAW_GIT_RE.search(command):
            _deny(
                "Raw git is blocked in Vergil-managed repos. "
                "Use vrg-git instead. All git operations must go "
                "through the vrg-git wrapper."
            )
            return 0

        if _RAW_GH_RE.search(command):
            _deny(
                "Raw gh is blocked in Vergil-managed repos. "
                "Use vrg-gh instead. All GitHub CLI operations must "
                "go through the vrg-gh wrapper."
            )
            return 0

    return 0
