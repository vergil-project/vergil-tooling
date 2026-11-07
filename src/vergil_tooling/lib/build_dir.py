"""The one way any code reaches a repository's ``build/`` directory.

``build/`` is ignored by git, so a fresh clone has none. :func:`ensure_build_dir`
creates it when absent and refuses one that is a symlink or not a directory,
so nothing written below it can be redirected out of the repository.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: The ignored scratch directory at a repository's root.
BUILD_DIR = "build"


class BuildDirError(Exception):
    """A repository's ``build`` is a symlink or not a directory."""


def ensure_build_dir(repo_root: Path) -> Path:
    """Return *repo_root*/``build``, creating it with a plain mkdir when absent.

    It is ordinary scratch, so it has no mode rule. Raises
    :class:`BuildDirError` naming the path when it is a symlink or anything
    other than a directory.
    """
    build = repo_root / BUILD_DIR
    # Whatever already holds the name (a directory, a file, a symlink even a
    # dangling one) makes mkdir fail; the checks below then judge it.
    with contextlib.suppress(FileExistsError):
        build.mkdir()
    if build.is_symlink():
        msg = f"{build} is a symlink, not a directory"
        raise BuildDirError(msg)
    if not build.is_dir():
        msg = f"{build} is not a directory"
        raise BuildDirError(msg)
    return build
