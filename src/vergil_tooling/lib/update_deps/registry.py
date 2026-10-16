"""Image-ref parsing and anonymous Docker Registry v2 tag listing (spec §8.3).

Epic vergil-project/.github#381. The opener is injected so tests never touch
the network. Any lookup failure fails loud (spec §3.7): a broken lookup must
never look like "no newer tag".
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib.update_deps.context import UpdateDepsError

Opener = Callable[[urllib.request.Request], tuple[int, dict[str, str], bytes]]

_DOCKER_HUB = "registry-1.docker.io"
_DOCKER_HUB_ALIASES = ("docker.io", "index.docker.io", _DOCKER_HUB)
_SKIP_PREFIXES = ("ghcr.io/vergil-project/",)
_REF_RE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9._/:-]*?):(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})$"
)
_CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


@dataclass(frozen=True)
class ImageRef:
    """A tagged image reference; ``text`` is the ref exactly as written."""

    registry: str
    repository: str
    tag: str
    text: str


def parse_image_ref(text: str) -> ImageRef | None:
    """Parse a tagged image ref; None for refs this updater skips (spec §8.3 table).

    Skipped: untagged refs, ``:latest``, digests, expressions (``${{ … }}``,
    ``$ARG``) and ``ghcr.io/vergil-project/*``.
    """
    if "$" in text or "@" in text or text.startswith(_SKIP_PREFIXES):
        return None
    match = _REF_RE.match(text)
    if match is None or match["tag"] == "latest":
        return None
    name = match["name"]
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        reg, repo = first, rest
    else:
        reg, repo = _DOCKER_HUB, name
    if reg in _DOCKER_HUB_ALIASES:
        reg = _DOCKER_HUB
        if "/" not in repo:
            repo = f"library/{repo}"
    return ImageRef(reg, repo, match["tag"], text)


def _open(req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
    """Default opener: one HTTPS request, an HTTP error status returned (not raised)."""
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - https only (checked)
            return int(resp.status), dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _fail(ref: ImageRef, why: str) -> UpdateDepsError:
    where = f"{ref.registry}/{ref.repository}"
    return UpdateDepsError(
        phase="update",
        command=f"list tags {where}",
        message=f"Tag lookup failed for {where}: {why}",
    )


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup; empty when absent."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower(), "")


def _json_object(ref: ImageRef, body: bytes, what: str) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise _fail(ref, f"{what} is not a JSON object") from exc
    if not isinstance(data, dict):
        raise _fail(ref, f"{what} is not a JSON object")
    return data


def _request(
    ref: ImageRef, url: str, what: str, headers: dict[str, str] | None = None
) -> urllib.request.Request:
    """Build a request, refusing any non-https URL (realms and links are server-supplied)."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise _fail(ref, f"{what} is not an https URL: {url!r}")
    return urllib.request.Request(url, headers=headers or {})  # noqa: S310 - https checked above


def _token(ref: ImageRef, challenge: str, opener: Opener) -> str:
    if not challenge.startswith("Bearer "):
        raise _fail(ref, f"unsupported auth challenge (need Bearer): {challenge!r}")
    params = dict(_CHALLENGE_RE.findall(challenge))
    query = urllib.parse.urlencode({k: params[k] for k in ("service", "scope") if k in params})
    req = _request(ref, f"{params.get('realm', '')}?{query}", "auth challenge realm")
    status, _, body = opener(req)
    if status != 200:
        raise _fail(ref, f"token endpoint returned HTTP {status}")
    data = _json_object(ref, body, "token response")
    token = data.get("token") or data.get("access_token")
    if not isinstance(token, str):
        raise _fail(ref, "token endpoint returned no token")
    return token


def _list_tags(ref: ImageRef, opener: Opener) -> list[str]:
    url = f"https://{ref.registry}/v2/{ref.repository}/tags/list?n=1000"
    headers: dict[str, str] = {}
    tags: list[str] = []
    authenticated = False
    while True:
        status, resp_headers, body = opener(_request(ref, url, "tag-list URL", headers))
        if status == 401 and not authenticated:
            challenge = _header(resp_headers, "WWW-Authenticate")
            headers = {"Authorization": f"Bearer {_token(ref, challenge, opener)}"}
            authenticated = True
            continue
        if status != 200:
            raise _fail(ref, f"HTTP {status}")
        page = _json_object(ref, body, "tag list").get("tags") or []
        tags.extend(t for t in page if isinstance(t, str))
        nxt = _NEXT_RE.search(_header(resp_headers, "Link"))
        if nxt is None:
            return tags
        url = urllib.parse.urljoin(f"https://{ref.registry}/", nxt.group(1))


def list_tags(ref: ImageRef, *, opener: Opener = _open) -> list[str]:
    """Every tag the registry lists for *ref*'s repository (anonymous pull token).

    Raises ``UpdateDepsError`` on any failure: an error status, an unsupported
    auth challenge, a malformed body, or a network error.
    """
    try:
        return _list_tags(ref, opener)
    except OSError as exc:  # URLError, timeouts, connection resets
        raise _fail(ref, str(exc)) from exc
