"""Resolve the vergil-tooling release a ``[dependencies].vergil`` pin installs (#4550).

A line pin (``v2.1``) installs whatever release is newest on the line when the
cached dev image is built. The cache key folds in the release the pin resolves
to *now*, so a new release on the line rebuilds every branch cache instead of
leaving it on the old tooling. An exact pin (``v2.1.320``) and a dev ref
(``develop``) resolve to themselves, with no lookup.

The lookup reads the org apt repository's ``Packages`` index for the host's
architecture, in every Ubuntu suite the package registry targets, and takes the
newest release on the line that every suite lists: that is a release the image
build can install whichever suite its base image uses. Like the version window
(:mod:`vergil_tooling.lib.versions.window`) it is read on every run, never
cached and never replaced by a guess. A failed fetch, or a line with no
release in the index, raises :class:`ToolingResolveError`; the explicit way to
work without the index is an exact pin, which needs no lookup.
"""

from __future__ import annotations

import http.client
import re
import urllib.request
from typing import TYPE_CHECKING

from vergil_tooling.lib import vm_packages
from vergil_tooling.lib.package import orgs
from vergil_tooling.lib.package import targets as tg

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

PACKAGE = "vergil-tooling"
_VENDOR = "vergil"
_FETCH_TIMEOUT_SECONDS = 30
_RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class ToolingResolveError(RuntimeError):
    """The release a line pin resolves to could not be determined."""


def _http_fetch(url: str) -> bytes:
    """GET *url*. Every failure, a 404 included, propagates to the caller."""
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed https base URL
        data: bytes = resp.read()
        return data


def index_url(suite: str, arch: str) -> str:
    """The org apt repository's ``Packages`` index for *suite* and *arch*."""
    base_url = orgs.for_vendor(_VENDOR).base_url
    return f"{base_url}/deb/dists/{suite}/main/binary-{arch}/Packages"


def deb_suites() -> list[str]:
    """Every Ubuntu suite the package registry targets, sorted."""
    return sorted({t.suite for t in tg.all_targets() if t.fmt == "deb"})


def _release_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def releases_on_line(tag: str, versions: Iterable[str]) -> set[str]:
    """The upstream *versions* (``2.1.320``) that are releases on the line *tag* (``v2.1``)."""
    prefix = f"{tag[1:]}."
    return {v for v in versions if _RELEASE.fullmatch(v) and v.startswith(prefix)}


def _suite_releases(tag: str, suite: str, arch: str, fetch: Callable[[str], bytes]) -> set[str]:
    """The releases on the line *tag* that *suite*'s index lists for vergil-tooling."""
    # Imported here: the release package pulls in the GitHub client, which a
    # plain vrg-container-run has no other use for.
    from vergil_tooling.lib.release.package_index import deb_package_versions

    url = index_url(suite, arch)

    def read(index: str) -> bytes:
        try:
            return fetch(index)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            msg = f"cannot read the vergil package index {index}: {exc}"
            raise ToolingResolveError(msg) from exc

    base_url = orgs.for_vendor(_VENDOR).base_url
    try:
        versions = deb_package_versions(base_url, suite, arch, read).get(PACKAGE, set())
    except UnicodeDecodeError as exc:
        msg = f"cannot read the vergil package index {url}: {exc}"
        raise ToolingResolveError(msg) from exc
    return releases_on_line(tag, versions)


def resolve_tooling_tag(
    tag: str,
    *,
    arch: str,
    fetch: Callable[[str], bytes] | None = None,
) -> str:
    """Return the exact release *tag* resolves to: ``v2.1`` becomes ``v2.1.320``.

    An exact release or a dev ref is returned unchanged, without a lookup. A
    line is resolved to the newest release on it that the index of every
    Ubuntu suite (:func:`deb_suites`) lists for *arch* (``amd64``, ``arm64``).
    *fetch* (default: an HTTPS GET) reads one index. Raises :class:`ToolingResolveError` when an index cannot be read, or no
    release on the line is listed in all of them.
    """
    if vm_packages.classify_ref(tag) != "line":
        return tag
    read = fetch or _http_fetch
    per_suite: list[set[str]] = []
    for suite in deb_suites():
        releases = _suite_releases(tag, suite, arch, read)
        if not releases:
            msg = (
                f"no vergil-tooling release on the {tag} line in the vergil package "
                f"index {index_url(suite, arch)}"
            )
            raise ToolingResolveError(msg)
        per_suite.append(releases)
    common = set.intersection(*per_suite)
    if not common:
        msg = (
            f"no vergil-tooling release on the {tag} line is listed in the vergil package "
            f"index of every suite ({', '.join(deb_suites())}) for {arch}"
        )
        raise ToolingResolveError(msg)
    return f"v{max(common, key=_release_key)}"
