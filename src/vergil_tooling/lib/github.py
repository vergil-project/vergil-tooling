"""GitHub CLI (``gh``) subprocess wrappers.

All functions that use ``check=True`` retry transparently on transient
GitHub API errors (HTTP 502, 503, 504, 429 and network-level timeouts)
with exponential backoff.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from vergil_tooling.lib import retry

log = logging.getLogger(__name__)

_cached_token: tuple[str, float] | None = None


def _load_app_config() -> tuple[str, str, Path] | None:
    """Read GitHub App credentials from env vars or ~/.config/vergil/.

    Returns ``(app_id, installation_id, key_path)`` or ``None``
    when App mode is not configured.
    """
    app_id = os.environ.get("VRG_APP_ID", "")
    installation_id = os.environ.get("VRG_INSTALLATION_ID", "")
    key_path_str = os.environ.get("VRG_PRIVATE_KEY_PATH", "")

    if app_id and installation_id and key_path_str:
        return app_id, installation_id, Path(key_path_str).expanduser()

    env_file = Path.home() / ".config" / "vergil" / "app.env"
    key_file = Path.home() / ".config" / "vergil" / "app.pem"
    if not env_file.exists() or not key_file.exists():
        return None

    values: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k, v = stripped.split("=", 1)
            values[k.strip()] = v.strip()

    app_id = app_id or values.get("APP_ID", "")
    installation_id = installation_id or values.get("INSTALLATION_ID", "")

    if not app_id or not installation_id:
        return None

    return app_id, installation_id, key_file


def _generate_jwt(app_id: str, key_path: Path) -> str:
    """Generate an RS256 JWT for GitHub App authentication.

    Uses ``openssl`` for RSA signing to avoid adding a
    cryptography dependency.
    """
    import base64 as _b64

    def b64url(data: bytes) -> str:
        return _b64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 600, "iss": int(app_id)}).encode())

    signing_input = f"{header}.{payload}"

    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(key_path),
            "-binary",
        ],
        input=signing_input.encode(),
        capture_output=True,
        check=True,
    )

    signature = b64url(result.stdout)
    return f"{header}.{payload}.{signature}"


def get_installation_token() -> str | None:
    """Return a GitHub App installation token, or ``None`` if not in App mode.

    Reads App credentials from ``~/.config/vergil/`` (or env var
    overrides), generates a JWT, and exchanges it for a 1-hour
    installation token via the GitHub API. Tokens are cached for
    55 minutes.
    """
    global _cached_token  # noqa: PLW0603
    if _cached_token is not None:
        token, expiry = _cached_token
        if time.time() < expiry:
            return token

    config = _load_app_config()
    if config is None:
        return None

    app_id, installation_id, key_path = config
    jwt_token = _generate_jwt(app_id, key_path)

    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gh",
            "api",
            f"/app/installations/{installation_id}/access_tokens",
            "-X",
            "POST",
            "--jq",
            ".token",
        ],
        env={**os.environ, "GH_TOKEN": jwt_token},
        capture_output=True,
        text=True,
        check=True,
    )

    token = result.stdout.strip()
    _cached_token = (token, time.time() + 3300)
    return token


def _gh_env() -> dict[str, str] | None:
    """Return env dict with App installation token, or ``None`` for ambient auth."""
    token = get_installation_token()
    if token is None:
        return None
    return {**os.environ, "GH_TOKEN": token}


class GitHubAPIError(subprocess.CalledProcessError):
    """``CalledProcessError`` that includes captured API output in its message."""

    def __str__(self) -> str:
        base = super().__str__()
        parts: list[str] = []
        if self.stderr:
            parts.append(f"stderr: {self.stderr.strip()}")
        if self.stdout:
            parts.append(f"stdout: {self.stdout.strip()}")
        if parts:
            return f"{base}\n{'\n'.join(parts)}"
        return base


_NO_CHECKS_PHRASE = "no checks reported"
_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 60


def _run_with_retry(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if "env" not in kwargs:
        env = _gh_env()
        if env is not None:
            kwargs["env"] = env
    try:
        return retry.run_with_retry(*args, **kwargs)
    except subprocess.CalledProcessError as exc:
        detail = ((exc.stderr or "") + (exc.stdout or "")).strip()
        if detail:
            raise GitHubAPIError(exc.returncode, exc.cmd, exc.stdout, exc.stderr) from exc
        raise


def run(*args: str) -> None:
    """Run a gh command and raise on failure."""
    result = _run_with_retry(("gh", *args), check=True, capture_output=True, text=True)  # noqa: S607
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


def read_output(*args: str) -> str:
    """Run a gh command and return stripped stdout."""
    result = _run_with_retry(
        ("gh", *args),  # noqa: S607
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def read_json(*args: str) -> dict[str, object] | list[object]:
    """Run a gh command and return parsed JSON from stdout."""
    raw = read_output(*args)
    result: dict[str, object] | list[object] = json.loads(raw)
    return result


def write_json(method: str, endpoint: str, body: dict[str, object]) -> None:
    """Call gh api with a JSON body via stdin."""
    _run_with_retry(
        ("gh", "api", endpoint, "-X", method, "--input", "-"),  # noqa: S607
        input=json.dumps(body),
        check=True,
        text=True,
        capture_output=True,
    )


def delete(endpoint: str) -> None:
    """Call gh api with DELETE method."""
    _run_with_retry(
        ("gh", "api", endpoint, "-X", "DELETE"),  # noqa: S607
        check=True,
        text=True,
        capture_output=True,
    )


def delete_if_exists(endpoint: str) -> bool:
    """Call gh api DELETE; return True if deleted (2xx), False if 404."""
    result = subprocess.run(  # noqa: S603
        ("gh", "api", endpoint, "-X", "DELETE", "-i"),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=_gh_env(),
    )
    first_line = result.stdout.split("\n")[0] if result.stdout else ""
    return "404" not in first_line


def create_pr(*, base: str, title: str, body_file: str) -> str:
    """Create a pull request and return its URL."""
    return read_output("pr", "create", "--base", base, "--title", title, "--body-file", body_file)


def _checks_registered(pr: str) -> bool:
    """Return True if at least one check is registered on ``pr``."""
    result = subprocess.run(  # noqa: S603
        ("gh", "pr", "checks", pr),  # noqa: S607
        capture_output=True,
        text=True,
        env=_gh_env(),
    )
    return _NO_CHECKS_PHRASE not in (result.stdout + result.stderr)


def wait_for_checks(
    pr: str,
    *,
    poll_interval: int = _POLL_INTERVAL_SECS,
    poll_timeout: int = _POLL_TIMEOUT_SECS,
) -> None:
    """Block until all required checks on ``pr`` complete; fail fast on the first red.

    Polls internally when no checks have registered yet (the window between
    git push and GitHub registering the checks run). Polls every
    ``poll_interval`` seconds for up to ``poll_timeout`` seconds before
    falling through to the blocking watch.

    Transient GitHub API errors (502/503/504/429) are retried automatically
    via the library-level retry wrapper.  Persistent failures surface as
    ``subprocess.CalledProcessError``.
    """
    deadline = time.monotonic() + poll_timeout
    while not _checks_registered(pr):
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    run("pr", "checks", pr, "--watch", "--fail-fast")


def mergeable(pr: str) -> str:
    """Return the PR's mergeable status (e.g. ``MERGEABLE``, ``CONFLICTING``, ``UNKNOWN``)."""
    return read_output(
        "pr",
        "view",
        pr,
        "--json",
        "mergeable",
        "--jq",
        ".mergeable",
    )


