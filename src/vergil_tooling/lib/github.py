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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

from vergil_tooling.lib import retry

log = logging.getLogger(__name__)

_token_cache: dict[str, tuple[str, float]] = {}
_installation_cache: dict[str, str] | None = None


class NoInstallationError(Exception):
    """The GitHub App has no installation for the requested owner."""

    def __init__(self, org: str, known: list[str]) -> None:
        super().__init__(f"no App installation for owner {org!r}")
        self.org = org
        self.known = known


def _load_app_config() -> tuple[str, Path] | None:
    """Read GitHub App credentials from env vars or ~/.config/vergil/.

    Returns ``(app_id, key_path)`` or ``None`` when App mode is
    not configured.
    """
    app_id = os.environ.get("VRG_APP_ID", "")
    key_path_str = os.environ.get("VRG_PRIVATE_KEY_PATH", "")

    if app_id and key_path_str:
        return app_id, Path(key_path_str).expanduser()

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

    if not app_id:
        return None

    return app_id, key_file


def _detect_org() -> str | None:
    """Detect the GitHub org from the current repo's git remote."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    url = result.stdout.strip()
    # git@github.com:ORG/REPO.git or https://github.com/ORG/REPO.git
    for prefix in ("git@github.com:", "https://github.com/"):
        if url.startswith(prefix):
            remainder = url[len(prefix) :]
            org = remainder.split("/")[0]
            if org:
                return org
    return None


def _jwt_api_request(endpoint: str, jwt_token: str, *, method: str = "GET") -> Any:
    """Make a GitHub API request with JWT Bearer authentication.

    The ``gh`` CLI sends ``GH_TOKEN`` values as ``Authorization: token``
    but GitHub requires JWTs to use ``Authorization: Bearer``.
    """
    url = f"https://api.github.com{endpoint}"
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, method=method, data=data)  # noqa: S310
    req.add_header("Authorization", f"Bearer {jwt_token}")
    req.add_header("Accept", "application/vnd.github+json")

    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read())


def _resolve_installations(jwt_token: str) -> dict[str, str]:
    """Fetch all App installations and return an org → installation ID map."""
    global _installation_cache  # noqa: PLW0603
    if _installation_cache is not None:
        return _installation_cache

    data = _jwt_api_request("/app/installations", jwt_token)

    mapping: dict[str, str] = {}
    for item in data:
        login = item.get("account", {}).get("login", "")
        inst_id = item.get("id")
        if login and inst_id is not None:
            mapping[login] = str(inst_id)

    _installation_cache = mapping
    return mapping


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


def get_installation_token(
    org: str | None = None, *, require_installation: bool = False
) -> str | None:
    """Return a GitHub App installation token, or ``None`` if not in App mode.

    Resolves the installation ID dynamically by calling
    ``GET /app/installations`` and matching the org. If *org* is
    ``None``, detects it from the current repo's git remote.
    Tokens are cached per-org for 55 minutes.

    With *require_installation*, an org that has no App installation
    raises :class:`NoInstallationError` instead of returning ``None``
    — an App token minted for a different installation cannot reach
    the requested owner's private repos, so silently falling back
    would mask the failure as a missing grant. Without App
    credentials configured, ``None`` is still returned (ambient gh
    auth is not installation-scoped).
    """
    if org is None:
        org = _detect_org()
    if org is None:
        return None

    cached = _token_cache.get(org)
    if cached is not None:
        token, expiry = cached
        if time.time() < expiry:
            return token

    config = _load_app_config()
    if config is None:
        return None

    app_id, key_path = config
    try:
        jwt_token = _generate_jwt(app_id, key_path)

        installations = _resolve_installations(jwt_token)
        installation_id = installations.get(org)
        if not installation_id:
            if require_installation:
                raise NoInstallationError(org, sorted(installations))
            return None

        data = _jwt_api_request(
            f"/app/installations/{installation_id}/access_tokens",
            jwt_token,
            method="POST",
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = ((exc.stderr or "") + (exc.stdout or "")).strip()
        if detail:
            log.warning("GitHub App auth failed, falling back to ambient auth: %s", detail)
        else:
            log.warning("GitHub App auth failed, falling back to ambient auth: %s", exc)
        return None

    token = str(data.get("token", ""))
    _token_cache[org] = (token, time.time() + 3300)
    return token


def is_app_mode() -> bool:
    """Return True when running with GitHub App credentials.

    True in either of two cases:

    1. vergil-tooling holds the App private key and mints its own
       installation token (``_load_app_config`` finds credentials).
    2. The ambient token ``gh`` will use is already a GitHub App
       installation token (``ghs_`` prefix) — e.g. one minted upstream
       by ``actions/create-github-app-token`` and passed in as
       ``GH_TOKEN``.

    GitHub App installation tokens cannot read ruleset ``bypass_actors``
    (the API returns an empty list regardless of the real configuration),
    so the audit must skip that comparison whenever either case holds.
    Case 2 is the path used by the reusable ops workflows, which mint the
    token in a separate step rather than handing the private key to
    vergil-tooling.
    """
    if _load_app_config() is not None:
        return True
    # ``gh`` resolves GH_TOKEN ahead of GITHUB_TOKEN; mirror that order.
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    return token.startswith("ghs_")


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


_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 180


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


def _checks_registered(repo: str, sha: str) -> bool:
    """Return True if at least one check run exists for *sha*."""
    result = _run_with_retry(
        (
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}/check-runs",  # noqa: S607
            "--jq",
            ".total_count",
        ),
        check=True,
        text=True,
        capture_output=True,
    )
    return int(result.stdout.strip()) > 0


def wait_for_checks(
    pr: str,
    *,
    poll_interval: int = _POLL_INTERVAL_SECS,
    poll_timeout: int = _POLL_TIMEOUT_SECS,
) -> None:
    """Block until all checks on *pr* reach a terminal state.

    Resolves the PR's HEAD commit SHA and polls the GitHub REST API
    until at least one check run exists for that commit.  Then hands
    off to ``gh pr checks --watch`` which blocks until every check
    completes.

    Transient GitHub API errors (502/503/504/429) are retried
    automatically via the library-level retry wrapper.
    """
    repo = current_repo()
    sha = head_sha(pr)

    deadline = time.monotonic() + poll_timeout
    while not _checks_registered(repo, sha):
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)

    if not _checks_registered(repo, sha):
        cmd = ("gh", "pr", "checks", pr, "--watch")
        raise GitHubAPIError(
            1,
            cmd,
            stderr=(
                f"no checks reported for {sha[:8]} after {poll_timeout}s"
                " — GitHub may be experiencing delays"
            ),
        )

    run("pr", "checks", pr, "--watch")


_FAILED_BUCKETS = frozenset({"fail", "cancel"})


def failed_check_names(pr: str) -> list[str]:
    """Return the names of checks on *pr* whose result is a failure.

    ``gh pr checks --watch`` (without ``--fail-fast``) exits 0 even when a
    check fails — its non-zero exit code 8 signals *pending*, not *failure* —
    so a green verdict cannot be inferred from the watch command's exit
    status.  This reads each check's ``bucket`` (gh's categorization of the
    check ``state`` into ``pass``/``fail``/``pending``/``skipping``/``cancel``)
    and returns the names whose bucket is ``fail`` or ``cancel``.  An empty
    list means every check passed or was skipped.

    ``gh pr checks`` exits non-zero when checks are failing or pending but
    still emits the requested JSON on stdout, so the call tolerates a
    non-zero exit and derives the verdict from the data rather than the exit
    code.  Empty stdout (e.g. a transient API error) is surfaced as an error
    rather than silently treated as a pass.
    """
    cmd = ("gh", "pr", "checks", pr, "--json", "name,bucket")
    result = _run_with_retry(cmd, check=False, text=True, capture_output=True)  # noqa: S607
    out = result.stdout.strip()
    if not out:
        raise GitHubAPIError(result.returncode or 1, cmd, stderr=result.stderr)
    checks = json.loads(out)
    return [str(c["name"]) for c in checks if c.get("bucket") in _FAILED_BUCKETS]


def pr_checks(pr: str) -> list[dict[str, str]]:
    """Return the PR's checks as ``{name, bucket, state}`` dicts.

    ``gh pr checks`` exits non-zero while checks are failing or pending but
    still emits the requested JSON, so the verdict is derived from the data,
    not the exit code. When *no* checks are registered for the head commit,
    ``gh`` prints nothing and reports "no checks reported"; that is a valid
    empty result (checks may not have started), distinct from a transient API
    error, which is surfaced.
    """
    cmd = ("gh", "pr", "checks", pr, "--json", "name,bucket,state")
    result = _run_with_retry(cmd, check=False, text=True, capture_output=True)  # noqa: S607
    out = result.stdout.strip()
    if out:
        return cast("list[dict[str, str]]", json.loads(out))
    if "no checks reported" in result.stderr.lower():
        return []
    raise GitHubAPIError(result.returncode or 1, cmd, stderr=result.stderr)


def pr_reviews(pr: str) -> list[dict[str, object]]:
    """Return the PR's reviews (``id``, ``state``, ``author``, ...)."""
    result = read_json("pr", "view", pr, "--json", "reviews", "--jq", ".reviews")
    return cast("list[dict[str, object]]", result) if isinstance(result, list) else []


