"""Shell commands that replace a file atomically from stdin.

Every file the tooling writes through a shell (a guest file over a
:class:`~vergil_tooling.lib.vm_transport.Transport`, an apt pin or a package
source) is written with :func:`atomic_write_cmd`, never with a bare
``cat > <file>`` (#3987, #4035). A ``cat >`` truncates the file first, so a
reader can see it empty or partial, and a write that fails part-way leaves it
truncated. Here the content goes to a temp file in the target's own directory,
gets its final mode there, and is renamed into place: a reader sees the old
file or the new one, and a failed write leaves the old one untouched.

This module is an import-graph leaf: it uses only the standard library, so the
guest helpers and the package layer can both use it.
"""

from __future__ import annotations

import shlex


def _shell_word(path: str) -> str:
    """*path* as one shell word, with a leading ``~/`` left for the shell to expand."""
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def atomic_write_cmd(path: str, mode: int) -> str:
    """Return the shell command that atomically replaces *path* with its stdin.

    *path* is absolute or starts with ``~/`` (expanded by the shell that runs the
    command). Stdin goes to a temp file made by ``mktemp`` in the directory of
    *path*, so the final ``mv -f`` is a rename on one filesystem. The temp file
    gets *mode* (such as ``0o600``) before the rename, so the file never exists
    at its final path with any other mode. If any step fails, the temp file is
    removed and the command exits non-zero, so the caller's transport raises and
    the existing file is left as it was, mode included. The rename needs only the
    directory to be writable, so a read-only file is replaced without reopening it.

    The directory must already exist.

    Raises:
        ValueError: *path* is neither absolute nor ``~/``-relative, names no file,
            or *mode* is not a permission mode.
    """
    if not path.startswith(("/", "~/")):
        msg = f"atomic write path {path!r} must be absolute or start with '~/'"
        raise ValueError(msg)
    directory, _, name = path.rpartition("/")
    if not name:
        msg = f"atomic write path {path!r} names no file"
        raise ValueError(msg)
    if not 0 <= mode <= 0o777:
        msg = f"atomic write mode {mode:#o} is not a permission mode"
        raise ValueError(msg)
    template = _shell_word(f"{directory}/.{name}.XXXXXX")
    dest = _shell_word(path)
    steps = f'cat > "$tmp" && chmod {mode:o} "$tmp" && mv -f "$tmp" {dest}'
    return f'tmp=$(mktemp {template}) && {{ {steps} || {{ rm -f "$tmp"; false; }}; }}'
