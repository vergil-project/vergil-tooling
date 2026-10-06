"""Install-test one package artifact in a clean container (spec §8.1, §6.5).

:func:`run_install_test` runs a fixed sequence against one test cell, through a
:data:`~vergil_tooling.lib.package.repo_setup.Run` callable (so it can be driven
locally or faked in tests):

1. prerequisites (``apt-get update`` on deb; nothing on rpm);
2. for ``builder = "python"`` only, bootstrap the vergil repository as a
   consumer would, since the runtime resolves from the **live** repository
   (``staged`` products install standalone);
3. install the local artifact file;
4. list the installed files;
5. if systemd units are shipped, install systemd and ``systemd-analyze verify``
   each one (no systemd as PID 1 in the container, so nothing is started);
6. run ``smoke`` and resolve every ``/usr/bin`` shim under :data:`CLEAN_ENV`, so
   only packaged binaries resolve — never the ``uv`` copy of vergil-tooling in
   ``~/.local/bin`` that drives the test;
7. purge/remove the package;
8. check that nothing remains under ``/opt/<vendor>/<name>`` or in the shims;
9. write the JSON report.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib.config import read_config
from vergil_tooling.lib.package import PackageError, matrix, naming, orgs, repo_setup
from vergil_tooling.lib.package import targets as tg
from vergil_tooling.lib.package.repo_setup import local_run

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.package.matrix import TestCell
    from vergil_tooling.lib.package.repo_setup import Run

CLEAN_ENV = "env -i HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
"""Prefix for the smoke and shim checks: an empty environment with a system-only ``PATH``."""

# The org whose repository serves the runtime that python-builder products depend on.
RUNTIME_VENDOR = "vergil"

_UNIT = re.compile(r"/systemd/system/[^/]+\.(?:service|timer|socket)$")
_SHIM = re.compile(r"^/usr/bin/([^/]+)$")


def select_artifact(artifacts: Path, name: str, cell: TestCell, noarch: bool) -> Path:
    """Return the one artifact in ``artifacts`` that ``cell`` installs; else raise.

    File names follow nFPM's conventions for the revision ``build`` assigns:
    deb ``<name>_<version>-<rev>_<arch>.deb`` and rpm
    ``<name>-<version>-<rev>.<rpm-arch>.rpm``, with ``<rev>`` ``1`` for shared
    builds and ``1~<suite>`` (deb) / ``1.<suite>`` (rpm) for native ones. A
    ``noarch`` package is ``all`` (deb) / ``noarch`` (rpm).
    """
    if cell.fmt == "deb":
        arch = "all" if noarch else cell.arch
        rev = f"1~{cell.suite}" if cell.native else "1"
        pattern = f"{name}_*-{rev}_{arch}.deb"
    else:
        arch = "noarch" if noarch else tg.REGISTRY[cell.target].rpm_arch
        rev = f"1.{cell.suite}" if cell.native else "1"
        pattern = f"{name}-*-{rev}.{arch}.rpm"
    hits = sorted(artifacts.glob(pattern))
    if len(hits) != 1:
        found = ", ".join(p.name for p in hits) or "none"
        msg = (
            f"expected exactly one artifact for {name} on {cell.target}, found {len(hits)} "
            f"matching {pattern} in {artifacts}: {found}"
        )
        raise PackageError(msg)
    return hits[0]


def _exists(run: Run, path: str) -> bool:
    """``test -e path`` on the target. A nonzero exit (raised or returned) means absent."""
    try:
        return run("test", "-e", path).returncode == 0
    except subprocess.CalledProcessError:
        return False


def _detail(exc: subprocess.CalledProcessError) -> str:
    return f"(exit {exc.returncode})\n{(exc.stderr or exc.stdout or '').strip()}".rstrip()


def run_install_test(
    repo_root: Path,
    cell_id: str,
    artifacts: Path,
    report: Path,
    run: Run = local_run,
) -> None:
    """Install-test test cell ``cell_id`` from ``artifacts``; write the JSON report to ``report``.

    Any failed step is fatal: ``run`` raising ``CalledProcessError`` propagates,
    a failed smoke, a misresolved shim or any residue raises ``PackageError``.
    """
    cfg = read_config(repo_root)
    if cfg.package is None:
        msg = "vergil.toml has no [package] section"
        raise PackageError(msg)
    pkg = cfg.package
    cells = {c.id: c for c in matrix.resolve(pkg).test}
    if cell_id not in cells:
        msg = f"unknown test cell {cell_id!r} (known: {', '.join(cells)})"
        raise PackageError(msg)
    cell = cells[cell_id]
    name = naming.package_name(repo_root, pkg)
    artifact = select_artifact(artifacts, name, cell, noarch=pkg.noarch).resolve()
    deb = cell.fmt == "deb"
    installer = "apt-get" if deb else "dnf"

    # 1. Prerequisites.
    if deb:
        run("apt-get", "update")
    # 2. The runtime a python-builder product depends on comes from the live repository.
    if pkg.builder == "python":
        repo_setup.bootstrap(run, orgs.for_vendor(RUNTIME_VENDOR), cell.fmt, cell.suite, sudo=False)
    # 3. Install the local artifact.
    run(installer, "install", "-y", str(artifact))
    # 4. List what it installed.
    listed = run(*(("dpkg", "-L") if deb else ("rpm", "-ql")), name).stdout
    paths = [line.strip() for line in listed.splitlines() if line.strip()]
    # 5. Shipped systemd units are installed and pass systemd-analyze verify.
    units = [p for p in paths if _UNIT.search(p)]
    if units:
        run(installer, "install", "-y", "systemd")
        for unit in units:
            run("systemd-analyze", "verify", unit)
    # 6. Smoke and shims, in the sanitized environment only.
    clean = shlex.split(CLEAN_ENV)
    try:
        run(*clean, "bash", "-c", pkg.smoke)
    except subprocess.CalledProcessError as exc:
        msg = f"smoke command failed on {cell.target}: {pkg.smoke} {_detail(exc)}"
        raise PackageError(msg) from exc
    shims = [p for p in paths if _SHIM.match(p)]
    for shim in shims:
        cmd = shim.removeprefix("/usr/bin/")
        try:
            got = run(*clean, "bash", "-c", f"command -v {cmd}").stdout.strip()
        except subprocess.CalledProcessError as exc:
            msg = f"shim {cmd} does not resolve on {cell.target} {_detail(exc)}"
            raise PackageError(msg) from exc
        if got != shim:
            msg = f"shim {cmd} resolves to {got}, expected {shim}"
            raise PackageError(msg)
    # 7. Remove.
    if deb:
        run("apt-get", "purge", "-y", name)
    else:
        run("dnf", "remove", "-y", name)
    # 8. Nothing may remain.
    residue = [p for p in (f"/opt/{pkg.vendor}/{name}", *shims) if _exists(run, p)]
    if residue:
        msg = f"{name} on {cell.target} left behind after removal: {', '.join(residue)}"
        raise PackageError(msg)
    # 9. Report.
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cell": cell.id,
        "target": cell.target,
        "artifact": artifact.name,
        "units": units,
        "smoke": "pass",
        "residue": residue,
    }
    report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