def post_check_run(
    repo: str,
    *,
    name: str,
    head_sha: str,
    conclusion: str,
    title: str,
    summary: str,
) -> None:
    """Post a completed check-run to ``repo`` for ``head_sha``.

    Check-runs are GitHub-App-only; ``write_json`` injects the App installation
    token via :func:`_gh_env`. The check is bound to ``head_sha``, so a later
    push leaves the new commit unchecked until a fresh run is posted — the
    per-commit semantics the merge gate relies on (§10).
    """
    write_json(
        "POST",
        f"repos/{repo}/check-runs",
        {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary},
        },
    )


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


def head_sha(pr: str) -> str:
    """Return the HEAD commit SHA for a PR."""
    return read_output("pr", "view", pr, "--json", "headRefOid", "--jq", ".headRefOid")


def update_branch(pr: str) -> None:
    """Fast-forward merge the base branch into the PR branch.

    Uses the GitHub REST API ``PUT /repos/{owner}/{repo}/pulls/{number}/update-branch``.
    Only appropriate when the branch is behind the base — not when there are
    merge conflicts.
    """
    number = read_output("pr", "view", pr, "--json", "number", "--jq", ".number")
    repo = current_repo()
    read_output("api", f"repos/{repo}/pulls/{number}/update-branch", "-X", "PUT")


