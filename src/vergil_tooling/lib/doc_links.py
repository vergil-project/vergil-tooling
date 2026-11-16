"""Links to the mike-versioned Vergil docs sites (issue #3831).

Each mike-versioned site serves its pages under a version segment
(``latest``, ``dev`` or a version number such as ``2.1``). A deep link with
no version segment returns 404; only the bare site root redirects. This
module finds such links in markdown text, against a fixed list of the
versioned sites, so the check needs no network.
"""

from __future__ import annotations

import re

#: The Vergil docs sites that mike versions. The org's ``docs`` and
#: ``packages`` sites are not versioned, so any path on them is valid. Add a
#: site here when its repository starts publishing versioned docs.
VERSIONED_SITES: frozenset[str] = frozenset(
    {
        "vergil-actions",
        "vergil-claude-plugin",
        "vergil-containers",
        "vergil-fixture-python-app",
        "vergil-fixture-python-lib",
        "vergil-fixture-rust-app",
        "vergil-fixture-rust-lib",
        "vergil-tooling",
        "vergil-vm",
    }
)

#: The version aliases mike publishes besides numbered versions.
VERSION_ALIASES: frozenset[str] = frozenset({"latest", "dev"})

_VERSION_NUMBER = re.compile(r"\d+(?:\.\d+)*")

# A URL on the org's Pages host: the site name, then the rest of the URL up to
# whitespace or a character that closes a markdown link, an HTML attribute or
# a code span. The host is matched case-insensitively, as DNS is.
_SITE_URL = re.compile(
    r"https?://(?i:vergil-project\.github\.io)/"
    r"(?P<site>[A-Za-z0-9._-]+)"
    r"(?P<rest>[^\s<>()\[\]\"'`]*)"
)


def unversioned_links(text: str) -> list[tuple[int, str]]:
    """Return ``(line, url)`` for every link in *text* to a versioned site
    whose first path segment is not a version, in text order.

    ``line`` is 1-based. A bare site root (with or without a trailing slash,
    query or fragment) is allowed, since mike redirects it to the default
    version. A site not in :data:`VERSIONED_SITES` is never flagged.
    """
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _SITE_URL.finditer(line):
            if match.group("site") not in VERSIONED_SITES:
                continue
            rest = match.group("rest")
            if not rest.startswith("/"):
                # Nothing after the site name, or only a query or fragment.
                continue
            first = re.split(r"[/?#]", rest[1:], maxsplit=1)[0]
            if not first or first in VERSION_ALIASES or _VERSION_NUMBER.fullmatch(first):
                continue
            hits.append((number, match.group(0)))
    return hits


def versioned_form(url: str) -> str:
    """Return *url* with ``latest/`` inserted after the site name, the fix
    the check suggests for an unversioned link."""
    match = _SITE_URL.match(url)
    if match is None:
        msg = f"not a Vergil docs site URL: {url}"
        raise ValueError(msg)
    return f"{url[: match.end('site')]}/latest{match.group('rest')}"
