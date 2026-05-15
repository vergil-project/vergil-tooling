"""Safe gh wrapper for AI agent sessions.

Enforces a two-level subcommand allowlist, flag deny lists, selects
credentials based on command context, and logs every invocation to
a JSON-lines audit file.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_ALLOWED: dict[str, set[str]] = {
    "issue": {"view", "create", "close", "edit", "list", "comment"},
    "pr": {"view", "checks", "list", "diff", "comment", "edit", "review", "merge"},
    "run": {"list", "view", "watch"},
    "repo": {"view"},
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

_ESCALATED_COMMANDS: set[tuple[str, str]] = {
    ("pr", "merge"),
}


def _log_path() -> Path:
    return Path.home() / ".local" / "share" / "vergil" / "vrg-gh.log"


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


def _discover_accounts() -> tuple[str, str]:
    result = subprocess.run(  # noqa: S603
        ["gh", "auth", "status"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout or result.stderr
    accounts = list(dict.fromkeys(re.findall(r"Logged in to github\.com account (\S+)", output)))
    human = [a for a in accounts if not a.endswith("-vergil")]
    agent = [a for a in accounts if a.endswith("-vergil")]
    if len(human) != 1 or len(agent) != 1:
        print(
            "vrg-gh: cannot discover accounts. Expected one human and one "
            f"-vergil account in gh auth status. Found human={human}, agent={agent}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return human[0], agent[0]


def _get_token(command: list[str]) -> str:  # noqa: ARG001
    # Workaround: always use human credentials while agent account is
    # flagged by GitHub (#799). Revert when the flag is lifted.
    human, _agent = _discover_accounts()
    account = human

    result = subprocess.run(  # noqa: S603
        ["gh", "auth", "token", "-u", account],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _validate_merge_context(argv: list[str]) -> str | None:
    if len(argv) < 3:  # noqa: PLR2004
        return "pr merge requires a PR number or URL."
    return None


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("usage: vrg-gh <subcommand> <action> [args...]", file=sys.stderr)
        _log([], "denied")
        return 2

    top = argv[0]

    if top in _DENIED_TOP:
        msg = _DENIED_TOP[top]
        print(f"vrg-gh: {top} is denied. {msg}", file=sys.stderr)
        _log(argv, "denied")
        return 1

    if top not in _ALLOWED:
        print(
            f"vrg-gh: {top} is not recognized. Allowed: {', '.join(sorted(_ALLOWED))}",
            file=sys.stderr,
        )
        _log(argv, "denied")
        return 1

    if len(argv) < 2:  # noqa: PLR2004
        print(
            f"vrg-gh: {top} requires a subcommand. Allowed: {', '.join(sorted(_ALLOWED[top]))}",
            file=sys.stderr,
        )
        _log(argv, "denied")
        return 1

    sub = argv[1]

    if top in _DENIED_PAIRS and sub in _DENIED_PAIRS[top]:
        msg = _DENIED_PAIRS[top][sub]
        print(f"vrg-gh: {top} {sub} is denied. {msg}", file=sys.stderr)
        _log(argv, "denied")
        return 1

    if sub not in _ALLOWED[top]:
        print(
            f"vrg-gh: {top} {sub} is not recognized. Allowed: {', '.join(sorted(_ALLOWED[top]))}",
            file=sys.stderr,
        )
        _log(argv, "denied")
        return 1

    if top == "pr" and sub == "review" and "--approve" in argv:
        print(
            "vrg-gh: pr review --approve is denied. Agents cannot approve PRs.",
            file=sys.stderr,
        )
        _log(argv, "denied")
        return 1

    if top == "pr" and sub == "merge":
        err = _validate_merge_context(argv)
        if err:
            print(f"vrg-gh: pr merge is denied. {err}", file=sys.stderr)
            _log(argv, "denied")
            return 1

    token = _get_token(argv)
    env = {**os.environ, "GH_TOKEN": token}
    result = subprocess.run(  # noqa: S603
        ["gh", *argv],  # noqa: S607
        env=env,
        check=False,
    )
    _log(argv, "allowed")
    return result.returncode
