"""The change classifier: is a branch's change ``docs-only`` or ``code``?

The caller's tools decide the class before dispatch, from the changed files,
never an agent (``docs/specs/merge-review-protocol.md``, "The request"). A
change is ``docs-only`` only when every changed file is an addition or an
edit of a path on the documentation allow-list; it is ``code`` otherwise:
any deletion, rename, copy or type change, any change to ``vergil.toml``
(where a repository's own allow-list lives, so a branch cannot widen its own
list), an empty change, or any doubt.

The allow-list is a list of base paths. An entry ending in ``/`` matches
every path below it; any other entry matches that one file exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.config import CONFIG_FILE
from vergil_tooling.lib.merge_review.schema import CHANGE_CLASSES

if TYPE_CHECKING:
    from collections.abc import Sequence

DOCS_ONLY, CODE = CHANGE_CLASSES

#: The paths every repository's allow-list starts with: files known to hold
#: nothing but documentation. A repository adds its own through the
#: ``[merge-review]`` ``docs-paths`` key of ``vergil.toml``.
BUILTIN_DOCS_PATHS: tuple[str, ...] = ("README.md", "CHANGELOG.md")

#: The only ``git diff --name-status`` statuses that can be docs-only: an
#: addition or an edit. Every other status (deletion, rename, copy, type
#: change, unmerged, unknown) makes the change code.
_DOCS_STATUSES = frozenset({"A", "M"})


def allow_list(repo_docs_paths: Sequence[str]) -> tuple[str, ...]:
    """The full allow-list: :data:`BUILTIN_DOCS_PATHS`, then the repo's own paths."""
    return (*BUILTIN_DOCS_PATHS, *repo_docs_paths)


def is_docs_path(path: str, docs_paths: Sequence[str]) -> bool:
    """Whether *path* is on the allow-list *docs_paths*.

    An entry ending in ``/`` matches every path below it; any other entry
    matches that path exactly, so ``docs`` never matches ``docs-extra/x.md``.
    """
    for entry in docs_paths:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def classify(changes: list[tuple[str, str, str | None]], docs_paths: Sequence[str]) -> str:
    """Return ``"docs-only"`` or ``"code"`` for *changes*.

    *changes* holds ``(status, path, new path for renames)`` tuples, as
    :func:`vergil_tooling.lib.git.diff_name_status` returns them. The change
    is ``docs-only`` only when there is at least one change, every change is
    an addition or an edit (status ``A`` or ``M``) of a path on *docs_paths*,
    and none touches ``vergil.toml``, whatever *docs_paths* says.
    """
    if not changes:
        return CODE
    for status, path, new_path in changes:
        if status not in _DOCS_STATUSES or new_path is not None:
            return CODE
        if path == CONFIG_FILE or not is_docs_path(path, docs_paths):
            return CODE
    return DOCS_ONLY