def pr_state(pr: str) -> str:
    """Return the PR state: ``OPEN``, ``CLOSED``, or ``MERGED``."""
    return read_output("pr", "view", pr, "--json", "state", "--jq", ".state")


def pr_for_branch(branch: str) -> dict[str, str] | None:
    """Return the open PR whose head is *branch*, or None.

    GitHub permits at most one open PR per head/base pair within a
    repo, so taking the first result is safe for the same-repo
    workflow this serves.
    """
    result = read_json(
        "pr", "list", "--head", branch, "--state", "open", "--json", "number,url,title"
    )
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    if not isinstance(first, dict):
        return None
    return {
        "number": str(first.get("number", "")),
        "url": str(first.get("url", "")),
        "title": str(first.get("title", "")),
    }


def is_draft(pr: str) -> bool:
    """Return True if *pr* is a draft."""
    return read_output("pr", "view", pr, "--json", "isDraft", "--jq", ".isDraft") == "true"


def head_ref(pr: str) -> str:
    """Return the PR's head branch name."""
    return read_output("pr", "view", pr, "--json", "headRefName", "--jq", ".headRefName")


def merge(pr: str, *, strategy: str) -> None:
    """Merge a PR synchronously (without ``--auto``).

    ``strategy`` is one of ``"merge"``, ``"squash"``, ``"rebase"`` — passed
    through as ``--merge``, ``--squash``, ``--rebase``.

    Does not pass ``--delete-branch`` — branch cleanup is handled by
    ``vrg-finalize-pr`` after the merge completes.
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
