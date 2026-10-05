"""glibc floor guard (spec §6.1 step 4).

A shared build cell builds on Ubuntu 24.04 but may emit an ``.rpm`` for RHEL 9,
whose glibc is older. Every ELF object in the staged tree is scanned for the
``GLIBC_x.y`` symbol versions it needs; anything newer than the lowest glibc
among the cell's targets is a hard error, so such a package can never ship.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.gnuversions import GNUVerNeedSection

from vergil_tooling.lib.package import PackageError

_ELF_MAGIC = b"\x7fELF"
_GLIBC = re.compile(r"GLIBC_(\d+)\.(\d+)(?:\.\d+)?")


def glibc_requirements(path: Path) -> set[tuple[int, int]]:
    """Return every ``(major, minor)`` glibc version ``path`` needs; empty for a non-ELF file."""
    with path.open("rb") as fh:
        if fh.read(len(_ELF_MAGIC)) != _ELF_MAGIC:
            return set()
        fh.seek(0)
        out: set[tuple[int, int]] = set()
        try:
            for section in ELFFile(fh).iter_sections():
                if not isinstance(section, GNUVerNeedSection):
                    continue
                for _verneed, auxiter in section.iter_versions():
                    for aux in auxiter:
                        m = _GLIBC.fullmatch(aux.name)
                        if m:
                            out.add((int(m[1]), int(m[2])))
        except ELFError as exc:
            msg = f"{path}: unreadable ELF ({exc})"
            raise PackageError(msg) from exc
        return out


def _regular_files(root: Path) -> list[Path]:
    """Every regular file under ``root``, sorted; symlinks are neither followed nor listed."""
    found: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if stat.S_ISREG(path.lstat().st_mode):
                found.append(path)
    return sorted(found)


def check_glibc_floor(root: Path, floor: tuple[int, int]) -> None:
    """Raise ``PackageError`` listing every file under ``root`` needing a glibc above ``floor``."""
    bad: list[str] = []
    for path in _regular_files(root):
        reqs = glibc_requirements(path)
        if reqs and max(reqs) > floor:
            need = max(reqs)
            bad.append(f"  {path.relative_to(root)} needs {need[0]}.{need[1]}")
    if bad:
        msg = (
            f"GLIBC floor {floor[0]}.{floor[1]} exceeded (a dependency was compiled against "
            "a newer glibc; use a manylinux wheel or a [package].native build):\n" + "\n".join(bad)
        )
        raise PackageError(msg)
