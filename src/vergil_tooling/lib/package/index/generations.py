"""Keep earlier index generations' content-addressed files published (#4230).

The site is rebuilt from scratch on every run and deployed to GitHub Pages as
one artifact, so a file the new build does not write is gone once it is live.
GitHub Pages' CDN caches each file on its own (``Cache-Control:
max-age=600``), so for minutes after a deployment a client can still be served
the previous ``InRelease`` or ``repomd.xml``. Those name their indexes by
digest: apt's ``by-hash/SHA256/<sha256>`` files (see ``apt``) and
createrepo_c's ``repodata/<sha256>-<type>.xml.gz`` files. A client holding an
earlier generation's ``InRelease`` or ``repomd.xml`` must still find them, so
every build republishes the content-addressed files of the earlier
generations it keeps.

Where the earlier generations come from: the published site itself. Every
build writes :data:`MANIFEST` at the site root, listing the content-addressed
files of each generation it publishes, newest first. The next build reads the
live manifest from the base URL (with a cache-busting query, so the CDN serves
the origin's copy), keeps the newest ``generations - 1`` of its generations,
downloads each of their files that the new build did not write itself, and
checks every one against the SHA-256 its name carries. Older generations are
not carried over: they are pruned when the new deployment replaces the site.
The new manifest is the new build's own generation followed by the ones kept.

A site with no manifest yet (HTTP 404) has no earlier by-hash generation:
its ``Release`` predates ``Acquire-By-Hash``, so its apt clients fetch indexes
by their plain names. Any other failure (a malformed manifest, a listed file
that is missing or does not match its digest, a network error after retries)
fails the build.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import retry
from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    # GET a URL: its bytes, or ``None`` when it does not exist (HTTP 404).
    Fetch = Callable[[str], bytes | None]

MANIFEST = "index-generations.json"
SCHEMA = 1

# Content-addressed files, as paths relative to the site root. The digest in
# each name is the SHA-256 of the file's bytes.
_DEB = re.compile(
    r"deb/dists/[a-z0-9][a-z0-9.-]*/main/binary-[a-z0-9]+/by-hash/SHA256/(?P<sha256>[0-9a-f]{64})"
)
_RPM = re.compile(
    r"rpm/[a-z0-9]+/[a-z0-9_]+/repodata/(?P<sha256>[0-9a-f]{64})-[a-z0-9_]+(?:\.[a-z0-9]+)+"
)


@dataclass(frozen=True)
class Generation:
    """One published index: when it was built, and its content-addressed files."""

    date: str
    files: tuple[str, ...]


# Every other file a dnf ``repodata`` directory holds (signed by the caller).
_REPOMD = frozenset({"repomd.xml", "repomd.xml.asc"})


def _digest(path: str) -> str | None:
    """The SHA-256 a content-addressed path names, or ``None`` if it is not one."""
    m = _DEB.fullmatch(path) or _RPM.fullmatch(path)
    return m["sha256"] if m else None


def http_fetch(url: str) -> bytes | None:
    """GET ``url``; ``None`` on HTTP 404. A 5xx or network error is retried, then fatal."""
    for attempt in range(retry.MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - https base URL
                data: bytes = resp.read()
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code < 500 or attempt == retry.MAX_RETRIES:
                msg = f"fetching {url}: HTTP {exc.code}"
                raise PackageError(msg) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retry.MAX_RETRIES:
                msg = f"fetching {url}: {exc}"
                raise PackageError(msg) from exc
        time.sleep(retry.compute_delay(attempt))
    raise AssertionError("unreachable")  # pragma: no cover


def _url(base_url: str, path: str) -> str:
    # The query keeps the CDN from answering with a copy cached before the
    # last deployment.
    return f"{base_url.rstrip('/')}/{path}?generation={time.time_ns()}"


def current(site: Path, now: datetime) -> Generation:
    """The generation the build wrote under ``site``, dated ``now``: its files, sorted.

    Every ``by-hash`` file and every dnf metadata file but ``repomd.xml`` and
    its signature. A ``repodata`` file of any other shape, or one whose bytes
    do not match the digest its name carries, is fatal.
    """
    found: list[str] = []
    candidates = [*site.glob("deb/dists/*/*/*/by-hash/*/*"), *site.glob("rpm/*/*/repodata/*")]
    for p in candidates:
        if p.name in _REPOMD and p.parent.name == "repodata":
            continue
        rel = p.relative_to(site).as_posix()
        sha256 = _digest(rel)
        if sha256 is None:
            msg = f"{rel} is not a content-addressed index file"
            raise PackageError(msg)
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha256:
            msg = f"{rel} does not match the SHA-256 its name carries"
            raise PackageError(msg)
        found.append(rel)
    return Generation(now.isoformat(), tuple(sorted(found)))


def _parse(raw: bytes, where: str) -> list[Generation]:
    try:
        data: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"{where} is not valid JSON: {exc}"
        raise PackageError(msg) from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        msg = f"{where} is not a schema-{SCHEMA} index generations manifest"
        raise PackageError(msg)
    gens = data.get("generations")
    if not isinstance(gens, list):
        msg = f"{where}: generations must be a list"
        raise PackageError(msg)
    out: list[Generation] = []
    for i, gen in enumerate(gens):
        date = gen.get("date") if isinstance(gen, dict) else None
        files = gen.get("files") if isinstance(gen, dict) else None
        if not isinstance(date, str) or not isinstance(files, list):
            msg = f"{where}: generation {i} needs a date string and a list of files"
            raise PackageError(msg)
        for path in files:
            if not isinstance(path, str) or _digest(path) is None:
                msg = f"{where}: generation {i} lists {path!r}, not a content-addressed index file"
                raise PackageError(msg)
        out.append(Generation(date, tuple(files)))
    return out


def load_previous(base_url: str, fetch: Fetch) -> list[Generation]:
    """The live site's generations, newest first; ``[]`` if it has no manifest yet."""
    url = _url(base_url, MANIFEST)
    raw = fetch(url)
    if raw is None:
        print(f"no {MANIFEST} at {base_url}: no earlier index generation to keep")
        return []
    return _parse(raw, url.partition("?")[0])


def carry(
    site: Path, base_url: str, previous: list[Generation], keep: int, fetch: Fetch
) -> list[Generation]:
    """Republish under ``site`` the files of the newest ``keep - 1`` earlier generations.

    A file the new build already wrote is not downloaded again: its name is
    its digest, so its bytes are the same. Returns the generations kept.
    """
    kept = previous[: keep - 1]
    fetched = 0
    for path in dict.fromkeys(p for gen in kept for p in gen.files):
        dest = site / path
        if dest.exists():
            continue
        data = fetch(_url(base_url, path))
        if data is None:
            msg = f"{path}, listed in the live {MANIFEST}, is missing from {base_url}"
            raise PackageError(msg)
        if hashlib.sha256(data).hexdigest() != _digest(path):
            msg = f"{path} from {base_url} does not match the SHA-256 its name carries"
            raise PackageError(msg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        fetched += 1
    print(
        f"keeping {len(kept)} earlier index generation(s) of {len(previous)}: "
        f"{fetched} file(s) carried over"
    )
    return kept


def write_manifest(site: Path, current: Generation, kept: list[Generation]) -> Path:
    """Write :data:`MANIFEST`: ``current`` then ``kept``, newest first."""
    gens = [{"date": g.date, "files": list(g.files)} for g in (current, *kept)]
    out = site / MANIFEST
    out.write_text(
        json.dumps({"schema": SCHEMA, "generations": gens}, indent=2) + "\n", encoding="utf-8"
    )
    return out
