"""Crate versions from Cargo's sparse index, for the cargo held-back report.

Epic vergil-project/.github#1165, spec §6.6. The newest versions come from
``https://index.crates.io``, the static, CDN-served data ``cargo update``
itself reads, not from the crates.io web API: crates.io's policy caps API
clients at one request per second and asks for an identifying User-Agent
with contact details (RFC 3463). Each index line is one published version,
with its number, its ``yanked`` flag and its ``rust_version``.

A fetch or parse failure raises :class:`SparseIndexError`: a failed lookup
must never read as "nothing held back".
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from collections.abc import Sequence

INDEX_URL = "https://index.crates.io"

Fetch = Callable[[str], bytes]

_NUM = r"0|[1-9][0-9]*"
_IDENT = r"[0-9A-Za-z-]+"
_VERSION_RE = re.compile(
    rf"^(?P<major>{_NUM})\.(?P<minor>{_NUM})\.(?P<patch>{_NUM})"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    rf"(?:\+{_IDENT}(?:\.{_IDENT})*)?$"
)
# A Cargo ``rust-version``: one to three numeric parts, such as ``1.85`` or ``1.85.0``.
_RUST_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")

# One pre-release identifier's sort key: numeric identifiers sort below
# alphanumeric ones, numerically among themselves (semver §11).
type _IdentKey = tuple[int, int, str]
type VersionKey = tuple[int, int, int, int, tuple[_IdentKey, ...]]


class SparseIndexError(UpdateDepsError):
    """A sparse-index read failed; ``command`` is the URL fetched."""

    def __init__(self, url: str, message: str) -> None:
        super().__init__(phase="update", command=url, message=message)


@dataclass(frozen=True)
class IndexVersion:
    """One published version of a crate, as its index line gives it."""

    vers: str
    yanked: bool
    rust_version: str | None


def index_path(crate: str) -> str:
    """The index file path for *crate*, by Cargo's rule, on the lowercased name.

    One- and two-character names live under ``1/`` and ``2/``, three-character
    names under ``3/<first char>/``, and every longer name under
    ``<first two>/<next two>/``.
    """
    name = crate.lower()
    if len(name) <= 2:
        return f"{len(name)}/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def index_url(crate: str) -> str:
    """The sparse-index URL for *crate*."""
    return f"{INDEX_URL}/{index_path(crate)}"


def fetch_url(url: str) -> bytes:
    """GET *url* and return its body."""
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https base URL
        data: bytes = resp.read()
        return data


def is_version(text: str) -> bool:
    """Whether *text* is a full semver version (``major.minor.patch[-pre][+build]``)."""
    return _VERSION_RE.match(text) is not None


def is_rust_version(text: str) -> bool:
    """Whether *text* is a Cargo ``rust-version`` (``1``, ``1.85`` or ``1.85.0``)."""
    return _RUST_VERSION_RE.match(text) is not None


def _ident_key(ident: str) -> _IdentKey:
    return (0, int(ident), "") if ident.isdigit() else (1, 0, ident)


def version_key(vers: str) -> VersionKey:
    """A sort key that orders semver versions by precedence; build metadata is ignored.

    A release sorts above every pre-release of the same ``major.minor.patch``.
    A string that is not a semver version raises :class:`ValueError`.
    """
    m = _VERSION_RE.match(vers)
    if m is None:
        msg = f"not a semver version: {vers!r}"
        raise ValueError(msg)
    pre = m["pre"]
    idents = tuple(_ident_key(i) for i in pre.split(".")) if pre else ()
    return (int(m["major"]), int(m["minor"]), int(m["patch"]), 0 if pre else 1, idents)


def is_prerelease(vers: str) -> bool:
    """Whether the semver version *vers* is a pre-release."""
    return version_key(vers)[3] == 0


def rust_version_key(text: str) -> tuple[int, int, int]:
    """A sort key for a ``rust-version``, its missing parts read as 0."""
    if not is_rust_version(text):
        msg = f"not a rust-version: {text!r}"
        raise ValueError(msg)
    parts = [int(p) for p in text.split(".")]
    parts.extend([0] * (3 - len(parts)))
    return (parts[0], parts[1], parts[2])


def _parse_line(line: str, *, crate: str, url: str, lineno: int) -> IndexVersion:
    where = f"sparse index entry for {crate} ({url}), line {lineno}"
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SparseIndexError(url, f"Malformed {where}: not valid JSON ({exc.msg}).") from exc
    if not isinstance(data, dict):
        raise SparseIndexError(url, f"Malformed {where}: not a JSON object.")
    vers = data.get("vers")
    if not isinstance(vers, str) or not is_version(vers):
        raise SparseIndexError(url, f"Malformed {where}: 'vers' is not a version: {vers!r}.")
    yanked = data.get("yanked")
    if not isinstance(yanked, bool):
        raise SparseIndexError(url, f"Malformed {where}: 'yanked' is not a boolean.")
    rust_version = data.get("rust_version")
    if rust_version is not None and (
        not isinstance(rust_version, str) or not is_rust_version(rust_version)
    ):
        raise SparseIndexError(
            url, f"Malformed {where}: 'rust_version' is not a rust-version: {rust_version!r}."
        )
    return IndexVersion(vers=vers, yanked=yanked, rust_version=rust_version)


def versions(crate: str, fetch: Fetch = fetch_url) -> list[IndexVersion]:
    """Every published version of *crate* in the sparse index, in index order.

    A fetch failure, an undecodable body, a malformed line or a crate with no
    versions raises :class:`SparseIndexError`.
    """
    url = index_url(crate)
    try:
        body = fetch(url).decode()
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        # HTTPError is a URLError, so a 404 for an unknown crate lands here.
        raise SparseIndexError(url, f"Sparse index read failed for {crate}: {exc}") from exc
    found = [
        _parse_line(line, crate=crate, url=url, lineno=lineno)
        for lineno, line in enumerate(body.splitlines(), start=1)
        if line.strip()
    ]
    if not found:
        raise SparseIndexError(url, f"Sparse index entry for {crate} lists no versions.")
    return found


def newest_stable(entries: Sequence[IndexVersion]) -> IndexVersion | None:
    """The highest version that is neither yanked nor a pre-release, if any."""
    stable = [e for e in entries if not e.yanked and not is_prerelease(e.vers)]
    return max(stable, key=lambda e: version_key(e.vers), default=None)
