"""Links to the Vergil docs sites (issues #3831 and #4599).

Each mike-versioned site serves its pages under a version segment
(``latest``, ``dev`` or a version number such as ``2.1``). A deep link with
no version segment returns 404; only the bare site root redirects. A link to
a site name the org does not publish at all returns 404 whatever its path.
This module finds both kinds of link in markdown text, against fixed lists
of the published sites, so the check needs no network.
"""

from __future__ import annotations

import re

#: The Vergil docs sites that mike versions. Add a site here when its
#: repository starts publishing versioned docs (``repo_init`` creates such
#: sites): until then the check flags every link to it as unpublished.
VERSIONED_SITES: frozenset[str] = frozenset(
    {
        "vergil-actions",
        "vergil-claude-plugin",
        "vergil-containers",
        "vergil-fixture-python-app",
        "vergil-fixture-python-lib",
        "vergil-fixture-rust-app",
        "vergil-fixture-rust-lib",
        "vergil-fixture-ts-app",
        "vergil-fixture-ts-lib",
        "vergil-tooling",
        "vergil-vm",
    }
)

#: The published Vergil sites that mike does not version, so any path on
#: them is valid.
UNVERSIONED_SITES: frozenset[str] = frozenset({"docs", "packages"})

#: Every site the org publishes on its Pages host. A link to any other site
#: name returns 404.
PUBLISHED_SITES: frozenset[str] = VERSIONED_SITES | UNVERSIONED_SITES

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
    version. A site not in :data:`VERSIONED_SITES` is never flagged here:
    :func:`unpublished_links` flags one that is not published at all.
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


def unpublished_links(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line, site, url)`` for every link in *text* to a site on the
    org's Pages host that is not in :data:`PUBLISHED_SITES`, in text order.

    ``line`` is 1-based. The site name is compared exactly, as the Pages path
    is. A link to the host alone, with no site name, is never flagged. When
    the URL ends at the site name, trailing dots are sentence punctuation,
    not part of the name (as GitHub's autolinks treat them), so
    ``…github.io/vergil-tooling.`` is the published site.
    """
    hits: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _SITE_URL.finditer(line):
            site = match.group("site")
            url = match.group(0)
            if not match.group("rest"):
                site = site.rstrip(".")
                url = url.rstrip(".")
            if site and site not in PUBLISHED_SITES:
                hits.append((number, site, url))
    return hits


def versioned_form(url: str) -> str:
    """Return *url* with ``latest/`` inserted after the site name, the fix
    the check suggests for an unversioned link."""
    match = _SITE_URL.match(url)
    if match is None:
        msg = f"not a Vergil docs site URL: {url}"
        raise ValueError(msg)
    return f"{url[: match.end('site')]}/latest{match.group('rest')}"
