"""dnf repository metadata via ``createrepo_c`` (spec §7.2, §7.3).

Layout under ``<site>/rpm``::

    pool/<file>.rpm                          every indexed rpm, flat
    <el>/{x86_64,aarch64}/repodata/          one repository per EL release and arch

Each ``(el, arch)`` repository is built by ``createrepo_c`` over a temporary
directory of symlinks to the pool files it lists, with
``--baseurl <base>/rpm/pool/``. Every package ``location`` is then
``xml:base="<base>/rpm/pool/"`` plus a bare filename, so dnf resolves it to the
shared pool (the spec §7.3 verification item) without copying any rpm per EL.

- A shared build (release ``1``) is listed in every EL; a ``native`` build
  (release ``1.<el>``) only in its own EL.
- ``noarch`` packages are listed in every arch.
- An ``(el, arch)`` with nothing to list still gets valid, empty repodata.

Signing (``repomd.xml.asc``) is done by the caller; see ``sign``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.index.collect import _call
from vergil_tooling.lib.package.targets import ARCHES

if TYPE_CHECKING:
    from vergil_tooling.lib.package.index.collect import Artifact
    from vergil_tooling.lib.package.repo_setup import Run

_RPM_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}


def _el_of(a: Artifact, els: list[str]) -> str | None:
    """The one EL a native build belongs to, or ``None`` for a shared build."""
    _, sep, el = a.release.partition(".")
    if not sep:
        return None
    if el not in els:
        msg = (
            f"{a.path.name} ({a.product}@{a.tag}) is a native build for {el!r}, "
            f"which is not an indexed EL release ({', '.join(els)})"
        )
        raise PackageError(msg)
    return el


def write(
    site: Path, artifacts: list[Artifact], base_url: str, els: list[str], run: Run
) -> list[Path]:
    """Write the dnf repositories for ``artifacts`` (``.rpm`` only) under ``site/rpm``.

    Returns each ``repomd.xml`` path, ``els`` outermost, then arch.
    """
    root = site / "rpm"
    pool = root / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    rpms: list[tuple[Artifact, str | None, Path]] = []
    for a in sorted((a for a in artifacts if a.fmt == "rpm"), key=lambda a: a.path.name):
        dest = pool / a.path.name
        shutil.copyfile(a.path, dest)
        rpms.append((a, _el_of(a, els), dest))
    pool_url = base_url.rstrip("/") + "/rpm/pool/"
    out: list[Path] = []
    for el in els:
        for arch in ARCHES:
            rpm_arch = _RPM_ARCH[arch]
            with tempfile.TemporaryDirectory(prefix=f"repo-{el}-{rpm_arch}-") as tmp:
                work = Path(tmp)
                for a, own, file in rpms:
                    if own in (None, el) and a.arch in (arch, "all"):
                        (work / file.name).symlink_to(file.resolve())
                _call(
                    run,
                    f"createrepo_c for {el}/{rpm_arch}",
                    "createrepo_c",
                    "--baseurl",
                    pool_url,
                    "--general-compress-type",
                    "gz",
                    "--outputdir",
                    str(work),
                    str(work),
                )
                repodata = work / "repodata"
                if not (repodata / "repomd.xml").is_file():
                    msg = f"createrepo_c for {el}/{rpm_arch} produced no {repodata}/repomd.xml"
                    raise PackageError(msg)
                dest = root / el / rpm_arch / "repodata"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(repodata, dest)
                out.append(dest / "repomd.xml")
    return out
