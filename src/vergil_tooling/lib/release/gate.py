"""The declared-identity gate: refuse to release a repo that is not released.

A repo opts in to releasing with ``[publish] release = true`` in
``vergil.toml``; the key defaults to false when absent. ``vrg-release`` and
every command that cascades into it (``vrg-finalize-pr --release/--install``,
``vrg-submit-pr --release/--install``) check this before any side effect, so a
repo that is not released is refused up front with nothing changed, rather
than discovering it partway through a release, after the release PR has
merged to ``main`` (issue #4175).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git

if TYPE_CHECKING:
    from pathlib import Path


def unreleased_refusal(repo_root: Path | None = None) -> str | None:
    """Return the refusal for a repo that does not declare itself released.

    *repo_root* defaults to the main worktree's root, the checkout
    ``vrg-release`` runs from. Returns ``None`` when ``[publish].release`` is
    true. A missing or invalid ``vergil.toml`` raises, as ``read_config``
    does: an unreadable declaration is never taken as a release.
    """
    root = repo_root if repo_root is not None else git.main_worktree_root()
    if config.read_config(root).publish.release:
        return None
    return (
        "this repo is not released ([publish].release is false or absent in "
        "vergil.toml); nothing was changed."
    )
