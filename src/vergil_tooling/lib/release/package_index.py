"""Deferred ``package-index`` stage: wait for the release in the package index (spec §8.3).

A repo with a ``[package]`` block publishes OS packages; the org's
``publish-index`` workflow then adds them to the apt/dnf index asynchronously.
This stage polls the published index until the released version is visible, so
the consumer refresh that follows never races it. A miss (timeout, or any
error while polling) is recorded as a deferred publish failure — the release
itself stands, and ``consumer_refresh`` holds while any publish is deferred.
"""

from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET  # noqa: N817
from collections.abc import Callable
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

from vergil_tooling.lib import config
from vergil_tooling.lib.package import matrix, naming, orgs
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from vergil_tooling.lib.config import PackageConfig
    from vergil_tooling.lib.package.targets import Target
    from vergil_tooling.lib.release.context import ReleaseContext

Fetch = Callable[[str], bytes]

PHASE = "package-index"


def _fetch(url: str) -> bytes:
    """GET ``url``; a 404 (index or file not published yet) is ``b""``.

    Any other error propagates.
    """
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https base URL
            data: bytes = resp.read()
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return b""
        raise


def _deb_url(base_url: str, suite: str, arch: str) -> str:
    return f"{base_url}/deb/dists/{suite}/main/binary-{arch}/Packages"


def _rpm_dir(base_url: str, el: str, rpm_arch: str) -> str:
    return f"{base_url}/rpm/{el}/{rpm_arch}"


def deb_has(
    base_url: str, suite: str, arch: str, name: str, full_version: str, fetch: Fetch
) -> bool:
    """Whether the suite's ``binary-<arch>/Packages`` lists ``name`` at ``full_version``."""
    text = fetch(_deb_url(base_url, suite, arch)).decode("utf-8")
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if line[:1].isspace() or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        if fields.get("Package") == name and fields.get("Version") == full_version:
            return True
    return False


def _local(tag: str) -> str:
    """An element's tag without its ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def rpm_has(
    base_url: str,
    el: str,
    rpm_arch: str,
    name: str,
    version: str,
    release: str,
    fetch: Fetch,
) -> bool:
    """Whether ``<el>/<rpm_arch>``'s primary metadata lists ``name`` at ``version-release``.

    Reads ``repodata/repomd.xml`` to locate the primary metadata, then scans it.
    """
    repo = _rpm_dir(base_url, el, rpm_arch)
    repomd = fetch(f"{repo}/repodata/repomd.xml")
    if not repomd:
        return False
    # The index is the org's own signed Pages site, fetched over HTTPS.
    root = ET.fromstring(repomd)  # noqa: S314
    href = None
    for data in root:
        if _local(data.tag) == "data" and data.get("type") == "primary":
            location = next((c for c in data if _local(c.tag) == "location"), None)
            href = location.get("href") if location is not None else None
            if href:
                break
    if not href:
        raise ReleaseError(
            phase=PHASE,
            command="parse repomd.xml",
            message=f"{repo}/repodata/repomd.xml has no primary metadata location",
        )
    primary = fetch(f"{repo}/{href}")
    if not primary:
        return False
    meta = ET.fromstring(gzip.decompress(primary))  # noqa: S314
    for pkg in meta:
        fields = {_local(c.tag): c for c in pkg}
        name_el = fields.get("name")
        ver_el = fields.get("version")
        if (
            name_el is not None
            and ver_el is not None
            and name_el.text == name
            and ver_el.get("ver") == version
            and ver_el.get("rel") == release
        ):
            return True
    return False


def _pick_target(pkg: PackageConfig) -> Target:
    """The first selected deb target, else the first rpm target."""
    selected = matrix.select_targets(pkg)
    debs = [t for t in selected if t.fmt == "deb"]
    return debs[0] if debs else selected[0]


def _revision(pkg: PackageConfig, target: Target) -> str:
    """Package revision: ``1`` for shared builds, suite-suffixed for ``native`` ones."""
    if not any(fnmatchcase(target.key, p) for p in pkg.native):
        return "1"
    return f"1~{target.suite}" if target.fmt == "deb" else f"1.{target.suite}"


def _wait(
    ctx: ReleaseContext,
    *,
    fetch: Fetch,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    timeout: int,
    interval: int,
) -> None:
    pkg = config.read_config(ctx.repo_root).package
    if pkg is None:
        print("No [package] section — nothing to wait for in a package index.")
        return
    name = naming.package_name(ctx.repo_root, pkg)
    version = pkg.version or ctx.version
    target = _pick_target(pkg)
    release = _revision(pkg, target)
    full = f"{version}-{release}"
    org = orgs.for_vendor(pkg.vendor)
    base = org.base_url
    # A noarch package (deb ``all`` / rpm ``noarch``) is listed in every
    # per-arch index, so the concrete target arch directory is always polled.
    is_deb = target.fmt == "deb"
    if is_deb:
        where = _deb_url(base, target.suite, target.arch)
    else:
        where = f"{_rpm_dir(base, target.suite, target.rpm_arch)}/repodata/repomd.xml"

    def visible() -> bool:
        if is_deb:
            return deb_has(base, target.suite, target.arch, name, full, fetch)
        return rpm_has(base, target.suite, target.rpm_arch, name, version, release, fetch)

    print(f"Waiting for {name} {full} in {where} (timeout {timeout}s)...")
    deadline = now() + timeout
    while not visible():
        if now() >= deadline:
            raise ReleaseError(
                phase=PHASE,
                command="poll package index",
                message=f"{name} {full} not visible in {where} after {timeout}s",
                detail=(
                    f"re-run the publish-index workflow in {org.github_org}/packages; "
                    "the release itself stands"
                ),
            )
        sleep(interval)
    ctx.package_index_url = where
    print(f"{name} {full} is visible in {where}.")


def wait_for_index(
    ctx: ReleaseContext,
    *,
    fetch: Fetch = _fetch,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    timeout: int = 1200,
    interval: int = 30,
) -> None:
    """Poll the org package index until this release's package is visible.

    No-op without ``[package]``. Any failure (a timeout, or an error while
    polling) records ``package-index`` in ``ctx.deferred_publish_failures``
    before propagating, so the consumer refresh holds.
    """
    try:
        _wait(ctx, fetch=fetch, sleep=sleep, now=now, timeout=timeout, interval=interval)
    except Exception:
        if PHASE not in ctx.deferred_publish_failures:
            ctx.deferred_publish_failures.append(PHASE)
        raise
