"""Project host agent memory into a cloud guest as a read-only cache.

Net-new logic for the cloud-memory-projection epic (vergil-project/.github#156,
spec Component 3). The physical host is the single source of truth for agent
memory; a cloud VM is a projection of it. Before a cloud session opens, the host
copies that repo's memory subset — the per-repo ``memory/`` directory (including
``MEMORY.md``) — plus the global ``~/.claude/CLAUDE.md`` into the guest at the
slug Claude derives from the host project path, file-by-file over the
established ``transport.pipe`` (``cat > <dest>``) idiom (the same mechanism
``copy_claude_config`` uses; there is no ``rsync`` over the transport).

The read-only lock that freezes this projection is applied separately by
``lock_projection`` (issue #2412); this module only *copies* the files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.vm_transport import Transport

# The guest ``~/.claude`` root. ``~`` is deliberately left unquoted so the guest
# shell expands it, matching the ``copy_claude_config`` idiom (``cat > ~/.claude/…``).
_GUEST_CLAUDE_DIR = "~/.claude"


def host_slug(host_workdir: str) -> str:
    """Return Claude's memory slug for an absolute path.

    Claude derives ``~/.claude/projects/<slug>`` from the session's starting
    working directory by replacing every ``/`` with ``-`` (so an absolute path
    keeps its leading ``-``). Matching that convention is what lets the projected
    memory be *found* under the guest's ``~/.claude/projects/<host-slug>/`` once
    the session starts at the host-equivalent path (spec Component 2a).
    """
    return host_workdir.replace("/", "-")


def host_memory_dir(claude_dir: Path, slug: str) -> Path:
    """Return the host memory dir for a slug: ``<claude_dir>/projects/<slug>/memory``."""
    return claude_dir / "projects" / slug / "memory"


def project_memory(transport: Transport, *, claude_dir: Path, host_workdir: str) -> None:
    """Copy the host memory subset and global ``CLAUDE.md`` into the cloud guest.

    Resolves the host memory slug from ``host_workdir``, ensures the guest memory
    directory exists, then copies — file-by-file over ``transport.pipe`` — every
    file under the host ``memory/`` directory (including ``MEMORY.md``) to the
    matching guest path under ``~/.claude/projects/<slug>/memory/`` and the global
    ``~/.claude/CLAUDE.md``. Each pipe clears any prior read-only lock
    (``chmod u+w`` before ``cat >``) so a re-projection can overwrite a locked
    cache. If the host has no memory directory for this repo yet, the guest memory
    directory is still created and the memory copy is skipped (not an error — a
    repo may have no memory yet); the global ``CLAUDE.md`` is projected regardless.

    The read-only lock is **not** applied here — ``lock_projection`` (issue #2412)
    owns that and will be inserted at the end of this function once it lands.
    """
    slug = host_slug(host_workdir)
    guest_memory_dir = f"{_GUEST_CLAUDE_DIR}/projects/{slug}/memory"

    src_memory_dir = host_memory_dir(claude_dir, slug)
    src_files = (
        sorted(p for p in src_memory_dir.rglob("*") if p.is_file())
        if src_memory_dir.is_dir()
        else []
    )

    # Create the guest memory dir (and any nested subdirs the source carries) up
    # front, over the ``bash -c`` idiom so the guest shell expands ``~``.
    guest_dirs = {guest_memory_dir}
    copies: list[tuple[str, Path]] = []
    for src in src_files:
        rel = src.relative_to(src_memory_dir).as_posix()
        dest = f"{guest_memory_dir}/{rel}"
        guest_dirs.add(dest.rsplit("/", 1)[0])
        copies.append((dest, src))
    transport.run("bash", "-c", f"mkdir -p {' '.join(sorted(guest_dirs))}")

    for dest, src in copies:
        transport.pipe(f"chmod u+w {dest} 2>/dev/null; cat > {dest}", src.read_text())

    global_claude_md = claude_dir / "CLAUDE.md"
    if global_claude_md.exists():
        dest = f"{_GUEST_CLAUDE_DIR}/CLAUDE.md"
        transport.pipe(f"chmod u+w {dest} 2>/dev/null; cat > {dest}", global_claude_md.read_text())
