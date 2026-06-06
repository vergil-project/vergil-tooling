"""Claude Code PreToolUse hook guard for Vergil-managed repos.

Blocks raw ``git`` and ``gh`` commands, directing agents to use
``vrg-git`` and ``vrg-gh`` wrappers instead.  Reads Claude Code
hook JSON from stdin and outputs a deny decision on stdout when
a raw invocation is detected.

Matching follows the canonical quote-strip + command-position rule
shared with the vergil-claude-plugin hook scripts (spec:
vergil-claude-plugin
``docs/specs/2026-06-05-450-command-matcher-quoting-design.md``):
quoted spans are replaced with ``""``, then tool names match only
at command position in the stripped text.

Gated on managed-repo detection: no-op in repos without
``vergil.toml``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Canonical command-position anchor (spec section 2.2): the tool name
# matches only at line start or after a command separator, applied per
# line of the quote-stripped text.
_RAW_GIT_RE = re.compile(r"(?:^|[;&|({]\s*)git(?:\s|$)", re.MULTILINE)

_RAW_GH_RE = re.compile(r"(?:^|[;&|({]\s*)gh(?:\s|$)", re.MULTILINE)

# Canonical quote-stripping alternation (spec section 2.1): an
# escape-aware double-quoted span or a single-quoted span, combined as
# one alternation so leftmost-match-wins mirrors shell scanning.
_QUOTED_STR_RE = re.compile(r"\"(?:\\.|[^\"\\])*\"|'[^']*'")

_SH_EXEC_RE = re.compile(r"(?:^|\s)(?:bash|sh)\s+-c\s")

# The quoted span immediately following ``bash -c``/``sh -c`` in the
# raw command text (spec section 4.3): the recheck runs against this
# extracted content only, never the whole raw command.
_SH_EXEC_ARG_RE = re.compile(
    r"(?:^|\s)(?:bash|sh)\s+-c\s+"
    r"(\"(?:\\.|[^\"\\])*\"|'[^']*')"
)

_GIT_DENY_REASON = (
    "Raw git is blocked in Vergil-managed repos. "
    "Use vrg-git instead. All git operations must go "
    "through the vrg-git wrapper."
)

_GH_DENY_REASON = (
    "Raw gh is blocked in Vergil-managed repos. "
    "Use vrg-gh instead. All GitHub CLI operations must "
    "go through the vrg-gh wrapper."
)


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
        _deny(_GIT_DENY_REASON)
        return 0

    if _RAW_GH_RE.search(stripped):
        _deny(_GH_DENY_REASON)
        return 0

    if _SH_EXEC_RE.search(stripped):
        for match in _SH_EXEC_ARG_RE.finditer(command):
            arg = match.group(1)[1:-1]
            if _RAW_GIT_RE.search(arg):
                _deny(_GIT_DENY_REASON)
                return 0
            if _RAW_GH_RE.search(arg):
                _deny(_GH_DENY_REASON)
                return 0

    return 0
