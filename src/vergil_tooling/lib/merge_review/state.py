"""merge-review's private directory, checked level by level before any write.

Every file of a run lives in
``build/.vergil-state/issue-implement/<N>/merge-review/`` in the task
repository's main clone (``docs/specs/merge-review-protocol.md``, "Files").
Its privacy rests on the ``build/.vergil-state`` root being mode 700: the
human and the agents share one Unix user, so a private root makes everything
below it private. Each level from ``.vergil-state`` down to ``merge-review``
must also be a real directory, not a symlink, and mode 700, so no link can
redirect a write out of the private root. :func:`ensure_private_dir` makes
these checks, creating any absent level mode 700, and the caller runs it
before every write it makes there.
"""

from __future__ import annotations

import contextlib
import stat
from typing import TYPE_CHECKING

from vergil_tooling.lib.build_dir import ensure_build_dir

if TYPE_CHECKING:
    from pathlib import Path

#: The levels below ``build``, the issue's number taking the place of ``{issue}``.
LEVELS = (".vergil-state", "issue-implement", "{issue}", "merge-review")

#: The only permission bits a level may have.
PRIVATE_MODE = 0o700


class StateDirError(Exception):
    """A level of the private directory is not a private, real directory."""


def ensure_private_dir(main_clone: Path, issue: int) -> Path:
    """Check (creating where absent) every level of *issue*'s private directory.

    Calls :func:`ensure_build_dir` first, then takes each level below
    ``build`` in turn. An existing level must be a real directory, not a
    symlink, with permission bits exactly 700; an absent one is created mode
    700. Raises :class:`StateDirError` naming the level otherwise, before
    anything is created below it. Returns the ``merge-review`` directory.
    """
    if issue < 1:
        msg = f"issue number must be positive, not {issue}"
        raise StateDirError(msg)
    path = ensure_build_dir(main_clone)
    for level in LEVELS:
        path /= level.format(issue=issue)
        _ensure_level(path)
    return path


def _ensure_level(path: Path) -> None:
    created = False
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=PRIVATE_MODE)
        created = True
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        msg = f"{path} is a symlink, not a directory"
        raise StateDirError(msg)
    if not stat.S_ISDIR(info.st_mode):
        msg = f"{path} is not a directory"
        raise StateDirError(msg)
    if created and stat.S_IMODE(info.st_mode) != PRIVATE_MODE:
        # The umask narrowed the mode mkdir was given; set exactly 700.
        path.chmod(PRIVATE_MODE)
        info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if mode != PRIVATE_MODE:
        msg = f"{path} has mode {mode:o}, not {PRIVATE_MODE:o}: it must be private"
        raise StateDirError(msg)
