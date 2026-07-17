"""Project host agent memory into a cloud guest as a read-only cache.

Net-new logic for the cloud-memory-projection epic (vergil-project/.github#156,
spec Component 3). The physical host is the single source of truth for agent
memory; a cloud VM is a projection of it. Before a cloud session opens, the host
copies that repo's memory subset — the per-repo ``memory/`` directory (including
``MEMORY.md``) — plus the global ``~/.claude/CLAUDE.md`` into the guest at the
slug Claude derives from the host project path, file-by-file over the
established ``transport.pipe`` (``cat > <dest>``) idiom (the same mechanism
``copy_claude_config`` uses; there is no ``rsync`` over the transport).

Once the copy completes, the projection is frozen read-only by
``lock_projection`` (issue #2412), which ``project_memory`` calls at the end so a
futile cloud write fails loudly with ``EACCES`` instead of succeeding-then-
vanishing (spec §Component 3, §Error handling).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
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

    After the copy, ``lock_projection`` freezes the projected canonical set
    read-only. Re-projection is safe: each pipe above clears the lock
    (``chmod u+w``) before overwriting, and the lock is re-applied here.
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

    # Freeze the projection read-only. ``$HOME`` (not ``~``) is used because the
    # lock paths are double-quoted to survive the slug's characters, and ``~``
    # does not expand inside double quotes whereas ``$HOME`` does. The only extra
    # beyond the memory subtree is the global ``CLAUDE.md`` (the Task-1 audit's
    # locked set: vergil-project/.github#161); ``settings.json`` is deliberately
    # NOT locked — provisioning and the Claude runtime both write it.
    lock_projection(
        transport,
        claude_dir_guest="$HOME/.claude",
        slug=slug,
        locked_set=["$HOME/.claude/CLAUDE.md"],
    )


def lock_projection(
    transport: Transport,
    *,
    claude_dir_guest: str,
    slug: str,
    locked_set: Sequence[str],
) -> None:
    """Apply a surgical read-only lock to the projected canonical memory set.

    ``chmod`` read-only exactly the projected canonical data so a futile cloud
    write fails loudly with ``EACCES`` rather than succeeding-then-vanishing
    (spec §Component 3): the per-repo ``memory`` subtree
    (``<claude_dir_guest>/projects/<slug>/memory``, recursively), its
    ``MEMORY.md``, and every path in ``locked_set`` (the Task-1 audit's extras,
    i.e. the global ``CLAUDE.md``).

    **Surgical.** The ``projects/<slug>/`` directory co-mingles the durable
    ``memory/`` subtree with the session-transcript ``.jsonl`` files Claude writes
    continuously, so this **never** blanket-``chmod``s ``projects/<slug>/`` itself
    — a recursive lock there would break session logging with ``EACCES``. Only
    ``memory/`` and the enumerated files are frozen; the transcripts stay
    writable. ``settings.json`` is likewise never in the locked set (provisioning
    and the runtime both write it).

    Each ``chmod`` is guarded by an ``[ -e ]`` existence test so a missing
    optional file is a no-op, not an error, and the ``if … then … fi`` form always
    exits 0 (re-locking after a re-projection is safe — ``project_memory``
    ``chmod u+w``s before overwriting).
    """
    memory_dir = f'"{claude_dir_guest}/projects/{slug}/memory"'
    memory_md = f'"{claude_dir_guest}/projects/{slug}/memory/MEMORY.md"'

    targets: list[tuple[str, str]] = [
        (memory_dir, f"chmod -R a-w {memory_dir}"),
        (memory_md, f"chmod a-w {memory_md}"),
    ]
    for path in locked_set:
        quoted = f'"{path}"'
        targets.append((quoted, f"chmod a-w {quoted}"))

    script = "\n".join(f"if [ -e {target} ]; then {chmod}; fi" for target, chmod in targets)
    transport.run("bash", "-c", script)
