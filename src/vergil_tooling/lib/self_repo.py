"""Whether a checkout is vergil-tooling itself (the "self repo").

vergil-tooling runs its own unreleased code: its dev container skips
installing the released tooling, and its release audits its managed files
against the master templates in its own source tree rather than the installed
release's (issue #4414). Both decide "this is vergil-tooling" with the test
the shared setup action uses (vergil-actions ``install-tooling.sh``): the
checkout's ``pyproject.toml`` names the ``vergil-tooling`` project.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: The ``[project].name`` in vergil-tooling's ``pyproject.toml``.
SELF_PROJECT_NAME = "vergil-tooling"


def is_self_repo(repo_root: Path) -> bool:
    """Return True when *repo_root* is the vergil-tooling project itself.

    A missing or unparsable ``pyproject.toml``, or one with no
    ``[project].name``, is not the self repo.
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
    except OSError, tomllib.TOMLDecodeError:
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == SELF_PROJECT_NAME
