"""Safe git wrapper for AI agent sessions.

Enforces a subcommand allowlist and flag deny lists.

vrg-git protects origin, not the local checkout. The guards that protect
origin (the push deny-list, the frozen-push refusal) are strict. Rewriting
or discarding local work (``reset``, ``clean``, a pathspec ``checkout`` and
``rebase -i --autosquash``) is allowed on a branch that is neither protected
nor frozen, and refused, failing closed, everywhere else (#4217).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

from vergil_tooling.lib import branch_names, epics, github, retry
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.git import (
    WORKFLOWS_PATH,
    WorkflowRefusal,
    _git_auth_env,
    diagnose_workflow_refusal,
    is_workflow_push_refusal,
    main_worktree_root,
    stale_workflow_refusal_text,
    unknown_workflow_refusal_note,
)
from vergil_tooling.lib.pr_workflow import freeze

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
    # Local rewrites: allowed only on a branch that is neither protected nor
    # frozen, by _check_local_rewrite (#4217).
    "reset",
    "clean",
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

# symbolic-ref reads a symbolic ref with one positional argument and writes or
# deletes one otherwise (`<name> <ref>`, -d/--delete, -m). Only the read form,
# with these flags, is allowed: paad's skills resolve the default branch
# with `git symbolic-ref refs/remotes/origin/HEAD` (#3722).
_SYMBOLIC_REF_READ_FLAGS: frozenset[str] = frozenset({"-q", "--quiet", "--short"})

_DENIED: dict[str, str] = {
    "commit": "Use vrg-commit instead of git commit.",
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
    # -i/--interactive is allowed only together with --autosquash (#4217).
    "rebase": {"-i", "--interactive"},
}


_REMOTE_SUBCOMMANDS: set[str] = {"push", "pull", "fetch", "ls-remote", "clone"}


@cli_main
def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    targets, argv = _split_dash_c(argv)
    if targets is None:
        print("vrg-git: option '-C' requires a value", file=sys.stderr)
        return 2
    if not targets:
        return _run(argv)
    # Apply each `-C <path>` the way git does (each relative to the one before,
    # an empty one a no-op) by changing into it *before* any check runs, so
    # every cwd-derived check and the token's org follow the -C repo exactly as
    # they would follow a `cd` (#3737). The -C options are not forwarded: git
    # already runs in the target, and re-applying a relative path would land
    # elsewhere. The previous cwd is restored on return for in-process callers.
    with contextlib.ExitStack() as stack:
        for target in targets:
            if not target:
                continue
            try:
                stack.enter_context(contextlib.chdir(target))
            except OSError as exc:
                print(
                    f"vrg-git: cannot change to '{target}': {exc.strerror or exc}",
                    file=sys.stderr,
                )
                return 128
        return _run(argv)


def _split_dash_c(argv: list[str]) -> tuple[list[str] | None, list[str]]:
    """Split the leading global ``-C <path>`` options off *argv*.

    Returns ``(paths, rest)``: the ``-C`` paths in order and the remaining
    arguments, starting at the subcommand. ``paths`` is None when a ``-C`` has
    no value. Only the separate ``-C <path>`` form is git's (``-C<path>`` is
    not), and only before the subcommand; any other global option stays an
    unrecognized subcommand and is refused.
    """
    paths: list[str] = []
    index = 0
    while index < len(argv) and argv[index] == "-C":
        if index + 1 >= len(argv):
            return None, []
        paths.append(argv[index + 1])
        index += 2
    return paths, argv[index:]


def _run(argv: list[str]) -> int:
    """Check *argv* against the policy in the current directory and run it."""
    if not argv:
        print("usage: vrg-git [-C <path>]... <subcommand> [args...]", file=sys.stderr)
        return 2

    if argv[0] in ("-h", "--help"):
        _print_help()
        return 0

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

    if subcmd == "symbolic-ref":
        symref_err = _check_symbolic_ref_read(argv[1:])
        if symref_err:
            print(f"vrg-git: {symref_err}", file=sys.stderr)
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

        if subcmd == "worktree" and argv[1] == "add":
            add_err = _check_worktree_add(argv[2:])
            if add_err:
                print(f"vrg-git: {add_err}", file=sys.stderr)
                return 1

        result = subprocess.run(["git", *argv], check=False)  # noqa: S603, S607
        return result.returncode

    if subcmd not in _ALLOWED_SIMPLE:
        allowed = (
            _ALLOWED_SIMPLE
            | set(_ALLOWED_COMPOUND)
            | set(_READONLY_WITH_DENIED_SUBS)
            | {"symbolic-ref"}
        )
        print(
            f"vrg-git: {subcmd} is not recognized. Allowed: {', '.join(sorted(allowed))}",
            file=sys.stderr,
        )
        return 1

    flag_err = _check_denied_flags(subcmd, argv[1:])
    if flag_err:
        print(f"vrg-git: {flag_err}", file=sys.stderr)
        return 1

    rewrite_err = _check_local_rewrite(subcmd, argv[1:])
    if rewrite_err:
        print(rewrite_err, file=sys.stderr)
        return 1

    worktree_err = _check_worktree_convention(subcmd, argv[1:])
    if worktree_err:
        print(f"vrg-git: {worktree_err}", file=sys.stderr)
        return 1

    branch_name_err = _check_new_branch_name(subcmd, argv[1:])
    if branch_name_err:
        print(f"vrg-git: {branch_name_err}", file=sys.stderr)
        return 1

    if subcmd == "push":
        frozen_msg = _check_frozen_push(argv[1:])
        if frozen_msg is not None:
            print(frozen_msg, file=sys.stderr)
            return 1

    env = None
    if subcmd in _REMOTE_SUBCOMMANDS:
        # clone runs outside any repo, so get_installation_token's default
        # org-from-remote detection finds nothing — derive the org from the
        # clone URL argument instead. (#1785)
        org = _org_from_clone_url(argv[1:]) if subcmd == "clone" else None
        token = github.get_installation_token(org)
        if token is not None:
            env = _git_auth_env(token)

    if subcmd == "rebase":
        env = _noninteractive_rebase_env(env)

    if subcmd in _REMOTE_SUBCOMMANDS:
        # Network op: capture output so its stderr can be inspected for
        # retryable transients (GitHub 502s, SSH-backend blips), and ride
        # those out instead of failing the first attempt (#2835).
        net_result = _run_network_with_retry(argv, env)
        if net_result.stdout:
            sys.stdout.write(net_result.stdout)
        if net_result.stderr:
            sys.stderr.write(net_result.stderr)
        if (
            subcmd == "push"
            and net_result.returncode != 0
            and is_workflow_push_refusal(net_result.stderr)
        ):
            # A workflow-file push refusal is expected and routine: the agent
            # never pushes a workflow branch — the human's vrg-submit-pr does,
            # at submit time. Warn and report success so the agent proceeds to
            # report-ready instead of mistreating a normal case as a failure
            # (#2540). Any other push failure keeps its original returncode.
            # A stale branch, whose own commits change no workflow file but
            # whose tree carries an outdated one, is refused the same way; its
            # remedy is a rebase, not report-ready, so it stays a failure (#3814).
            diagnosis = _diagnose_workflow_refusal(argv[1:])
            if diagnosis.kind == "stale":
                _print_stale_workflow_push_guidance(diagnosis)
                return net_result.returncode
            _print_workflow_push_guidance(diagnosis)
            return 0
        return net_result.returncode

    result = subprocess.run(["git", *argv], check=False, env=env)  # noqa: S603, S607
    return result.returncode


def _check_symbolic_ref_read(args: list[str]) -> str | None:
    """Refuse every symbolic-ref form except the read: one ref, optional read flags."""
    flags = [a for a in args if a.startswith("-")]
    positionals = [a for a in args if not a.startswith("-")]
    bad_flags = [f for f in flags if f not in _SYMBOLIC_REF_READ_FLAGS]
    if bad_flags or len(positionals) != 1:
        return (
            "symbolic-ref is denied except its read form: "
            "symbolic-ref [-q|--quiet] [--short] <name>."
        )
    return None


def _org_from_clone_url(args: list[str]) -> str | None:
    """Return the owner of the first github.com repo URL among clone args.

    Used to mint the installation token for ``clone``, which runs outside any
    repo, so the org cannot come from a remote. An arg counts only when
    :func:`github.github_remote_repo`, the one GitHub remote URL parser, reads
    it as naming a repo on github.com (#4088).
    """
    for arg in args:
        repo = github.github_remote_repo(arg)
        if repo is not None:
            return repo.split("/", 1)[0]
    return None


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

    vrg-git allows ``-i``/``--interactive`` only with ``--autosquash``, and
    with these editors that rebase runs its generated todo list unedited, so
    every rebase it runs is non-interactive by policy. git can still try to
    launch a sequence or
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

    if subcmd == "rebase":
        if _rebase_is_interactive(args) and not _rebase_has_autosquash(args):
            return (
                "rebase -i/--interactive is denied without --autosquash; use "
                "vrg-git rebase -i --autosquash <base>."
            )
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


# rebase short options whose value is the rest of the cluster, or the next
# token when nothing follows in the cluster (-S takes only an attached
# value); and long options that take the next token as their value. A value
# is never read as a flag.
_REBASE_SHORT_WITH_VALUE: set[str] = set("xsXC")
_REBASE_SHORT_ATTACHED_VALUE: set[str] = set("S")
_REBASE_LONG_WITH_VALUE: set[str] = {"--exec", "--strategy", "--strategy-option", "--onto"}


def _rebase_flags(args: list[str]) -> list[str]:
    """Return the option tokens of ``rebase *args*``, with option values removed.

    Each short cluster is split into one ``-<c>`` per flag, so ``-ri`` reads
    as ``-r`` and ``-i``. Positionals and option values are dropped.
    """
    return _split_rebase_args(args)[0]


def _rebase_positionals(args: list[str]) -> list[str]:
    """Return the positionals of ``rebase *args*``: ``[<upstream> [<branch>]]``."""
    return _split_rebase_args(args)[1]


def _rebase_names_branch(args: list[str]) -> bool:
    """Return True when ``rebase *args*`` names a ``<branch>`` to check out and rewrite.

    git reads ``rebase [<upstream> [<branch>]]``, or ``rebase --root
    [<branch>]``, where the one positional is the branch. ``--root`` and any
    abbreviation git would accept for it count.
    """
    root = any(
        len(flag) >= len("--ro") and "--root".startswith(flag.split("=", 1)[0])
        for flag in _rebase_flags(args)
    )
    return len(_rebase_positionals(args)) > (0 if root else 1)


def _split_rebase_args(args: list[str]) -> tuple[list[str], list[str]]:
    """Split ``rebase *args*`` into its flags and its positionals, values dropped."""
    flags: list[str] = []
    positionals: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            continue
        if arg == "-" or not arg.startswith("-"):
            positionals.append(arg)
            continue
        if arg.startswith("--"):
            flags.append(arg)
            skip_next = arg in _REBASE_LONG_WITH_VALUE
            continue
        cluster = arg[1:]
        for index, char in enumerate(cluster):
            flags.append(f"-{char}")
            if char in _REBASE_SHORT_ATTACHED_VALUE:
                break
            if char in _REBASE_SHORT_WITH_VALUE:
                skip_next = index == len(cluster) - 1
                break
    return flags, positionals


def _rebase_is_interactive(args: list[str]) -> bool:
    """Return True when ``rebase *args*`` asks for an interactive rebase.

    Over-approximates on purpose: ``-i`` anywhere in a short cluster and any
    abbreviation git would accept for ``--interactive`` count, so an
    interactive rebase cannot slip past the ``--autosquash`` rule.
    """
    for flag in _rebase_flags(args):
        if flag == "-i":
            return True
        name = flag.split("=", 1)[0]
        if len(name) > len("--") and "--interactive".startswith(name):
            return True
    return False


def _rebase_has_autosquash(args: list[str]) -> bool:
    """Return True when ``rebase *args*`` ends with ``--autosquash`` in effect.

    Only the exact ``--autosquash`` turns it on; a later ``--no-autosquash``
    (or an abbreviation of it) turns it off again, as in git.
    """
    enabled = False
    for flag in _rebase_flags(args):
        if flag == "--autosquash":
            enabled = True
        elif len(flag) >= len("--no-autosq") and "--no-autosquash".startswith(flag):
            enabled = False
    return enabled


# checkout options that take the next token as their value.
_CHECKOUT_LONG_WITH_VALUE: set[str] = {"--orphan", "--conflict"}
_CHECKOUT_SHORT_WITH_VALUE: set[str] = set("bB")


def _checkout_has_pathspec(args: list[str]) -> bool:
    """Return True when ``checkout *args*`` restores paths instead of switching.

    A pathspec checkout overwrites local changes, so it is a local rewrite.
    It is one when it has ``--``, ``-p``/``--patch`` or
    ``--pathspec-from-file``, more than one positional, or a single
    positional that names neither a commit nor a remote-tracking branch
    (``checkout .``, ``checkout src/``). A plain branch or commit switch, with
    or without ``-b``, is not.
    """
    positionals: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            return True
        if arg == "-":
            # `checkout -` switches to the previous branch.
            continue
        if arg.startswith("--"):
            if arg.split("=", 1)[0] in ("--patch", "--pathspec-from-file"):
                return True
            skip_next = arg in _CHECKOUT_LONG_WITH_VALUE
            continue
        if arg.startswith("-"):
            cluster = arg[1:]
            for index, char in enumerate(cluster):
                if char == "p":
                    return True
                if char in _CHECKOUT_SHORT_WITH_VALUE:
                    skip_next = index == len(cluster) - 1
                    break
            continue
        positionals.append(arg)
    if len(positionals) > 1:
        return True
    if not positionals:
        return False
    return not _names_commit_or_remote_branch(positionals[0])


def _names_commit_or_remote_branch(name: str) -> bool:
    """Return True when *name* is a commit, or a branch git would make from a remote.

    The two things a one-argument ``checkout <name>`` switches to: a
    commit-ish, or (git's ``--guess``) a branch that exists only as
    ``<remote>/<name>``. Anything else is read as a path.
    """
    commit = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode == 0:
        return True
    remotes = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if remotes.returncode != 0:
        return False
    return name in remotes.stdout.splitlines()


def _is_local_rewrite(subcmd: str, args: list[str]) -> bool:
    """Return True when ``git *subcmd* *args*`` rewrites or discards local work."""
    if subcmd in ("reset", "clean"):
        return True
    if subcmd == "checkout":
        return _checkout_has_pathspec(args)
    if subcmd == "rebase":
        return _rebase_is_interactive(args)
    return False


def _rewrite_target_branch() -> str | None:
    """Return the branch a local rewrite here would change, or None.

    The checked-out branch; or, while a rebase is in progress (HEAD is
    detached), the branch being rebased, which the rebase updates when it
    finishes. None for any other detached HEAD or an unresolvable one, so an
    unknown is refused rather than treated as a feature branch.
    """
    head = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    for state in ("rebase-merge/head-name", "rebase-apply/head-name"):
        located = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--git-path", state],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if located.returncode != 0 or not located.stdout.strip():
            return None
        try:
            head_name = Path(located.stdout.strip()).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if head_name.startswith("refs/heads/"):
            return head_name.removeprefix("refs/heads/")
        return None
    return None


def _check_local_rewrite(subcmd: str, args: list[str]) -> str | None:
    """Refuse a local rewrite unless the branch is neither protected nor frozen.

    ``reset``, ``clean``, a pathspec ``checkout`` and ``rebase -i
    --autosquash`` rewrite or discard local work. vrg-git protects origin,
    not the local checkout, so they are allowed on a feature branch, but
    refused on ``develop``, ``main`` and ``release/*``, on a detached HEAD,
    and on a branch reported ready (the freeze, #1719). Unlike commit and
    push, which warn and proceed, they fail closed when the PR-workflow
    state exists but cannot be read, because they throw work away (#4217).
    """
    if not _is_local_rewrite(subcmd, args):
        return None
    if subcmd == "rebase" and _rebase_names_branch(args):
        # `rebase <upstream> <branch>` and `rebase --root <branch>` check out
        # and rewrite <branch>, which the checks below (made on the current
        # branch) would never see.
        return (
            "vrg-git: rebase -i --autosquash takes only <base>; check out the "
            "branch to rebase in its own worktree and run it there."
        )
    branch = _rewrite_target_branch()
    if branch is None:
        return (
            f"vrg-git: {subcmd} is refused here: HEAD is not on a branch, or the "
            "branch cannot be resolved. vrg-git allows local rewrites only on a "
            "checked-out feature branch."
        )
    if _is_protected_branch_name(branch):
        return (
            f"vrg-git: {subcmd} is refused on protected branch {branch} "
            "(develop, main, release/*). vrg-git allows local rewrites only on a "
            "feature branch."
        )
    root = _current_worktree_root()
    if root is None:
        return (
            f"vrg-git: {subcmd} is refused here: the worktree root cannot be "
            "resolved, so the report-ready freeze cannot be checked."
        )
    check = freeze.check_worktree(root, action="rewrite this branch")
    if check.read_error is not None:
        return (
            f"vrg-git: {subcmd} is refused: could not read PR-workflow state "
            f"({check.read_error}). A local rewrite discards work, so it fails "
            "closed instead of skipping the freeze check. Fix or remove "
            f"{root / '.vergil' / 'pr-workflow.json'}, then re-run "
            "vrg-pr-workflow report-ready if the branch was reported ready."
        )
    if check.frozen:
        return check.message
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


# `worktree add` options that consume the following token as their value, so
# the path-positional scan must skip both the flag and its argument. `=`-joined
# forms (e.g. `--reason=busy`) start with `-` and are skipped as plain flags.
_WORKTREE_ADD_VALUE_OPTS: set[str] = {"-b", "-B", "--reason"}


def _parse_worktree_add_path(args: list[str]) -> str | None:
    """Return the ``<path>`` positional from ``worktree add`` *args*, or None.

    *args* are the tokens after ``add``. The first token that is neither an
    option nor an option's consumed value is the path; the optional
    ``<commit-ish>`` that follows is ignored.
    """
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in _WORKTREE_ADD_VALUE_OPTS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _parse_new_branch(create_flags: tuple[str, ...], args: list[str]) -> str | None:
    """Return the branch name a ``-b``/``-B``/``-c``/``-C`` flag creates, or None.

    *create_flags* is the set of new-branch flags for the subcommand (``-b``/
    ``-B`` for ``worktree add`` and ``checkout``; ``-c``/``-C`` for ``switch``).
    Returns the token immediately following the first such flag; None when no
    new branch is being created (existing-branch checkout/adoption).
    """
    for index, arg in enumerate(args):
        if arg in create_flags:
            return args[index + 1] if index + 1 < len(args) else None
    return None


def _branch_format_error(branch: str) -> str:
    """The shared refusal for an issue-family branch name missing ``<N>-<slug>``.

    Same rule and wording as the vrg-commit Check-4 guard (both read from
    lib.branch_names), so a branch rejected at creation and one rejected at
    commit read identically (issue #2550).
    """
    return f"branch name must include a repo issue number ({branch}). {branch_names.FORMAT_HINT}"


def _check_worktree_add(args: list[str]) -> str | None:
    """Reject a ``worktree add`` that violates the work-unit conventions.

    Two invariants, both required for the ``issue -> branch -> worktree`` chain
    to thread one identifier (issue #2550):

    1. **Anchoring (#1922).** The new worktree's resolved path must be a direct
       child of ``<main_worktree_root>/.worktrees/``. This rejects a relative
       path resolved from inside another worktree (the nesting bug) and any
       absolute or ``../`` path that escapes the canonical container.
    2. **Branch/directory lockstep (#2550).** When ``-b``/``-B`` creates an
       issue-family branch, the branch must be a well-formed ``<type>/<N>-<slug>``
       and the directory must be the ``issue-<N>-<slug>`` derived from it — so the
       directory name, branch name, and issue number never diverge at the source.

    When the main worktree root cannot be resolved (not in a repo), the guard is
    skipped so git reports its own error rather than this layer masking it.
    """
    path = _parse_worktree_add_path(args)
    if path is None:
        return None
    try:
        root = main_worktree_root()
    except (subprocess.CalledProcessError, OSError):
        return None
    target = Path(path)
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()
    canonical = (root / ".worktrees").resolve()
    if target.parent != canonical:
        return (
            f"worktree add target must be a direct child of {canonical} "
            f"(it resolved to {target}). Run the command from the project root "
            f"with a relative path like .worktrees/issue-<N>-<slug>; never create "
            f"a worktree from inside another worktree."
        )

    # Branch/directory lockstep: only when -b/-B creates an issue-family branch.
    # A worktree adopting an existing branch (no -b), or creating a release/*
    # branch, carries no issue number and is left untouched.
    new_branch = _parse_new_branch(("-b", "-B"), args)
    if new_branch is None or not branch_names.requires_issue_number(new_branch):
        return None
    if not branch_names.is_valid_issue_branch(new_branch):
        return _branch_format_error(new_branch)
    expected = branch_names.expected_worktree_dirname(new_branch)
    if target.name != expected:
        return (
            f"worktree directory name '{target.name}' does not match branch "
            f"'{new_branch}'. Use '.worktrees/{expected}' so the directory, "
            f"branch, and issue number stay in lockstep (issue #2550)."
        )
    return _check_issue_is_open_task(new_branch)


# `git branch` options that select a mode naming no new branch: listing (and the
# filters that imply it; -a/-r refuse a name), deleting, and upstream or
# description edits. Any one of them means the command creates nothing.
_BRANCH_NO_CREATE_LONG: set[str] = {
    "--list",
    "--all",
    "--remotes",
    "--verbose",
    "--show-current",
    "--contains",
    "--no-contains",
    "--merged",
    "--no-merged",
    "--points-at",
    "--delete",
    "--set-upstream-to",
    "--unset-upstream",
    "--edit-description",
}
_BRANCH_NO_CREATE_SHORT: set[str] = set("lavrdDu")
# Rename (-m/-M/--move) and copy (-c/-C/--copy) take ``[<old>] <new>``.
_BRANCH_RENAME_LONG: set[str] = {"--move", "--copy"}
_BRANCH_RENAME_SHORT: set[str] = set("mMcC")
# Short flags whose remaining cluster characters are their value, not flags.
_BRANCH_SHORT_WITH_VALUE: set[str] = set("tu")
# Long options that consume the following token as their value.
_BRANCH_VALUE_OPTS: set[str] = {"--sort", "--format"}


def _parse_branch_create(args: list[str]) -> str | None:
    """Return the branch name ``git branch *args*`` brings into being, or None.

    Three forms name a new branch: ``branch [-f] <name> [<start>]``, a copy
    (``-c``/``-C``/``--copy``) and a rename (``-m``/``-M``/``--move``), whose
    new name is the last of ``[<old>] <new>``. A rename is covered because
    renaming a branch *to* an issue-branch name makes that branch as surely as
    creating it; a rename to a non-issue name is unaffected. Every other form
    (listing, deleting, upstream and description edits, ``--show-current``)
    names no new branch and returns None.
    """
    positionals: list[str] = []
    rename = False
    skip_next = False
    after_separator = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if after_separator or not arg.startswith("-"):
            positionals.append(arg)
            continue
        if arg == "--":
            after_separator = True
            continue
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name in _BRANCH_NO_CREATE_LONG:
                return None
            if name in _BRANCH_RENAME_LONG:
                rename = True
            elif arg in _BRANCH_VALUE_OPTS:
                skip_next = True
            continue
        for char in arg[1:]:
            if char in _BRANCH_NO_CREATE_SHORT:
                return None
            if char in _BRANCH_RENAME_SHORT:
                rename = True
            if char in _BRANCH_SHORT_WITH_VALUE:
                break
    if not positionals:
        return None
    return positionals[-1] if rename else positionals[0]


def _check_new_branch_name(subcmd: str, args: list[str]) -> str | None:
    """Reject ``checkout -b``/``switch -c``/``branch`` making a bad issue branch.

    Extends the issue-number rule to the other creation paths the worktree
    convention allows inside a secondary worktree, so a non-conforming branch is
    caught at creation rather than only later at commit (issue #2550); ``branch
    <name>`` and its copy/rename forms no longer bypass it (issue #3606). A
    non-issue branch (e.g. ``release/*``), a plain checkout/switch of an
    existing branch, and the non-creating ``branch`` forms are left untouched.
    """
    if subcmd == "branch":
        new_branch = _parse_branch_create(args)
    else:
        create_flags = {"checkout": ("-b", "-B"), "switch": ("-c", "-C")}.get(subcmd)
        if create_flags is None:
            return None
        new_branch = _parse_new_branch(create_flags, args)
    if new_branch is None or not branch_names.requires_issue_number(new_branch):
        return None
    if not branch_names.is_valid_issue_branch(new_branch):
        return _branch_format_error(new_branch)
    return _check_issue_is_open_task(new_branch)


# A failed issue read, in any of the shapes the gh boundary raises: gh exiting
# non-zero (incl. GitHubAPIError), gh not runnable, unparseable JSON
# (JSONDecodeError is a ValueError), or no App token for the repo's owner.
_ISSUE_READ_ERRORS = (
    subprocess.CalledProcessError,
    OSError,
    ValueError,
    github.NoInstallationError,
    github.AppAuthError,
)


def _check_issue_is_open_task(branch: str) -> str | None:
    """Refuse an issue branch whose ``<N>`` is not an open task (#3592).

    For a well-formed ``<type>/<N>-<slug>`` *branch*, the issue ``<N>``, read
    from the current repo, must be open and carry neither the ``epic`` nor the
    ``archive`` label: a branch is one task's single PR, and an epic or archive
    is never closed by a PR (#2842). Refusing here stops the work at its source
    rather than at report-ready or submit, after it is built. Any other branch
    name is not this convention's and is left alone (no lookup).

    One ``gh issue view`` reads state and labels together, so the guard costs a
    single call per creation. Fail closed: a failed or malformed read refuses
    with the reason and never passes as "a task".
    """
    parsed = branch_names.parse_issue_branch(branch)
    if parsed is None:
        return None
    number = parsed.number
    try:
        raw = github.read_json("issue", "view", number, "--json", "state,labels")
    except _ISSUE_READ_ERRORS as exc:
        return _issue_read_refusal(branch, number, str(exc).strip() or type(exc).__name__)
    if not isinstance(raw, dict):
        return _issue_read_refusal(branch, number, f"unexpected response {raw!r}")
    labels = {str(label.get("name", "")) for label in _as_label_list(raw.get("labels"))}
    kind = epics.non_task_kind_of_labels(labels)
    if kind is not None:
        return epics.non_task_refusal_message(branch, f"#{number}", kind)
    if raw.get("state") != "OPEN":
        return (
            f"{branch} (#{number}) is not an open issue (state: {raw.get('state')}); "
            "a branch carries an open task's work. Reopen the issue, or create a "
            "new task and use its number."
        )
    return None


def _as_label_list(value: object) -> list[dict[str, object]]:
    """The label objects of a ``gh issue view --json labels`` payload."""
    if not isinstance(value, list):
        return []
    return [label for label in value if isinstance(label, dict)]


def _issue_read_refusal(branch: str, number: str, reason: str) -> str:
    """The refusal when issue #*number* cannot be read to vet *branch*."""
    return (
        f"cannot create {branch}: could not read issue #{number} to confirm it is "
        f"an open task, not an epic or archive ({reason}). Check that "
        f"'vrg-gh issue view {number}' works here (auth, network, and that the "
        "issue exists in this repo), then retry."
    )


def _current_worktree_root() -> Path | None:
    """Return the CWD's worktree root, or None when not resolvable.

    Used to locate ``.vergil/pr-workflow.json`` for the freeze check. A
    non-repo CWD (or any resolution failure) yields None so the push falls
    through to git's own error rather than this layer masking it.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],  # noqa: S603, S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root) if root else None


# Push flags that change only output/upstream bookkeeping, never *what* is
# pushed or how. Anything else (``--all``, ``--mirror``, ``--tags``,
# ``--delete``, ``--force-with-lease``, ``-o``, ...) keeps the frozen push
# refused — the reported-commit allowance is fail-closed (issue #3071).
_PUBLISH_SAFE_PUSH_FLAGS = frozenset(
    {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--progress", "--no-progress"}
)


def _pushes_only_branch_at_head(args: list[str], branch: str) -> bool:
    """Return True when ``git push *args`` publishes only *branch* at HEAD.

    Fail-closed parse: every flag must be in :data:`_PUBLISH_SAFE_PUSH_FLAGS`,
    there must be an explicit remote **and** at least one explicit refspec, and
    every refspec must take its source from HEAD/*branch* and land on *branch*
    — no ``+`` force, no other source commit or destination. A bare ``push``
    is refused because what it publishes depends on ``push.default`` config.
    """
    positionals: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            if arg not in _PUBLISH_SAFE_PUSH_FLAGS:
                return False
        else:
            positionals.append(arg)
    if len(positionals) < 2:
        return False
    names = {branch, f"refs/heads/{branch}"}
    for refspec in positionals[1:]:
        src, sep, dst = refspec.partition(":")
        if src not in names | {"HEAD"}:
            return False
        if sep and dst not in names:
            return False
    return True


def _check_frozen_push(args: list[str]) -> str | None:
    """Return a refusal message when the current worktree's branch is frozen.

    A branch reported ready (``report-ready``) is done; advancing it with a push
    produces the reused-branch straggler (issue #1719). The human's
    ``vrg-submit-pr`` pushes via ``lib.git`` directly, not this wrapper, so this
    guard only stops an *agent* push of a frozen branch. Returns None (push
    allowed) when not frozen or when the worktree root cannot be resolved.

    One frozen push is allowed: publishing the branch at exactly the reported
    commit (HEAD == recorded ``head_sha``, on the recorded branch, with refspecs
    that push only that branch at HEAD). That push cannot move the branch past
    what ``report-ready`` recorded, so it is not drift — and it is what a relay
    ``vrg-submit-pr <branch>`` needs when the branch was not pushed before
    ``report-ready`` (issue #3071). A missing ``head_sha`` or unresolvable HEAD
    never qualifies.
    """
    root = _current_worktree_root()
    if root is None:
        return None
    check = freeze.check_worktree(root, action="advance this branch with a push")
    if check.read_error is not None:
        print(
            f"vrg-git: warning: could not read PR-workflow state ({check.read_error}); "
            "proceeding without a freeze check.",
            file=sys.stderr,
        )
    if not check.frozen:
        return None
    branch = check.publishable_branch
    if branch is None:
        return check.message
    if _pushes_only_branch_at_head(args, branch):
        print(
            "vrg-git: branch is frozen, but HEAD is exactly the reported commit; "
            "allowing this push (it publishes the reported commit, not new work).",
            file=sys.stderr,
        )
        return None
    return (
        f"{check.message}\n\n"
        "HEAD is still exactly the reported commit, so publishing it is allowed —\n"
        "but only with an explicit push of this branch:\n"
        f"  vrg-git push -u origin {branch}"
    )


# Push flags that leave the pushed commits as named by the refspecs, so the
# refused push is still "this branch at HEAD" and its workflow refusal can be
# diagnosed against the branch's own diff. Any other flag (``--all``,
# ``--mirror``, ``--tags``, ``-o``, ...) leaves the diagnosis unknown (#3814).
_DIAGNOSABLE_PUSH_FLAGS = _PUBLISH_SAFE_PUSH_FLAGS | {
    "--force-with-lease",
    "--force-if-includes",
    "--no-verify",
    "--atomic",
}


def _pushed_remote_if_head_only(args: list[str]) -> tuple[str, str | None]:
    """Return ``(remote, None)`` when ``git push *args`` pushes only HEAD, else a reason.

    A bare push (or one naming only the remote) pushes the current branch
    under the default ``push.default``; an explicit refspec must take its
    source from HEAD or the current branch. Fail-closed: anything else is
    reported as undiagnosable rather than guessed.
    """
    positionals: list[str] = []
    for arg in args:
        if arg.startswith("-"):
            if arg not in _DIAGNOSABLE_PUSH_FLAGS and not arg.startswith("--force-with-lease="):
                return "", f"the push uses {arg}"
        else:
            positionals.append(arg)
    remote = positionals[0] if positionals else "origin"
    refspecs = positionals[1:]
    if not refspecs:
        return remote, None
    current = subprocess.run(  # noqa: S603
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    branch = current.stdout.strip() if current.returncode == 0 else ""
    names = {"HEAD"} | ({branch, f"refs/heads/{branch}"} if branch else set())
    for refspec in refspecs:
        src = refspec.removeprefix("+").partition(":")[0]
        if src not in names:
            return "", f"the push names {refspec!r}, not the current branch"
    return remote, None


def _diagnose_workflow_refusal(args: list[str]) -> WorkflowRefusal:
    """Diagnose a refused ``git push *args`` with the shared helper (#3814, #4077).

    Only a push of HEAD is diagnosable; any other push is ``unknown``, which
    keeps the usual hand-off guidance.
    """
    remote, not_head = _pushed_remote_if_head_only(args)
    if not_head is not None:
        return WorkflowRefusal("unknown", reason=not_head)
    return diagnose_workflow_refusal(remote)


def _print_workflow_push_guidance(diagnosis: WorkflowRefusal) -> None:
    if diagnosis.kind == "own":
        cause = (
            f"  This branch's own commits change {WORKFLOWS_PATH}** "
            f"({', '.join(diagnosis.files)}),\n"
            "  which an agent is not permitted to push;\n"
        )
    else:
        cause = (
            f"  GitHub refused it for {WORKFLOWS_PATH}** changes, which an agent is"
            " not permitted to push;\n"
        )
    print(
        "\nvrg-git: warning: push skipped for workflow-file changes — this is"
        " EXPECTED and NOT an error.\n"
        f"{cause}"
        "  it does not need an agent push. The human's vrg-submit-pr pushes"
        " the branch at\n"
        "  submit time. PROCEED AS USUAL — run `vrg-pr-workflow report-ready`"
        " to hand off.\n"
        "  Do NOT remove the workflow files from the commit to force a push.",
        file=sys.stderr,
    )
    if diagnosis.kind == "unknown":
        print(f"  vrg-git {unknown_workflow_refusal_note(diagnosis)}", file=sys.stderr)


def _print_stale_workflow_push_guidance(diagnosis: WorkflowRefusal) -> None:
    print(
        f"\nvrg-git: error: {stale_workflow_refusal_text(diagnosis)}"
        "  then re-run `vrg-container-run -- vrg-validate` and push again"
        " (with --force-with-lease\n"
        "  if the branch is already on the remote). Do NOT hand this off with"
        " `vrg-pr-workflow report-ready`.",
        file=sys.stderr,
    )


def _run_network_with_retry(
    argv: list[str], env: dict[str, str] | None
) -> subprocess.CompletedProcess[str]:
    """Run ``git *argv`` capturing output, retrying transient GitHub failures.

    Returns the final attempt's ``CompletedProcess``. Network ops are captured
    (not streamed) so their stderr can be inspected for retryable transients —
    GitHub 502s, SSH-backend blips rejecting a valid key. Every retry is
    announced on stderr so a genuinely non-transient failure (a real auth error,
    a missing repo) still surfaces before the final result — no silent masking
    (#2835).
    """
    for attempt in range(retry.MAX_RETRIES + 1):
        result = subprocess.run(  # noqa: S603
            ["git", *argv],  # noqa: S607
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or attempt == retry.MAX_RETRIES:
            return result
        exc = subprocess.CalledProcessError(
            result.returncode, ("git", *argv), output=result.stdout, stderr=result.stderr
        )
        if not retry.is_retryable(exc):
            return result
        delay = retry.compute_delay(attempt)
        sys.stderr.write(
            f"vrg-git {argv[0]}: transient GitHub failure, retrying in "
            f"{delay:.1f}s (attempt {attempt + 1}/{retry.MAX_RETRIES + 1})\n"
        )
        time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _print_help() -> None:
    """Print vrg-git's own help — what the wrapper enforces before forwarding."""
    denied = ", ".join(sorted(_DENIED))
    print(
        "usage: vrg-git [-C <path>]... <subcommand> [args...]\n"
        "\n"
        "A safe git wrapper for agent sessions: it forwards to git only after\n"
        "enforcing policy, so it is not a drop-in replacement for git.\n"
        "\n"
        "-C <path> runs as if started in <path> (repeatable, each relative to\n"
        "the one before, as in git). It changes directory before any check,\n"
        "so every check below, and the org of the identity token, applies to\n"
        "the -C repo. No other global git option is accepted.\n"
        "\n"
        "What it enforces:\n"
        "  - Subcommand allowlist: read-only inspection commands plus a vetted\n"
        "    set of mutating ones (add, push, fetch, pull, clone, checkout,\n"
        "    switch, stash, merge, cherry-pick, rebase, reset, clean,\n"
        "    worktree add/list/remove).\n"
        "  - Read-only forms only: reflog (expire/delete denied) and\n"
        "    symbolic-ref [-q|--quiet] [--short] <name> (setting a ref,\n"
        "    -d/--delete and -m are denied).\n"
        f"  - Denied subcommands: {denied} (use vrg-commit for commits).\n"
        "  - Flag deny-list: push -f/--force, branch -D/--force; rebase -i\n"
        "    only with --autosquash; --force-with-lease is blocked on protected\n"
        "    branches (develop, main, release/*).\n"
        "  - Local rewrites: vrg-git protects origin, not the local checkout.\n"
        "    reset, clean, a pathspec checkout (checkout ., checkout -- <path>)\n"
        "    and rebase -i --autosquash run only on a branch that is neither\n"
        "    protected nor frozen; an unreadable PR-workflow state refuses them.\n"
        "  - Worktree convention: branch switches in the main worktree and\n"
        "    'worktree add' targets outside .worktrees/ are refused.\n"
        "  - Work-unit identifier: creating an issue-family branch (via\n"
        "    'worktree add -b', 'checkout -b', 'switch -c') requires the\n"
        "    <type>/<N>-<slug> form, and 'worktree add' requires the directory\n"
        "    to be the matching issue-<N>-<slug> (dir/branch stay in lockstep).\n"
        "    Issue <N> must be open and not an epic or archive; a failed issue\n"
        "    read refuses the branch.\n"
        "  - Freeze: a push or a local rewrite is refused once the branch was\n"
        "    reported ready\n"
        "    (vrg-pr-workflow report-ready); unfreeze deliberately to reopen it.\n"
        "  - Identity: remote ops run with the GitHub App installation token for\n"
        "    the resolved identity mode (see vrg-whoami).\n"
        "\n"
        "Per-subcommand git help still forwards, e.g. 'vrg-git log --help'."
    )


if __name__ == "__main__":
    sys.exit(main())
