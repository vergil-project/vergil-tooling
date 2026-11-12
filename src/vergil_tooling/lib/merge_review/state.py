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

The task's own level, ``issue-implement/<N>/``, also holds the batch marker
(epic vergil-project/.github#1019): :func:`ensure_issue_dir` makes the same
checks down to ``<N>`` only, and :func:`existing_issue_dir` makes them
without creating anything, for a reader or a remover.
"""

from __future__ import annotations

import contextlib
import stat
from typing import TYPE_CHECKING

from vergil_tooling.lib.build_dir import BUILD_DIR, ensure_build_dir

if TYPE_CHECKING:
    from pathlib import Path

#: The levels below ``build``, the issue's number taking the place of ``{issue}``.
LEVELS = (".vergil-state", "issue-implement", "{issue}", "merge-review")

#: The levels down to the issue's own directory, ``issue-implement/<N>``.
ISSUE_LEVELS = LEVELS[:-1]

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
    path = ensure_issue_dir(main_clone, issue) / LEVELS[-1]
    _ensure_level(path)
    return path


def ensure_issue_dir(main_clone: Path, issue: int) -> Path:
    """Check (creating where absent) every level down to ``issue-implement/<N>``.

    The checks of :func:`ensure_private_dir`, stopping at the issue's own
    directory, which it returns.
    """
    _check_issue(issue)
    path = ensure_build_dir(main_clone)
    for level in ISSUE_LEVELS:
        path /= level.format(issue=issue)
        _ensure_level(path)
    return path


def existing_issue_dir(main_clone: Path, issue: int) -> Path | None:
    """Check the levels down to ``issue-implement/<N>`` without creating any.

    Returns the issue's directory, or ``None`` when ``build`` or any level
    below it is absent. A level that exists must pass the checks of
    :func:`ensure_private_dir` (``build`` must be a real directory), or
    :class:`StateDirError` names it.
    """
    _check_issue(issue)
    path = main_clone / BUILD_DIR
    if not _exists(path):
        return None
    if path.is_symlink() or not path.is_dir():
        msg = f"{path} is not a real directory"
        raise StateDirError(msg)
    for level in ISSUE_LEVELS:
        path /= level.format(issue=issue)
        if not _exists(path):
            return None
        _check_level(path)
    return path


def _check_issue(issue: int) -> None:
    if issue < 1:
        msg = f"issue number must be positive, not {issue}"
        raise StateDirError(msg)


def _exists(path: Path) -> bool:
    """True when anything, a dangling symlink included, holds *path*'s name."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _ensure_level(path: Path) -> None:
    created = False
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=PRIVATE_MODE)
        created = True
    info = path.lstat()
    if created and stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) != PRIVATE_MODE:
        # The umask narrowed the mode mkdir was given; set exactly 700.
        path.chmod(PRIVATE_MODE)
    _check_level(path)


def _check_level(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        msg = f"{path} is a symlink, not a directory"
        raise StateDirError(msg)
    if not stat.S_ISDIR(info.st_mode):
        msg = f"{path} is not a directory"
        raise StateDirError(msg)
    mode = stat.S_IMODE(info.st_mode)
    if mode != PRIVATE_MODE:
        msg = f"{path} has mode {mode:o}, not {PRIVATE_MODE:o}: it must be private"
        raise StateDirError(msg)
