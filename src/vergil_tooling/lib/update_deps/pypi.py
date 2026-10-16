"""Latest-release lookups on PyPI for the python held-back report (spec §8.1).

Epic vergil-project/.github#381. A lookup failure fails loud: a failed lookup
must never read as "nothing held back" (spec §3.7).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable

from packaging.utils import canonicalize_name

from vergil_tooling.lib.update_deps.context import UpdateDepsError

Fetch = Callable[[str], bytes]


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https base URL
        data: bytes = resp.read()
        return data


def latest_version(name: str, *, fetch: Fetch = _fetch) -> str:
    """The latest release of *name* on PyPI (``info.version``). Fails loud on any error."""
    canonical = canonicalize_name(name)
    url = f"https://pypi.org/pypi/{canonical}/json"
    command = f"GET {url}"
    try:
        payload = json.loads(fetch(url))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # HTTPError is a URLError; json.JSONDecodeError is a ValueError.
        raise UpdateDepsError(
            phase="update",
            command=command,
            message=f"PyPI lookup failed for {canonical}: {exc}",
        ) from exc
    info = payload.get("info") if isinstance(payload, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if not isinstance(version, str) or not version:
        raise UpdateDepsError(
            phase="update",
            command=command,
            message=f"PyPI response for {canonical} has no info.version.",
        )
    return version
