"""Safe git wrapper for AI agent sessions.

Enforces a subcommand allowlist and flag deny lists.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import github

_ALLOWED_SIMPLE: set[str] = {
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "ls-remote",
    "rev-parse",
    "add",
    "mv",
    "rm",
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


_REMOTE_SUBCOMMANDS: set[str] = {"push", "pull", "fetch", "ls-remote"}

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
            if arg == "--force-with-lease" and _is_protected_branch():
                return "push --force-with-lease is denied on a protected branch."
        return None

    if subcmd == "branch":
        for arg in args:
            if arg == "--force":
                return "branch --force is denied by vrg-git."
            if arg == "-D":
                idx = args.index("-D")
                if idx + 1 >= len(args):
                    return "branch -D is denied by vrg-git."
                branch_name = args[idx + 1]
                if not _is_upstream_gone(branch_name):
                    return f"branch -D is denied (upstream is not gone for {branch_name})."
        return None

    for arg in args:
        if arg in denied_flags:
            return f"{subcmd} {arg} is denied by vrg-git."
    return None


_BRANCH_SWITCH_SUBCOMMANDS: set[str] = {"checkout", "switch"}


def _is_main_worktree() -> bool:
    git_path = Path(".git")
    return git_path.is_dir()


def _worktree_convention_active() -> bool:
    return Path(".worktrees").is_dir()


def _parse_branch_target(subcmd: str, args: list[str]) -> str | None:
    if subcmd == "checkout":
        after_separator = False
        for arg in args:
            if arg == "--":
                after_separator = True
                continue
            if after_separator:
                return None
            if arg.startswith("-"):
                continue
            return arg
        return None

    if subcmd == "switch":
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                return arg
            if arg in ("-c", "-C"):
                skip_next = True
                continue
            if arg.startswith("-"):
                continue
            return arg
        return None

    return None


def _check_worktree_convention(subcmd: str, args: list[str]) -> str | None:
    if subcmd not in _BRANCH_SWITCH_SUBCOMMANDS:
        return None
    if not _is_main_worktree() or not _worktree_convention_active():
        return None
    target = _parse_branch_target(subcmd, args)
    if target is None:
        return None
    if target in _PROTECTED_BRANCHES:
        return None
    return (
        "Branch switches in the main worktree are blocked. "
        "Use a worktree under .worktrees/ instead."
    )


def _git_auth_env(token: str) -> dict[str, str]:
    """Return env dict that authenticates HTTPS git to GitHub."""
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("usage: vrg-git <subcommand> [args...]", file=sys.stderr)
        return 2

    subcmd = argv[0]

    if subcmd in _DENIED:
        msg = _DENIED[subcmd]
        print(f"vrg-git: {subcmd} is denied. {msg}", file=sys.stderr)
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
            return 1

        flag_err = _check_denied_flags(subcmd, argv[1:])
        if flag_err:
            print(f"vrg-git: {flag_err}", file=sys.stderr)
            return 1

        result = subprocess.run(["git", *argv], check=False)  # noqa: S603, S607
        return result.returncode

    if subcmd not in _ALLOWED_SIMPLE:
        print(
            f"vrg-git: {subcmd} is not recognized. "
            f"Allowed: {', '.join(sorted(_ALLOWED_SIMPLE | set(_ALLOWED_COMPOUND)))}",
            file=sys.stderr,
        )
        return 1

    flag_err = _check_denied_flags(subcmd, argv[1:])
    if flag_err:
        print(f"vrg-git: {flag_err}", file=sys.stderr)
        return 1

    worktree_err = _check_worktree_convention(subcmd, argv[1:])
    if worktree_err:
        print(f"vrg-git: {worktree_err}", file=sys.stderr)
        return 1

    env = None
    if subcmd in _REMOTE_SUBCOMMANDS:
        token = github.get_installation_token()
        if token is not None:
            env = _git_auth_env(token)
    result = subprocess.run(["git", *argv], check=False, env=env)  # noqa: S603, S607
    return result.returncode
