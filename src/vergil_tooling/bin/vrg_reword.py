"""Reword a branch-local commit's message through the standards path.

The agent tool surface otherwise cannot fix a commit message:
``vrg-git commit`` is denied, ``vrg-commit`` only creates new commits,
and interactive rebase is unavailable. Yet ``commit-message-fidelity``
is a judgment gate, so a USER agent needs a way to act on it. This tool
rewords *any of the current branch's own commits* via a scripted,
non-interactive rebase, re-stamping the new message through the same
standards/identity path ``vrg-commit`` uses.

It is a deliberate relaxation of a tight restriction, so it is bounded:

- **Branch-local only** — refuses to touch any commit reachable from the
  base branch (never rewrites shared/merged history) or any commit on a
  protected branch.
- **Own-identity only** — refuses when the target commit's author email
  differs from the current git identity, unless ``--allow-foreign-author``
  is passed.
- **Force-with-lease** — when the branch is already on the remote, the
  rewrite is pushed with ``--force-with-lease`` (never bare ``--force``);
  safe on an unmerged feature branch (the #1557 pattern).

Rewording a *mid-chain* commit re-applies the commits after it, which
changes their **committer** (the author is preserved). In an
identity-aware system that should be a deliberate decision, so the tool
prints a warning naming the affected commits before it runs.

The rebase drives two scripted editors, both re-entrant invocations of
this same module (``--seq-edit`` / ``--msg-edit``): the sequence editor
flips the target line from ``pick`` to ``reword``, and the commit-message
editor writes the re-stamped message. Neither prompts interactively.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import git
from vergil_tooling.lib.commit_message import (
    ALLOWED_TYPES,
    build_commit_message,
    contains_autoclose,
)

_PROTECTED_BRANCHES = {"develop", "main"}
_PROTECTED_PREFIXES = ("release/",)

# Env channel from the parent invocation to its scripted-editor children.
_ENV_TARGET = "VRG_REWORD_TARGET"
_ENV_MSG_FILE = "VRG_REWORD_MSG_FILE"

_SEQ_EDIT_FLAG = "--seq-edit"
_MSG_EDIT_FLAG = "--msg-edit"


def _is_protected_branch(branch: str) -> bool:
    if branch in _PROTECTED_BRANCHES:
        return True
    return any(branch.startswith(p) for p in _PROTECTED_PREFIXES)


def _base_branch(branch: str) -> str:
    """Return the integration branch this feature branch targets."""
    return "main" if branch.startswith("release/") else "develop"


def _reject(reason: str, *hints: str) -> int:
    """Print rejection reason and hints to stderr; return 1."""
    print(f"vrg-reword: {reason}", file=sys.stderr)
    for hint in hints:
        print(f"  {hint}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# Scripted-editor children (re-entrant invocations of this module)
# --------------------------------------------------------------------------


def _seq_edit(todo_path: Path, target_sha: str) -> int:
    """Flip the target commit's ``pick`` line to ``reword`` in the todo list.

    Invoked by git as ``GIT_SEQUENCE_EDITOR`` with the rebase todo file.
    The target SHA arrives via the environment so this stays a fixed,
    quote-safe command string.
    """
    lines = todo_path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "pick" and target_sha.startswith(parts[1]):
            lines[i] = line.replace("pick", "reword", 1)
            todo_path.write_text("".join(lines), encoding="utf-8")
            return 0
    print(
        f"vrg-reword: could not find commit {target_sha[:12]} in the rebase plan.",
        file=sys.stderr,
    )
    return 1


def _msg_edit(msg_path: Path, source_path: Path) -> int:
    """Overwrite the commit-message file with the re-stamped message.

    Invoked by git as ``GIT_EDITOR`` for the single reworded commit. The
    source path (a temp file holding the new message) arrives via the
    environment.
    """
    msg_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    return 0


def _dispatch_editor(argv: list[str]) -> int | None:
    """Run an editor child if *argv* names one; else return None.

    git invokes the configured editor as ``<command> <file>``, so the
    file path is the trailing argument.
    """
    if not argv:
        return None
    if argv[0] == _SEQ_EDIT_FLAG:
        target = os.environ.get(_ENV_TARGET, "")
        if not target:
            print(f"vrg-reword: {_ENV_TARGET} not set for sequence editor.", file=sys.stderr)
            return 1
        return _seq_edit(Path(argv[-1]), target)
    if argv[0] == _MSG_EDIT_FLAG:
        source = os.environ.get(_ENV_MSG_FILE, "")
        if not source:
            print(f"vrg-reword: {_ENV_MSG_FILE} not set for message editor.", file=sys.stderr)
            return 1
        return _msg_edit(Path(argv[-1]), Path(source))
    return None


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def _rev_parse(ref: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        ("git", "rev-parse", "--verify", "--quiet", ref),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _is_ancestor(commit: str, ancestor_of: str) -> bool:
    result = subprocess.run(  # noqa: S603
        ("git", "merge-base", "--is-ancestor", commit, ancestor_of),  # noqa: S607
        check=False,
    )
    return result.returncode == 0


def _author_email(sha: str) -> str:
    return git.read_output("show", "-s", "--format=%ae", sha)


def _current_identity_email() -> str:
    result = subprocess.run(  # noqa: S603
        ("git", "config", "user.email"),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _later_commits(target_sha: str) -> list[str]:
    """Return short SHAs of commits applied after *target_sha* on HEAD."""
    out = git.read_output("rev-list", "--reverse", "--abbrev-commit", f"{target_sha}..HEAD")
    return out.splitlines() if out else []


def _validate(args: argparse.Namespace) -> tuple[int, str, str]:
    """Run every guard. Return ``(rc, target_sha, branch)``.

    ``rc`` is 0 when all guards pass, and the SHA and branch are then
    meaningful. On any failure ``rc`` is 1 and the two strings are empty
    placeholders the caller must not use.
    """
    branch = git.current_branch()

    if _is_protected_branch(branch):
        return (
            _reject(
                f"refusing to reword on protected branch '{branch}'.",
                "Reword only your own feature branch's commits.",
            ),
            "",
            "",
        )

    target_sha = _rev_parse(args.sha)
    if target_sha is None:
        return _reject(f"'{args.sha}' is not a valid commit."), "", ""

    if not _is_ancestor(target_sha, "HEAD"):
        return _reject(f"commit {args.sha} is not reachable from HEAD ({branch})."), "", ""

    base = _base_branch(branch)
    base_ref = f"origin/{base}" if _rev_parse(f"origin/{base}") else base
    if _rev_parse(base_ref) is None:
        return (
            _reject(
                f"cannot resolve base branch '{base}' to verify history is unshared.",
                f"Fetch '{base}' before rewording.",
            ),
            "",
            "",
        )
    if _is_ancestor(target_sha, base_ref):
        return (
            _reject(
                f"commit {args.sha} is already part of '{base_ref}' — "
                "refusing to rewrite shared history.",
                "Reword only commits unique to this branch.",
            ),
            "",
            "",
        )

    if not args.allow_foreign_author:
        author = _author_email(target_sha)
        current = _current_identity_email()
        if not current:
            return (
                _reject(
                    "cannot determine the current git identity (user.email is unset).",
                    "Configure git user.email, or pass --allow-foreign-author to skip the check.",
                ),
                "",
                "",
            )
        if author.lower() != current.lower():
            return (
                _reject(
                    f"commit {args.sha} was authored by {author}, not the current "
                    f"identity {current}.",
                    "Rewording re-stamps the message under the current identity.",
                    "Pass --allow-foreign-author to override this provenance guard.",
                ),
                "",
                "",
            )

    if git.working_tree_status():
        return (
            _reject(
                "the working tree is not clean; the rebase reword needs a clean tree.",
                "Commit or stash your changes first.",
            ),
            "",
            "",
        )

    return 0, target_sha, branch


# --------------------------------------------------------------------------
# Rebase
# --------------------------------------------------------------------------


def _editor_command(flag: str) -> str:
    """Build a quote-safe ``python -m`` editor command for git to invoke."""
    return f"{shlex.quote(sys.executable)} -m vergil_tooling.bin.vrg_reword {flag}"


def _run_reword_rebase(target_sha: str, message: str) -> int:
    """Drive the scripted non-interactive rebase that rewords *target_sha*."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".msg", delete=False, encoding="utf-8") as f:
        f.write(message)
        msg_file = f.name

    env = {
        **os.environ,
        _ENV_TARGET: target_sha,
        _ENV_MSG_FILE: msg_file,
        "GIT_SEQUENCE_EDITOR": _editor_command(_SEQ_EDIT_FLAG),
        "GIT_EDITOR": _editor_command(_MSG_EDIT_FLAG),
    }
    try:
        result = subprocess.run(  # noqa: S603
            ("git", "rebase", "-i", f"{target_sha}^"),  # noqa: S607
            env=env,
            check=False,
        )
    finally:
        Path(msg_file).unlink(missing_ok=True)

    if result.returncode != 0:
        print(
            "vrg-reword: the rebase did not complete. The repository may be "
            "mid-rebase;\n  run `git rebase --abort` to restore the branch.",
            file=sys.stderr,
        )
    return result.returncode


