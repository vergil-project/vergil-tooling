"""Safe git wrapper for AI agent sessions.

Enforces a subcommand allowlist and flag deny lists.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.git import _git_auth_env

_ALLOWED_SIMPLE: set[str] = {
    # Read-only inspection commands. These only read history, objects, or
    # refs and have no mode that mutates the repository, so they are allowed
    # unconditionally for triage and debugging (#1602).
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "ls-remote",
    "rev-parse",
    "annotate",
    "blame",
    "cat-file",
    "cherry",
    "count-objects",
    "describe",
    "diff-files",
    "diff-index",
    "diff-tree",
    "for-each-ref",
    "grep",
    "ls-files",
    "ls-tree",
    "merge-base",
    "name-rev",
    "rev-list",
    "shortlog",
    "show-branch",
    "show-ref",
    "var",
    "verify-commit",
    "verify-tag",
    "whatchanged",
    # Mutating commands, gated further by _FLAG_DENY and worktree checks.
    "add",
    "mv",
    "rm",
    "push",
    "fetch",
    "pull",
    # A network "create": the off-platform volume bootstrap runs `vrg-git clone
    # <url> <dest>` on the cloud box (vm_cloud.bootstrap_volume). Gated like the other
    # remote ops — identity-token injection via _REMOTE_SUBCOMMANDS. (#1780)
    "clone",
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

# Commands whose default/listing form is read-only but which also expose
# mutating sub-operations. The read-only forms (bare invocation plus any
# sub-operation not listed here) are allowed; the listed sub-operations are
# denied. Used for high-value debugging commands like reflog (#1602).
_READONLY_WITH_DENIED_SUBS: dict[str, set[str]] = {
    "reflog": {"expire", "delete"},
}

_DENIED: dict[str, str] = {
    "commit": "Use vrg-commit instead of git commit.",
    "reset": "git reset is denied by vrg-git.",
    "clean": "git clean is denied by vrg-git.",
    # config has a broad mutating-flag surface (--add/--unset/--replace-all/
    # --rename-section/--remove-section/-e/bare set); its read-only forms
    # (--get/--list) need a careful flag allowlist and are deferred (#1602).
    "config": "git config is denied by vrg-git.",
    "remote": "git remote is denied by vrg-git.",
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


_REMOTE_SUBCOMMANDS: set[str] = {"push", "pull", "fetch", "ls-remote", "clone"}

_PROTECTED_BRANCHES: set[str] = {"develop", "main"}
_PROTECTED_PREFIXES: tuple[str, ...] = ("release/",)


def _is_protected_branch_name(branch_name: str) -> bool:
    if branch_name in _PROTECTED_BRANCHES:
        return True
    return any(branch_name.startswith(p) for p in _PROTECTED_PREFIXES)


def _is_protected_branch() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S603, S607
        capture_output=True,
        text=True,
        check=False,
    )
    return _is_protected_branch_name(result.stdout.strip())


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


def _upstream_is_integration_branch(branch_name: str) -> bool:
    """Return True if the branch's upstream is a protected integration branch.

    Branches created per the worktree convention
    (``git worktree add -b <branch> ... origin/develop``) track
    ``origin/develop``, which is never gone — so the upstream-gone
    signal carries no information about unpushed work for them (#1426).
    """
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--abbrev-ref", f"{branch_name}@{{upstream}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    # Strip the remote name (e.g. "origin/develop" -> "develop").
    _, _, upstream = result.stdout.strip().partition("/")
    return _is_protected_branch_name(upstream)


def _noninteractive_rebase_env(base_env: dict[str, str] | None) -> dict[str, str]:
    """Force no-op editors so a rebase never blocks on an editor.

    vrg-git denies ``-i``/``--interactive``, so every rebase it runs is
    non-interactive by policy. git can still try to launch a sequence or
    commit editor for a plain ``rebase <upstream>`` (depending on git
    version and config) — which hangs in a headless agent session and
    leaves the worktree stranded mid-rebase with a stale ``index.lock``
    (#1742). Pointing both editors at ``true`` makes any such launch a
    clean no-op while leaving the rebase itself untouched.
    """
    env = dict(base_env) if base_env is not None else dict(os.environ)
    env["GIT_SEQUENCE_EDITOR"] = "true"
    env["GIT_EDITOR"] = "true"
    return env


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
                if _is_protected_branch_name(branch_name):
                    return f"branch -D is denied for protected branch {branch_name}."
                if not _is_upstream_gone(branch_name) and not _upstream_is_integration_branch(
                    branch_name
                ):
                    return (
                        f"branch -D is denied (upstream is not gone and not an "
                        f"integration branch for {branch_name})."
                    )
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


_WORKFLOW_PERMISSION_RE = re.compile(
    r"refusing to allow.*workflow.*without.*workflows.*permission",
    re.IGNORECASE,
)


def _print_workflow_push_guidance() -> None:
    mode = identity_mode.current_mode()
    print(
        f"\nvrg-git: Push rejected — workflow file changes require elevated permissions.\n"
        f"  Your identity ({mode.value}) is not permitted to push workflow file changes.\n"
        f"  Stop and escalate to a human maintainer. Do not attempt to work around\n"
        f"  this failure (e.g., by removing workflow files from the commit).",
        file=sys.stderr,
    )


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

    if subcmd in _READONLY_WITH_DENIED_SUBS:
        denied_subs = _READONLY_WITH_DENIED_SUBS[subcmd]
        # The mutating sub-operation, if any, is the first positional token.
        sub = argv[1] if len(argv) >= 2 else None
        if sub in denied_subs:
            print(f"vrg-git: {subcmd} {sub} is denied by vrg-git.", file=sys.stderr)
            return 1
        result = subprocess.run(["git", *argv], check=False)  # noqa: S603, S607
        return result.returncode

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
        allowed = _ALLOWED_SIMPLE | set(_ALLOWED_COMPOUND) | set(_READONLY_WITH_DENIED_SUBS)
        print(
            f"vrg-git: {subcmd} is not recognized. Allowed: {', '.join(sorted(allowed))}",
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

    if subcmd == "rebase":
        env = _noninteractive_rebase_env(env)

    if subcmd == "push":
        push_result = subprocess.run(  # noqa: S603
            ["git", *argv],  # noqa: S607
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        if push_result.stdout:
            sys.stdout.write(push_result.stdout)
        if push_result.stderr:
            sys.stderr.write(push_result.stderr)
        if push_result.returncode != 0 and _WORKFLOW_PERMISSION_RE.search(push_result.stderr or ""):
            _print_workflow_push_guidance()
        return push_result.returncode

    result = subprocess.run(["git", *argv], check=False, env=env)  # noqa: S603, S607
    return result.returncode
