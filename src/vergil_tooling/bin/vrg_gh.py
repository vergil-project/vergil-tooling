"""Safe gh wrapper for AI agent sessions.

Enforces identity-aware subcommand allowlists and flag deny lists.
Injects GitHub App installation tokens when available.
"""

from __future__ import annotations

import os
import subprocess
import sys

from vergil_tooling.lib import github, identity_mode, retry

_ALLOWED: dict[str, set[str]] = {
    "issue": {"view", "create", "close", "reopen", "edit", "list", "comment"},
    "pr": {"view", "checks", "list", "diff", "comment", "edit", "review", "merge"},
    "run": {"list", "view", "watch"},
    "repo": {"view", "list"},
    "label": {"list", "create"},
}

_ALLOWED_AUDIT: dict[str, set[str]] = {
    "pr": {"view", "diff", "list", "checks", "comment", "review"},
}

_DENIED_ALWAYS: dict[str, dict[str, str]] = {
    "pr": {
        "close": "gh pr close is denied by vrg-gh.",
    },
    "repo": {
        "edit": "gh repo edit is denied by vrg-gh.",
        "create": "gh repo create is denied by vrg-gh.",
        "delete": "gh repo delete is denied by vrg-gh.",
    },
}

_DENIED_AGENT: dict[str, dict[str, str]] = {
    "pr": {
        "create": "PR creation requires a human maintainer.",
        "edit": "PR edit requires a human maintainer.",
        "merge": "PR merge requires a human maintainer.",
    },
    "issue": {
        "close": "Issue close requires a human maintainer.",
        "reopen": "Issue reopen requires a human maintainer.",
        "edit": "Issue edit requires a human maintainer.",
    },
}

_DENIED_HUMAN: dict[str, dict[str, str]] = {
    "pr": {
        "create": "Use vrg-submit-pr instead of gh pr create.",
    },
}

_DENIED_TOP: dict[str, str] = {
    "auth": "gh auth is denied by vrg-gh.",
}

# `gh api` is identity-aware (handled in main, not in _DENIED_TOP):
#   human -> full; audit -> read-only GET; user/other agent -> denied.
# These flags flip gh's default verb from GET to POST.
_API_WRITE_FLAGS: set[str] = {"-f", "-F", "--field", "--raw-field", "--input"}

_MIN_MERGE_ARGS = 3


def _get_allowed() -> dict[str, set[str]]:
    if identity_mode.current_mode() == identity_mode.IdentityMode.AUDIT:
        return _ALLOWED_AUDIT
    return _ALLOWED


def _get_denied_pairs() -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for top, subs in _DENIED_ALWAYS.items():
        merged.setdefault(top, {}).update(subs)
    extra = _DENIED_AGENT if identity_mode.is_agent() else _DENIED_HUMAN
    for top, subs in extra.items():
        merged.setdefault(top, {}).update(subs)
    return merged


def _api_is_get(argv: list[str]) -> bool:
    """Return True if a ``gh api`` invocation is a read-only GET.

    gh defaults to GET, flips to POST when fields are present, and
    honors an explicit ``-X``/``--method``.
    """
    method: str | None = None
    has_fields = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-X", "--method"):
            if i + 1 < len(argv):
                method = argv[i + 1].upper()
            i += 2
            continue
        if arg.startswith("--method="):
            method = arg.split("=", 1)[1].upper()
        elif arg in _API_WRITE_FLAGS or arg.startswith(("--field=", "--raw-field=")):
            has_fields = True
        i += 1
    if method is not None:
        return method == "GET"
    return not has_fields


def _parse_repo_owner(value: str) -> str | None:
    """Extract the owner from a ``-R``/``--repo`` value.

    gh accepts ``OWNER/REPO``, ``HOST/OWNER/REPO``, and full URL forms.
    """
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    parts = [part for part in value.split("/") if part]
    if len(parts) >= 3:  # noqa: PLR2004 -- HOST/OWNER/REPO
        return parts[1]
    if len(parts) == 2:  # noqa: PLR2004 -- OWNER/REPO
        return parts[0]
    return None


