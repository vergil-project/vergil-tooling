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

For agent identities it also refuses the obvious raw ``vrg-gh`` reads of
issue and PR content (epic vergil-project/.github#1467, spec §6.4), naming
``vrg-issue-read``, the trusted read command, as the replacement:

- ``--comments`` on any ``issue`` or ``pr`` command;
- ``--json`` selecting ``body``, ``comments``, ``reviews``,
  ``latestReviews`` or ``closingIssuesReferences`` on an ``issue``, ``pr``
  or ``search issues|prs`` command;
- an ``issue view`` or ``pr view`` with no ``--json``, whose default output
  prints the body;
- an ``api`` path with a ``comments`` or ``reviews`` segment;
- every ``issue list`` and ``pr list``;
- a ``search issues`` or ``search prs`` whose query matches text (a
  free-text term, an ``in:`` qualifier or ``--match``), so that content
  picks the results. A query of qualifiers only (labels, states, dates,
  authors) is a metadata filter and stays allowed, as do metadata reads
  such as ``issue view --json labels,parent,state`` and run and release
  listings.

It is a blocklist against accidents, not a security boundary: forms it cannot
see (an invocation not at command position, a GraphQL query, a command built
at runtime) are outside it. It finds ``vrg-gh`` by the same command-position
rule, on the command's shell words, so quoted text never counts as a command,
and it reads the arguments unquoted, so a quoted ``--json`` value still counts.
Human mode is unchanged: the check applies only when an agent identity signal
is present (:func:`identity_mode.any_agent_signal`, which a
``VRG_IDENTITY_MODE=human`` value cannot override).

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

from vergil_tooling.lib import identity_mode
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


_CONTENT_READ_REMEDY = (
    "Agents read issue and PR content only through vrg-issue-read, which "
    "keeps only trusted content: `vrg-issue-read OWNER/REPO#N [--pr] [--json]` "
    "for one issue or PR, or `vrg-issue-read (--repo OWNER/REPO | --owner ORG) "
    "[--search QUERY] [--pr] [--state STATE] [--json]` for a list or search. "
    "Metadata reads (`--json` with fields such as labels, parent or state, "
    "and searches by qualifiers only) stay available through vrg-gh."
)

# The ``--json`` fields that carry issue or PR content, or links read from it.
_CONTENT_FIELDS = frozenset(
    {"body", "comments", "reviews", "latestReviews", "closingIssuesReferences"},
)

# Characters that end a simple command: the canonical anchor's ``;&|(``,
# plus ``)`` and a newline.
_SEPARATORS = frozenset(";&|()\n")

# ``gh search issues|prs`` flags that take no value; every other flag is
# taken to consume the next word. A boolean flag missing here can only hide a
# query term (a false negative), never refuse a read.
_SEARCH_BOOL_FLAGS = frozenset(
    {
        "--archived",
        "--draft",
        "--help",
        "--include-prs",
        "--locked",
        "--merged",
        "--no-assignee",
        "--no-label",
        "--no-milestone",
        "--no-project",
        "--web",
        "-h",
        "-w",
    },
)

# ``gh api`` flags that take no value.
_API_BOOL_FLAGS = frozenset({"--include", "--paginate", "--silent", "--slurp", "--verbose", "-i"})

# A GitHub search qualifier (``label:x``, ``is:open``, ``in:title``).
_QUALIFIER_RE = re.compile(r"^([A-Za-z][A-Za-z_-]*):")
# One GitHub search term: a double-quoted phrase or a run of non-space.
_QUERY_TERM_RE = re.compile(r'"[^"]*"?|\S+')
_QUERY_OPERATORS = frozenset({"AND", "OR", "NOT"})

# A backslash escape inside a double-quoted shell span.
_DQ_ESCAPE_RE = re.compile(r'\\([\\"$`])')


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

    form = _content_read(command)
    if form is not None:
        try:
            agent = identity_mode.any_agent_signal()
        except (OSError, UnicodeDecodeError) as exc:
            _deny(
                f"vrg-hook-guard cannot read the identity signals ({exc}), so it "
                f"refuses this raw content read: {form}. {_CONTENT_READ_REMEDY}"
            )
            return 0
        if agent:
            _deny(f"{form} reads raw issue or PR content. {_CONTENT_READ_REMEDY}")
    return 0


def _content_read(command: str) -> str | None:
    """Describe the first raw ``vrg-gh`` content read in *command*, or ``None``."""
    for simple in _simple_commands(command):
        words = simple
        while words and words[0] in ("{", "}"):
            words = words[1:]
        if not words:
            continue
        if words[0] in ("bash", "sh") and len(words) > 2 and words[1] == "-c":  # noqa: PLR2004
            form = _content_read(words[2])
        elif words[0] == "vrg-gh":
            form = _vrg_gh_content_read(words[1:])
        else:
            form = None
        if form is not None:
            return form
    return None


def _simple_commands(text: str) -> list[list[str]]:
    """Split *text* into simple commands, each a list of unquoted shell words.

    A small shell lexer. Single quotes, double quotes (backslash-escaped),
    backslash escapes and line continuations are honoured, and ``;``, ``&``,
    ``|``, ``(``, ``)`` and a newline outside quotes end a command. An
    unbalanced quote is an ordinary character, as the canonical quote-strip
    leaves it unstripped. Backticks are not separators (an accepted gap, as
    for raw ``git``).
    """
    commands: list[list[str]] = []
    words: list[str] = []
    word: list[str] = []
    in_word = False
    i = 0
    while i < len(text):
        ch = text[i]
        span = _quoted_span(text, i)
        if span is not None:
            content, i = span
            word.append(content)
            in_word = True
            continue
        if ch == "\\" and i + 1 < len(text):
            if text[i + 1] != "\n":
                word.append(text[i + 1])
                in_word = True
            i += 2
            continue
        if ch in _SEPARATORS or ch in " \t\r":
            if in_word:
                words.append("".join(word))
                word.clear()
                in_word = False
            if ch in _SEPARATORS:
                commands.append(words)
                words = []
        else:
            word.append(ch)
            in_word = True
        i += 1
    if in_word:
        words.append("".join(word))
    commands.append(words)
    return [c for c in commands if c]


def _quoted_span(text: str, i: int) -> tuple[str, int] | None:
    """The unquoted content of a balanced quoted span at *i*, and its end."""
    if text[i] not in "'\"":
        return None
    span = _QUOTED_STR_RE.match(text, i)
    if span is None:
        return None
    inner = span.group(0)[1:-1]
    if text[i] == '"':
        inner = _DQ_ESCAPE_RE.sub(r"\1", inner.replace("\\\n", ""))
    return inner, span.end()


def _vrg_gh_content_read(args: list[str]) -> str | None:
    """Describe the raw content read in ``vrg-gh <args>``, or ``None``."""
    if len(args) < 2:  # noqa: PLR2004
        return None
    top, sub, rest = args[0], args[1], args[2:]
    if _has_flag(args, "--help") or "-h" in args:
        return None
    if top == "api":
        return _api_content_read(args[1:])
    if top in ("issue", "pr"):
        return _item_content_read(top, sub, rest)
    if top == "search" and sub in ("issues", "prs"):
        return _search_content_read(sub, rest)
    return None


def _item_content_read(top: str, sub: str, rest: list[str]) -> str | None:
    """Describe the content read in ``vrg-gh issue|pr <sub> <rest>``, or ``None``."""
    if _has_flag(rest, "--comments"):
        return f"`vrg-gh {top} {sub} --comments`"
    field = _content_field(rest)
    if field is not None:
        return f"`vrg-gh {top} {sub} --json {field}`"
    if sub == "view" and not _has_flag(rest, "--json"):
        return f"`vrg-gh {top} view` without `--json` (its default output prints the body)"
    if sub in ("list", "ls"):
        return f"`vrg-gh {top} {sub}`"
    return None


def _search_content_read(sub: str, rest: list[str]) -> str | None:
    """Describe the content read in ``vrg-gh search issues|prs <rest>``, or ``None``."""
    field = _content_field(rest)
    if field is not None:
        return f"`vrg-gh search {sub} --json {field}`"
    if _has_flag(rest, "--match"):
        return f"`vrg-gh search {sub} --match`"
    term = _text_query_term(rest)
    if term is not None:
        return f"`vrg-gh search {sub}` with a query matching text ({term})"
    return None


def _has_flag(args: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(f"{flag}=") for a in args)


def _content_field(args: list[str]) -> str | None:
    """The first content field a ``--json`` in *args* selects, or ``None``."""
    for i, arg in enumerate(args):
        if arg == "--json" and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith("--json="):
            value = arg.removeprefix("--json=")
        else:
            continue
        for field in value.split(","):
            if field.strip() in _CONTENT_FIELDS:
                return field.strip()
    return None


def _positionals(args: list[str], bool_flags: frozenset[str]) -> list[str]:
    """The positional words of *args*; a flag not in *bool_flags* takes a value."""
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            out.extend(args[i + 1 :])
            break
        if arg.startswith("-") and len(arg) > 1:
            i += 1 if ("=" in arg or arg in bool_flags) else 2
            continue
        out.append(arg)
        i += 1
    return out


def _text_query_term(args: list[str]) -> str | None:
    """Name the first query term of a search that matches text, or ``None``."""
    for positional in _positionals(args, _SEARCH_BOOL_FLAGS):
        for raw in _QUERY_TERM_RE.findall(positional):
            term = raw.strip("()").removeprefix("-")
            if not term or term in _QUERY_OPERATORS:
                continue
            qualifier = _QUALIFIER_RE.match(term)
            if qualifier is None:
                return "a free-text term"
            if qualifier.group(1).lower() == "in":
                return "an `in:` qualifier"
    return None


def _api_content_read(args: list[str]) -> str | None:
    """Describe a ``vrg-gh api`` read of comments or reviews, or ``None``."""
    positionals = _positionals(args, _API_BOOL_FLAGS)
    if not positionals:
        return None
    path = positionals[0].split("?", 1)[0]
    segments = [s for s in path.split("/") if s]
    for segment in ("comments", "reviews"):
        if segment in segments:
            return f"`vrg-gh api` on a `{segment}` path"
    return None


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
