"""glibc floor guard (spec §6.1 step 4).

A shared build cell builds on Ubuntu 24.04 but may emit an ``.rpm`` for RHEL 9,
whose glibc is older. Every ELF object in the staged tree is scanned for the
``GLIBC_x.y`` symbol versions it needs from glibc's own libraries; anything
newer than the lowest glibc among the cell's targets is a hard error, so such a
package can never ship.
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

# The sonames glibc itself ships, on the architectures the fleet builds for
# (amd64 and arm64). A version need counts only when its library (the verneed
# entry's ``vn_file``) is in this set: other libraries may name their own
# versions ``GLIBC_x.y`` (aarch64's libgcc_s.so.1 has ``GLIBC_2.0`` for
# ``__register_frame``), and those say nothing about the glibc a file needs.
# The set is explicit, not a pattern, so an unknown library is never counted.
# ``libcrypt.so.1`` is left out: on current distributions it is libxcrypt, not glibc.
GLIBC_SONAMES: frozenset[str] = frozenset(
    {
        # The dynamic loaders.
        "ld-linux-x86-64.so.2",
        "ld-linux-aarch64.so.1",
        # The C library and the libraries split from it.
        "libc.so.6",
        "libm.so.6",
        "libmvec.so.1",
        "libpthread.so.0",
        "libdl.so.2",
        "librt.so.1",
        "libresolv.so.2",
        "libutil.so.1",
        "libanl.so.1",
        "libnsl.so.1",
        "libBrokenLocale.so.1",
        "libthread_db.so.1",
        "libc_malloc_debug.so.0",
    }
)


def glibc_requirements(path: Path) -> set[tuple[int, int]]:
    """Return every ``(major, minor)`` glibc version ``path`` needs; empty for a non-ELF file.

    Only version needs on a library in ``GLIBC_SONAMES`` are counted.
    """
    with path.open("rb") as fh:
        if fh.read(len(_ELF_MAGIC)) != _ELF_MAGIC:
            return set()
        fh.seek(0)
        out: set[tuple[int, int]] = set()
        try:
            for section in ELFFile(fh).iter_sections():
                if not isinstance(section, GNUVerNeedSection):
                    continue
                for verneed, auxiter in section.iter_versions():
                    # pyelftools sets ``name`` on a verneed entry to its ``vn_file``.
                    if verneed.name not in GLIBC_SONAMES:
                        continue
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