def merge_state_status(pr: str) -> str:
    """Return the PR's mergeStateStatus (e.g. ``CLEAN``, ``BEHIND``, ``DIRTY``)."""
    return read_output(
        "pr",
        "view",
        pr,
        "--json",
        "mergeStateStatus",
        "--jq",
        ".mergeStateStatus",
    )


def merge_status(pr: str) -> dict[str, str]:
    """Return merge state and review decision for a PR.

    Single API call returning ``{"mergeStateStatus": ..., "reviewDecision": ...}``.
    """
    result = read_json(
        "pr",
        "view",
        pr,
        "--json",
        "mergeStateStatus,reviewDecision",
    )
    if not isinstance(result, dict):
        msg = f"unexpected API response for pr view {pr}"
        raise GitHubAPIError(1, f"gh pr view {pr}", msg)
    state = str(result.get("mergeStateStatus", ""))
    review = result.get("reviewDecision")
    return {"mergeStateStatus": state, "reviewDecision": str(review) if review else ""}


def current_repo() -> str:
    """Return ``OWNER/REPO`` for the current directory's git remote."""
    return read_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")


def update_branch(pr: str) -> None:
    """Fast-forward merge the base branch into the PR branch.

    Uses the GitHub REST API ``PUT /repos/{owner}/{repo}/pulls/{number}/update-branch``.
    Only appropriate when the branch is behind the base — not when there are
    merge conflicts.
    """
    number = read_output("pr", "view", pr, "--json", "number", "--jq", ".number")
    repo = current_repo()
    read_output("api", f"repos/{repo}/pulls/{number}/update-branch", "-X", "PUT")


def merge(pr: str, *, strategy: str) -> None:
    """Merge a PR synchronously (without ``--auto``).

    ``strategy`` is one of ``"merge"``, ``"squash"``, ``"rebase"`` — passed
    through as ``--merge``, ``--squash``, ``--rebase``.

    Does not pass ``--delete-branch`` — branch cleanup is handled by
    ``vrg-finalize-repo`` after the merge completes.
    """
    run("pr", "merge", f"--{strategy}", pr)


def list_project_repos(owner: str, project: str) -> list[str]:
    """Return sorted, unique repos linked to a GitHub Project."""
    jq_filter = (
        f".[] | select(.projectsV2.Nodes | length > 0) "
        f"| select(.projectsV2.Nodes[].number == {project}) "
        f"| .nameWithOwner"
    )
    output = read_output(
        "repo",
        "list",
        owner,
        "--json",
        "nameWithOwner,projectsV2",
        "--limit",
        "100",
        "--jq",
        jq_filter,
    )
    return sorted({r for r in output.splitlines() if r})
