"""The host-local ``--plugin-dir`` override for ``vrg-vm session``.

Epic vergil-project/.github#699 (spec 4.7): the fleet can run an unreleased
plugin tree (for example a paad checkout's ``preview/paad``) instead of the
installed plugin by listing it in a host config file. ``vrg-vm session`` reads
the file on the host and adds ``--plugin-dir <path>`` for each entry to the
``claude`` command it launches on a Lima VM. The file is personal, lives
outside every repo, is created by hand, and the override is off when it is
absent.

File format — one key, a list of absolute paths::

    plugin_dirs = ["/Users/me/dev/projects/me/paad-fleet/preview/paad"]

Invariants:

- **Absolute paths only.** A Lima VM sees the Mac's projects tree at the same
  path, so an absolute host path is also the guest path; a relative one has no
  defined meaning in the guest and is refused.
- **No silent failure.** A missing file means "no override". Invalid TOML, an
  unknown key, a non-list value, a non-string entry or a relative entry is a
  loud :class:`PluginDirsError`; the session never starts without a tree the
  file asked for.
"""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePosixPath

_KEY = "plugin_dirs"


class PluginDirsError(ValueError):
    """The plugin-dirs override file is malformed."""


def default_plugin_dirs_path() -> Path:
    """Return the host-local override file, ``~/.config/vergil/plugin-dirs.toml``."""
    return Path.home() / ".config" / "vergil" / "plugin-dirs.toml"


def load_plugin_dirs(path: Path | None = None) -> tuple[str, ...]:
    """Read and validate the override file; a missing file means no override.

    Raises:
        PluginDirsError: the file is not valid TOML, carries a key other than
            ``plugin_dirs``, its value is not a list of strings, or an entry is
            not an absolute path.
    """
    path = path or default_plugin_dirs_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PluginDirsError(f"{path}: not valid TOML: {exc}") from exc
    unknown = sorted(set(data) - {_KEY})
    if unknown:
        raise PluginDirsError(f"{path}: unknown key(s) {', '.join(unknown)} (only {_KEY})")
    raw = data.get(_KEY, [])
    if not isinstance(raw, list):
        raise PluginDirsError(f"{path}: {_KEY} must be a list of absolute paths")
    entries: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise PluginDirsError(f"{path}: {_KEY} entry {entry!r} is not a string")
        if not PurePosixPath(entry).is_absolute():
            raise PluginDirsError(f"{path}: {_KEY} entry {entry!r} is not an absolute path")
        entries.append(entry)
    return tuple(entries)