def _extract_repo_owner(argv: list[str]) -> str | None:
    """Return the owner named by ``-R``/``--repo`` in the wrapped argv.

    Handles separate-value (``-R x``, ``--repo x``), equals
    (``--repo=x``, ``-R=x``), and glued-shorthand (``-Rx``) forms;
    the last occurrence wins, matching gh's flag parsing.
    """
    value: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-R", "--repo"):
            if i + 1 < len(argv):
                value = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--repo="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-R") and arg != "-R":
            value = arg[2:].removeprefix("=")
        i += 1
    if value is None:
        return None
    return _parse_repo_owner(value)


def _exec_gh(argv: list[str]) -> int:
    """Inject the installation token and execute ``gh`` with retry.

    The App installation is selected by the ``-R``/``--repo`` owner
    when present, falling back to cwd remote detection otherwise.
    A targeted owner with no installation fails loudly: a token
    minted for a different installation cannot reach that owner's
    private repos (#1413).
    """
    owner = _extract_repo_owner(argv)
    try:
        token = github.get_installation_token(org=owner, require_installation=owner is not None)
    except github.NoInstallationError as exc:
        known = ", ".join(exc.known) if exc.known else "none"
        print(
            f"vrg-gh: the GitHub App has no installation for owner "
            f"'{exc.org}' (from -R/--repo). Known installations: {known}.",
            file=sys.stderr,
        )
        return 1
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

    # Identity-aware `gh api`: the broad escape hatch is gated per identity.
    if top == "api":
        mode = identity_mode.current_mode()
        if mode == identity_mode.IdentityMode.USER:
            print(
                "vrg-gh: gh api is denied for the user identity "
                "(broad write-capable escape hatch).",
                file=sys.stderr,
            )
            return 1
        if mode == identity_mode.IdentityMode.AUDIT and not _api_is_get(argv):
            print(
                "vrg-gh: gh api is restricted to read-only GET calls for the audit identity.",
                file=sys.stderr,
            )
            return 1
        # human (full) or audit GET: execute directly, bypassing the
        # subcommand-pair allowlist (api has no fixed sub-actions).
        return _exec_gh(argv)

    allowed = _get_allowed()

    if top not in allowed:
        print(
            f"vrg-gh: {top} is not recognized. Allowed: {', '.join(sorted(allowed))}",
            file=sys.stderr,
        )
        return 1

    if len(argv) < 2:  # noqa: PLR2004
        print(
            f"vrg-gh: {top} requires a subcommand. Allowed: {', '.join(sorted(allowed[top]))}",
            file=sys.stderr,
        )
        return 1

    sub = argv[1]

    denied_pairs = _get_denied_pairs()
    if top in denied_pairs and sub in denied_pairs[top]:
        msg = denied_pairs[top][sub]
        print(f"vrg-gh: {top} {sub} is denied. {msg}", file=sys.stderr)
        return 1

    if sub not in allowed[top]:
        print(
            f"vrg-gh: {top} {sub} is not recognized. Allowed: {', '.join(sorted(allowed[top]))}",
            file=sys.stderr,
        )
        return 1

    mode = identity_mode.current_mode()

    if (
        top == "pr"
        and sub == "review"
        and "--approve" in argv
        and mode not in (identity_mode.IdentityMode.AUDIT, identity_mode.IdentityMode.HUMAN)
    ):
        print(
            "vrg-gh: pr review --approve is denied. "
            "PR approval is restricted to the audit identity or a human maintainer.",
            file=sys.stderr,
        )
        return 1

    if (
        top == "pr"
        and sub == "merge"
        and not identity_mode.is_agent()
        and len(argv) < _MIN_MERGE_ARGS
    ):
        print(
            "vrg-gh: pr merge is denied. pr merge requires a PR number or URL.",
            file=sys.stderr,
        )
        return 1

    return _exec_gh(argv)
