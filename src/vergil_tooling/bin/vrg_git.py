"""Safe git wrapper for AI agent sessions.

Enforces a subcommand allowlist, flag deny lists, and logs every
invocation to a JSON-lines audit file.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

_ALLOWED_SIMPLE: set[str] = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "ls-remote",
    "rev-parse",
    "add",
    "push",
    "fetch",
    "pull",
    "checkout",
    "switch",
    "stash",
    "merge",
    "cherry-pick",
    "rebase",
}

_ALLOWED_COMPOUND: dict[str, set[str]] = {
    "worktree": {"add", "list", "remove"},
}

_ALLOWED_EXACT: set[tuple[str, ...]] = {
    ("config", "core.hooksPath", ".githooks"),
}

_DENIED: dict[str, str] = {
    "commit": "Use vrg-commit instead of git commit.",
    "reset": "git reset is denied by vrg-git.",
    "clean": "git clean is denied by vrg-git.",
    "config": "git config is denied by vrg-git.",
    "remote": "git remote is denied by vrg-git.",
    "reflog": "git reflog is denied by vrg-git.",
    "gc": "git gc is denied by vrg-git.",
    "prune": "git prune is denied by vrg-git.",
    "filter-branch": "git filter-branch is denied by vrg-git.",
    "replace": "git replace is denied by vrg-git.",
}

_FLAG_DENY: dict[str, set[str]] = {
    "branch": {"-D", "--force"},
    "push": {"-f", "--force", "--force-with-lease"},
    "checkout": {".", "*"},
    "rebase": {"-i", "--interactive"},
}


_PROTECTED_BRANCHES: set[str] = {"develop", "main"}
_PROTECTED_PREFIXES: tuple[str, ...] = ("release/",)


def _is_protected_branch() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S603, S607
        capture_output=True,
        text=True,
        check=False,
    )
    branch = result.stdout.strip()
    if branch in _PROTECTED_BRANCHES:
        return True
    return any(branch.startswith(p) for p in _PROTECTED_PREFIXES)


def _is_upstream_gone(branch_name: str) -> bool:
    result = subprocess.run(
        ["git", "branch", "-vv"],  # noqa: S603, S607
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        stripped = line.lstrip("* ").strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if parts and parts[0] == branch_name:
            return ": gone]" in line
    return False


def _log_path() -> Path:
    return Path.home() / ".local" / "share" / "vergil" / "vrg-git.log"


def _log(args: list[str], result: str) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.datetime.now(tz=datetime.UTC).isoformat(),
        "args": args,
        "result": result,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _check_denied_flags(subcmd: str, args: list[str]) -> str | None:
    denied_flags = _FLAG_DENY.get(subcmd, set())
    if not denied_flags:
        return None

    if subcmd == "checkout":
        after_separator = False
        for arg in args:
            if arg == "--":
                after_separator = True
                continue
            if after_separator and arg in denied_flags:
                return f"checkout -- {arg} is denied by vrg-git."
        return None

    if subcmd == "push":
        for arg in args:
            if arg in ("-f", "--force"):
                return f"push {arg} is denied by vrg-git."
            if arg == "--force-with-lease":
                if _is_protected_branch():
                    return "push --force-with-lease is denied on a protected branch."
        return None

    for arg in args:
        if arg in denied_flags:
            return f"{subcmd} {arg} is denied by vrg-git."
    return None


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("usage: vrg-git <subcommand> [args...]", file=sys.stderr)
        _log([], "denied")
        return 2

    subcmd = argv[0]

    if tuple(argv) in _ALLOWED_EXACT:
        result = subprocess.run(["git", *argv], check=False)  # noqa: S603, S607
        _log(argv, "allowed")
        return result.returncode

    if subcmd in _DENIED:
        msg = _DENIED[subcmd]
        print(f"vrg-git: {subcmd} is denied. {msg}", file=sys.stderr)
        _log(argv, "denied")
        return 1

    if subcmd in _ALLOWED_COMPOUND:
        allowed_subs = _ALLOWED_COMPOUND[subcmd]
        if len(argv) < 2 or argv[1] not in allowed_subs:
            sub = argv[1] if len(argv) >= 2 else "(none)"
            print(
                f"vrg-git: {subcmd} {sub} is not recognized. "
                f"Allowed: {', '.join(sorted(allowed_subs))}",
                file=sys.stderr,
            )
            _log(argv, "denied")
            return 1

        flag_err = _check_denied_flags(subcmd, argv[1:])
        if flag_err:
            print(f"vrg-git: {flag_err}", file=sys.stderr)
            _log(argv, "denied")
            return 1

        result = subprocess.run(["git", *argv], check=False)  # noqa: S603, S607
        _log(argv, "allowed")
        return result.returncode

    if subcmd not in _ALLOWED_SIMPLE:
        print(
            f"vrg-git: {subcmd} is not recognized. "
            f"Allowed: {', '.join(sorted(_ALLOWED_SIMPLE | set(_ALLOWED_COMPOUND)))}",
            file=sys.stderr,
        )
        _log(argv, "denied")
        return 1

    flag_err = _check_denied_flags(subcmd, argv[1:])
    if flag_err:
        print(f"vrg-git: {flag_err}", file=sys.stderr)
        _log(argv, "denied")
        return 1

    result = subprocess.run(["git", *argv], check=False)  # noqa: S603, S607
    _log(argv, "allowed")
    return result.returncode
