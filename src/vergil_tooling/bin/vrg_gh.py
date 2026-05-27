"""Safe gh wrapper for AI agent sessions.

Enforces a two-level subcommand allowlist and flag deny lists.
Injects GitHub App installation tokens when available.
"""

from __future__ import annotations

import os
import subprocess
import sys

from vergil_tooling.lib import github, retry

_ALLOWED: dict[str, set[str]] = {
    "issue": {"view", "create", "close", "reopen", "edit", "list", "comment"},
    "pr": {"view", "checks", "list", "diff", "comment", "edit", "review", "merge"},
    "run": {"list", "view", "watch"},
    "repo": {"view", "list"},
    "label": {"list", "create"},
}

_DENIED_PAIRS: dict[str, dict[str, str]] = {
    "pr": {
        "create": "Use vrg-submit-pr instead of gh pr create.",
        "close": "gh pr close is denied by vrg-gh.",
    },
    "repo": {
        "edit": "gh repo edit is denied by vrg-gh.",
        "create": "gh repo create is denied by vrg-gh.",
        "delete": "gh repo delete is denied by vrg-gh.",
    },
}

_DENIED_TOP: dict[str, str] = {
    "api": "gh api is denied by vrg-gh.",
    "auth": "gh auth is denied by vrg-gh.",
}


def _validate_merge_context(argv: list[str]) -> str | None:
    if len(argv) < 3:  # noqa: PLR2004
        return "pr merge requires a PR number or URL."
    return None


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("usage: vrg-gh <subcommand> <action> [args...]", file=sys.stderr)
        return 2

    top = argv[0]

    if top in _DENIED_TOP:
        msg = _DENIED_TOP[top]
        print(f"vrg-gh: {top} is denied. {msg}", file=sys.stderr)
        return 1

    if top not in _ALLOWED:
        print(
            f"vrg-gh: {top} is not recognized. Allowed: {', '.join(sorted(_ALLOWED))}",
            file=sys.stderr,
        )
        return 1

    if len(argv) < 2:  # noqa: PLR2004
        print(
            f"vrg-gh: {top} requires a subcommand. Allowed: {', '.join(sorted(_ALLOWED[top]))}",
            file=sys.stderr,
        )
        return 1

    sub = argv[1]

    if top in _DENIED_PAIRS and sub in _DENIED_PAIRS[top]:
        msg = _DENIED_PAIRS[top][sub]
        print(f"vrg-gh: {top} {sub} is denied. {msg}", file=sys.stderr)
        return 1

    if sub not in _ALLOWED[top]:
        print(
            f"vrg-gh: {top} {sub} is not recognized. Allowed: {', '.join(sorted(_ALLOWED[top]))}",
            file=sys.stderr,
        )
        return 1

    if top == "pr" and sub == "review" and "--approve" in argv:
        print(
            "vrg-gh: pr review --approve is denied. Agents cannot approve PRs.",
            file=sys.stderr,
        )
        return 1

    if top == "pr" and sub == "merge":
        err = _validate_merge_context(argv)
        if err:
            print(f"vrg-gh: pr merge is denied. {err}", file=sys.stderr)
            return 1

    token = github.get_installation_token()
    env: dict[str, str] | None = None
    if token is not None:
        env = {**os.environ, "GH_TOKEN": token}
    try:
        result = retry.run_with_retry(
            ["gh", *argv],  # noqa: S607
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stdout.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        return exc.returncode
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return 0
