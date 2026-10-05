"""apt repository metadata, generated in Python from each ``.deb`` (spec §7.2, §7.3).

Layout under ``<site>/deb``::

    pool/<name>/<file>.deb
    dists/<suite>/Release
    dists/<suite>/main/binary-{amd64,arm64}/Packages{,.gz}

- A shared build (revision ``1``) is listed in every suite; a ``native`` build
  (revision ``1~<suite>``) only in its own suite.
- ``Architecture: all`` packages are listed in every ``binary-*`` directory.
- ``Packages`` stanzas are the package's full control data followed by
  ``Filename``/``Size``/``MD5sum``/``SHA1``/``SHA256``, sorted by name then
  version, separated by blank lines. ``Packages.gz`` is byte-deterministic.
- ``Release`` lists the ``MD5Sum``/``SHA256`` of every ``Packages{,.gz}``.

Signing (``InRelease``, ``Release.gpg``) is done by the caller; see ``sign``.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import shutil
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index.collect import _call
from vergil_tooling.lib.package.index.retention import version_key
from vergil_tooling.lib.package.repo_setup import local_run
from vergil_tooling.lib.package.targets import ARCHES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.package.index.collect import Artifact
    from vergil_tooling.lib.package.repo_setup import Run

COMPONENT = "main"


def deb_control(path: Path, run: Run = local_run) -> str:
    """The full control data of a ``.deb`` (``dpkg-deb -f`` with no field list)."""
    what = f"reading the control data of {path.name}"
    return str(_call(run, what, "dpkg-deb", "-f", str(path)).stdout)


def _suite_of(a: Artifact, suites: list[str]) -> str | None:
    """The one suite a native build belongs to, or ``None`` for a shared build."""
    _, sep, suite = a.release.partition("~")
    if not sep:
        return None
    if suite not in suites:
        msg = (
            f"{a.path.name} ({a.product}@{a.tag}) is a native build for suite "
            f"{suite!r}, which is not an indexed suite ({', '.join(suites)})"
        )
        raise PackageError(msg)
    return suite


def _stanza(a: Artifact, control: str, filename: str) -> str:
    data = a.path.read_bytes()
    lines = [line for line in control.strip("\n").splitlines() if line.strip()]
    lines += [
        f"Filename: {filename}",
        f"Size: {len(data)}",
        f"MD5sum: {hashlib.md5(data, usedforsecurity=False).hexdigest()}",
        f"SHA1: {hashlib.sha1(data, usedforsecurity=False).hexdigest()}",
        f"SHA256: {hashlib.sha256(data).hexdigest()}",
    ]
    return "\n".join(lines) + "\n"


def _gzip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def _release(vendor: str, suite: str, dist: Path, now: datetime) -> str:
    files = sorted(
        p.relative_to(dist).as_posix() for p in dist.glob(f"{COMPONENT}/binary-*/Packages*")
    )
    md5: list[str] = []
    sha256: list[str] = []
    for rel in files:
        data = (dist / rel).read_bytes()
        size = f"{len(data):>10}"
        md5.append(f" {hashlib.md5(data, usedforsecurity=False).hexdigest()} {size} {rel}")
        sha256.append(f" {hashlib.sha256(data).hexdigest()} {size} {rel}")
    head = [
        f"Origin: {vendor}",
        f"Label: {vendor}",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {format_datetime(now.astimezone(UTC), usegmt=True)}",
        f"Architectures: {' '.join(ARCHES)}",
        f"Components: {COMPONENT}",
    ]
    return "\n".join([*head, "MD5Sum:", *md5, "SHA256:", *sha256]) + "\n"


def write(
    site: Path,
    artifacts: list[Artifact],
    suites: list[str],
    control: Callable[[Path], str] = deb_control,
    *,
    vendor: str,
    now: datetime | None = None,
) -> list[Path]:
    """Write the apt repository for ``artifacts`` (``.deb`` only) under ``site/deb``.

    Returns each suite's ``Release`` path, in ``suites`` order.
    """
    debs = sorted(
        (a for a in artifacts if a.fmt == "deb"),
        key=lambda a: (a.name, version_key(a.full_version), a.full_version, a.arch),
    )
    root = site / "deb"
    entries: list[tuple[Artifact, str | None, str]] = []
    for a in debs:
        filename = f"pool/{a.name}/{a.path.name}"
        dest = root / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a.path, dest)
        entries.append((a, _suite_of(a, suites), _stanza(a, control(a.path), filename)))
    when = now if now is not None else datetime.now(UTC)
    releases: list[Path] = []
    for suite in suites:
        dist = root / "dists" / suite
        for arch in ARCHES:
            stanzas = [
                text for a, own, text in entries if own in (None, suite) and a.arch in (arch, "all")
            ]
            body = "\n".join(stanzas).encode()
            out = dist / COMPONENT / f"binary-{arch}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "Packages").write_bytes(body)
            (out / "Packages.gz").write_bytes(_gzip(body))
        release = dist / "Release"
        release.write_text(_release(vendor, suite, dist, when), encoding="utf-8")
        releases.append(release)
    return releases
