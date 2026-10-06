"""Builder interface and build orchestration (spec §6).

A builder turns a repo into a staged filesystem tree and returns the nFPM
``contents`` entries plus per-format dependencies (:class:`BuildResult`).
:func:`run_build` drives one build cell end to end: run the builder, apply the
glibc floor guard, load and check the overlay, and package every format the
cell emits. Builders register themselves via :func:`register`; importing their
modules (``bin/vrg_package.py`` does) populates :data:`BUILDERS`.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.config import PackageConfig, read_config
from vergil_tooling.lib.package import PackageError, elf, matrix, naming, nfpm
from vergil_tooling.lib.package import targets as tg

if TYPE_CHECKING:
    from vergil_tooling.lib.package.matrix import BuildCell


@dataclass(frozen=True)
class BuildContext:
    repo_root: Path
    pkg: PackageConfig
    cell: BuildCell
    name: str
    version: str
    staging_root: Path
    out_dir: Path


def _empty_depends() -> dict[str, list[str]]:
    return {"deb": [], "rpm": []}


@dataclass
class BuildResult:
    contents: list[dict[str, Any]]
    # Keyed by package format ("deb"/"rpm").
    depends: dict[str, list[str]] = field(default_factory=_empty_depends)


Builder = Callable[[BuildContext], BuildResult]
BUILDERS: dict[str, Builder] = {}


def register(kind: str) -> Callable[[Builder], Builder]:
    """Register the decorated function as the builder for ``[package].builder = kind``."""

    def deco(fn: Builder) -> Builder:
        BUILDERS[kind] = fn
        return fn

    return deco


def contents_from_tree(root: Path, own_below: str) -> list[dict[str, Any]]:
    """Return nFPM ``contents`` entries for the staged tree at ``root`` (``root`` stands for ``/``).

    Every regular file and symlink is an entry (symlinks keep their target and
    are never descended into). Every directory **strictly below** ``own_below``
    is owned (a ``type: dir`` entry); ``own_below`` itself and its ancestors —
    shared directories such as ``/opt/vergil`` — never are (spec §6.3).
    Anything else (a FIFO, socket or device node) is a hard error.
    """
    prefix = "/" + own_below.strip("/")
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        here = Path(dirpath)
        rel = here.relative_to(root).as_posix()
        dst_dir = "/" if rel == "." else "/" + rel
        if dst_dir.startswith(prefix + "/"):
            out.append({"dst": dst_dir, "type": "dir"})
        # os.walk lists a symlink-to-directory in dirnames but does not descend
        # into it; it is packaged as a symlink like any other.
        entries = filenames + [d for d in dirnames if (here / d).is_symlink()]
        for name in sorted(entries):
            path = here / name
            dst = dst_dir.rstrip("/") + "/" + name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                out.append({"src": str(path.readlink()), "dst": dst, "type": "symlink"})
            elif stat.S_ISREG(mode):
                out.append(
                    {"src": str(path), "dst": dst, "file_info": {"mode": stat.S_IMODE(mode)}}
                )
            else:
                msg = f"staged tree has a non-regular file at {dst} ({path}); cannot package it"
                raise PackageError(msg)
    return out


def package_release(cell: BuildCell, fmt: str) -> str:
    """The package revision: ``1`` for shared builds, ``1~<suite>``/``1.<suite>`` for native."""
    if not cell.native:
        return "1"
    return f"1~{cell.suite}" if fmt == "deb" else f"1.{cell.suite}"


def glibc_floor(cell: BuildCell) -> tuple[int, int]:
    """The lowest glibc among the cell's targets: no shipped ELF may need newer."""
    return min(tg.REGISTRY[k].glibc for k in cell.targets)


def run_build(
    repo_root: Path, cell_id: str, version: str, out_dir: Path, staging_root: Path
) -> list[Path]:
    """Build every package format of build cell ``cell_id``; return the artifact paths.

    ``version`` is the repo version; ``[package].version`` overrides it when set.
    ``staging_root`` is wiped and recreated; artifacts land in ``out_dir``.
    """
    cfg = read_config(repo_root)
    if cfg.package is None:
        msg = "vergil.toml has no [package] section"
        raise PackageError(msg)
    pkg = cfg.package
    cells = {c.id: c for c in matrix.resolve(pkg).build}
    if cell_id not in cells:
        msg = f"unknown build cell {cell_id!r} (known: {', '.join(cells)})"
        raise PackageError(msg)
    cell = cells[cell_id]
    builder = BUILDERS.get(pkg.builder)
    if builder is None:
        msg = f"no builder registered for [package].builder = {pkg.builder!r}"
        raise PackageError(msg)
    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = BuildContext(
        repo_root=repo_root,
        pkg=pkg,
        cell=cell,
        name=naming.package_name(repo_root, pkg),
        version=pkg.version or version,
        staging_root=staging_root,
        out_dir=out_dir,
    )
    result = builder(ctx)
    elf.check_glibc_floor(staging_root, glibc_floor(cell))
    overlay = nfpm.load_overlay(repo_root)
    nfpm.check_maintainer_scripts(repo_root, overlay)
    if not result.contents and not overlay.get("contents"):
        msg = f"build produced an empty package for {ctx.name} (cell {cell_id})"
        raise PackageError(msg)
    return [nfpm.package(nfpm.render(ctx, result, fmt, overlay), fmt, out_dir) for fmt in cell.fmts]