def _maybe_push(branch: str, *, no_push: bool) -> int:
    """Push the rewritten branch with --force-with-lease when it has a remote.

    Skips silently when the branch was never pushed (nothing to update)
    or when ``--no-push`` is given. ``--force-with-lease`` refuses to
    clobber commits pushed elsewhere since the last fetch.
    """
    if no_push:
        print(
            "Skipping push (--no-push). Update the remote with:\n"
            f"  vrg-git push --force-with-lease origin {branch}"
        )
        return 0
    if _rev_parse(f"refs/remotes/origin/{branch}") is None:
        print(f"Branch '{branch}' is not on origin yet; nothing to push.")
        return 0
    print(f"Updating origin/{branch} with --force-with-lease...")
    try:
        git.run("push", "--force-with-lease", "origin", branch)
    except subprocess.CalledProcessError:
        print(
            "vrg-reword: the reword succeeded locally but the push was refused — "
            "the remote moved since your last fetch.\n"
            f"  Run `vrg-git fetch origin` and review origin/{branch} before retrying.",
            file=sys.stderr,
        )
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Reword a branch-local commit's message via a scripted rebase."
    )
    parser.add_argument("sha", help="The commit to reword (any branch-local revision)")
    parser.add_argument(
        "--type",
        required=True,
        choices=ALLOWED_TYPES,
        dest="commit_type",
        help="Conventional commit type",
    )
    parser.add_argument("--scope", required=True, help="Conventional commit scope")
    parser.add_argument("--message", required=True, help="Commit description")
    parser.add_argument("--body", default="", help="Detailed commit body")
    parser.add_argument(
        "--allow-foreign-author",
        action="store_true",
        default=False,
        help="Reword even when the commit's author differs from the current identity",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        default=False,
        help="Do not push the rewritten branch to origin afterward",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # Re-entrant scripted-editor modes must short-circuit before argparse,
    # which would reject the bare editor flags.
    editor_rc = _dispatch_editor(argv)
    if editor_rc is not None:
        return editor_rc

    args = parse_args(argv)

    if args.body and contains_autoclose(args.body):
        return _reject(
            "commit body contains a GitHub auto-close keyword (close/fix/resolve).",
            "Use 'Ref #N' instead. Issues stay open until post-merge workflows succeed.",
        )

    rc, target_sha, branch = _validate(args)
    if rc != 0:
        return rc

    later = _later_commits(target_sha)
    if later:
        print(
            f"Note: rewording {args.sha} re-applies {len(later)} later commit(s) "
            f"({', '.join(later)}),\n"
            "  which changes their committer (the author is preserved).",
            file=sys.stderr,
        )

    co_author = os.environ.get("VRG_CO_AUTHOR")
    message = build_commit_message(
        commit_type=args.commit_type,
        scope=args.scope,
        message=args.message,
        body=args.body,
        co_author=co_author,
    )

    rebase_rc = _run_reword_rebase(target_sha, message)
    if rebase_rc != 0:
        return rebase_rc

    print(f"Reworded {args.sha}: {args.commit_type}({args.scope}): {args.message}")
    return _maybe_push(branch, no_push=args.no_push)


if __name__ == "__main__":
    sys.exit(main())
