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

Fails closed: inside a managed repo, input the guard cannot check
(unreadable or empty stdin, invalid JSON, a non-object payload, or a
missing ``tool_input.command`` string) is refused with Claude Code's
blocking exit code 2 and a stderr message naming the problem, rather
than let through unchecked.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, cast

from vergil_tooling.lib.cli import cli_main

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


# Claude Code's blocking exit code for a PreToolUse hook: the tool call is
# refused and stderr is fed back to the agent.
_BLOCKING_EXIT = 2


class _MalformedInputError(Exception):
    """The hook input cannot be checked; the message names the problem."""


@cli_main
def main() -> int:
    try:
        data = _load_input()
    except _MalformedInputError as exc:
        # Unparseable input names no cwd, so scope falls back to the hook
        # process's own working directory (Claude Code runs hooks there).
        if _find_vergil_toml(Path()) is None:
            return 0
        return _fail_closed(str(exc))

    if _find_vergil_toml(_scope_dir(data)) is None:
        return 0

    if _is_other_tool(data):
        return 0

    try:
        command = _command_of(data)
    except _MalformedInputError as exc:
        return _fail_closed(str(exc))
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


def _load_input() -> dict[str, Any]:
    """Read and parse the hook JSON from stdin, or raise naming the problem."""
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"could not read the hook input from stdin ({exc})"
        raise _MalformedInputError(msg) from exc
    if not raw.strip():
        msg = "the hook input on stdin is empty"
        raise _MalformedInputError(msg)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"the hook input is not valid JSON ({exc})"
        raise _MalformedInputError(msg) from exc
    if not isinstance(data, dict):
        msg = f"the hook input is not a JSON object (got {type(data).__name__})"
        raise _MalformedInputError(msg)
    return cast("dict[str, Any]", data)


def _scope_dir(data: dict[str, Any]) -> Path:
    """The directory whose ancestry decides whether the guard applies."""
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        inner = tool_input.get("cwd")
        if isinstance(inner, str) and inner:
            return Path(inner)
    outer = data.get("cwd")
    if isinstance(outer, str) and outer:
        return Path(outer)
    return Path()


def _is_other_tool(data: dict[str, Any]) -> bool:
    """True for a well-formed payload naming a tool other than Bash."""
    tool_name = data.get("tool_name")
    return isinstance(tool_name, str) and tool_name != "Bash"


def _command_of(data: dict[str, Any]) -> str:
    """The Bash command to check, or raise naming the structural problem."""
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        msg = "the hook input has no tool_input object"
        raise _MalformedInputError(msg)
    command = tool_input.get("command")
    if not isinstance(command, str):
        msg = "the hook input has no tool_input.command string"
        raise _MalformedInputError(msg)
    return command


def _fail_closed(problem: str) -> int:
    print(
        f"vrg-hook-guard: blocked: {problem}. The guard cannot check this "
        "call for raw git/gh, so it fails closed and refuses it. This is a "
        "hook-protocol problem, not a problem with the command itself.",
        file=sys.stderr,
    )
    return _BLOCKING_EXIT


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
