"""Read a repository's ``vergil.toml`` through the GitHub contents API.

Commands that act on a ``--repo`` target must evaluate *that* repo's config,
not whatever ``vergil.toml`` happens to sit in the current directory. These
helpers fetch the file for ``repo`` at an explicit ``ref`` so the result is the
same from any working directory (issue #3226).

Only ``Contents: read`` on the target repo is needed.

- :func:`fetch_config_from_ref` is lenient about absence: an HTTP 404 returns
  ``None`` so a caller can try another source (``vrg-github-repo-config``'s
  develop → cwd bootstrap resolution).
- :func:`require_config_at_ref` is strict: an absent, undecodable, or invalid
  file raises :class:`RemoteConfigError` with an actionable message. There is
  no fallback, so a missing config can never be silently replaced by another.
"""

from __future__ import annotations

import base64
import tomllib

from vergil_tooling.lib import github
from vergil_tooling.lib.config import CONFIG_FILE, ConfigError, VergilConfig, _parse_raw_config


class RemoteConfigError(Exception):
    """A repo's ``vergil.toml`` could not be fetched or parsed at a given ref."""


def is_not_found(exc: github.GitHubAPIError) -> bool:
    """Return True when a GitHub API error is an HTTP 404 (Not Found)."""
    return "404" in (exc.stderr or "") or "404" in (exc.stdout or "")


def decode_contents(
    repo: str, ref: str, what: str, content_data: dict[str, object] | list[object]
) -> str:
    """Decode a GitHub contents API response into the file's text."""
    if not isinstance(content_data, dict):
        msg = f"Unexpected response fetching {what} from {repo}@{ref}"
        raise RuntimeError(msg)
    content = content_data.get("content")
    if not isinstance(content, str):
        msg = f"No content field in {what} response from {repo}@{ref}"
        raise RuntimeError(msg)
    return base64.b64decode(content).decode()


def decode_config(
    repo: str, ref: str, content_data: dict[str, object] | list[object]
) -> VergilConfig:
    """Decode a GitHub contents API response into a VergilConfig."""
    raw = tomllib.loads(decode_contents(repo, ref, "config", content_data))
    return _parse_raw_config(raw, source=f"{repo}@{ref}:{CONFIG_FILE}")


def fetch_config_from_ref(repo: str, ref: str) -> VergilConfig | None:
    """Fetch and parse ``vergil.toml`` of ``repo`` at ``ref``.

    Returns ``None`` when the file is absent at that ref (HTTP 404).
    Non-404 API errors propagate unchanged.
    """
    try:
        content_data = github.read_json(
            "api",
            f"repos/{repo}/contents/{CONFIG_FILE}?ref={ref}",
        )
    except github.GitHubAPIError as exc:
        if is_not_found(exc):
            return None
        raise
    return decode_config(repo, ref, content_data)


def require_config_at_ref(repo: str, ref: str) -> VergilConfig:
    """Return ``repo``'s ``vergil.toml`` at ``ref``, or raise :class:`RemoteConfigError`.

    Strict counterpart of :func:`fetch_config_from_ref`: an absent file (HTTP
    404, which GitHub also returns for a repo the token cannot see), a
    malformed contents response, undecodable bytes, invalid TOML, or an invalid
    config all raise :class:`RemoteConfigError` naming ``repo@ref``. Any other
    API error (auth, rate limit, server) propagates unchanged — loud either way.
    """
    try:
        config = fetch_config_from_ref(repo, ref)
    except (ValueError, ConfigError, RuntimeError) as exc:
        # ValueError covers TOMLDecodeError, binascii.Error and UnicodeDecodeError.
        msg = f"{CONFIG_FILE} of {repo}@{ref} could not be parsed: {exc}"
        raise RemoteConfigError(msg) from exc
    if config is None:
        msg = (
            f"{CONFIG_FILE} not found in {repo} at {ref}. Confirm the file is "
            f"committed at that ref, that the repo exists, and that the token has "
            f"Contents: read on {repo}."
        )
        raise RemoteConfigError(msg)
    return config
