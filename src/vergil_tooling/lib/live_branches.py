"""Live branches across every repository a machine works in.

A per-branch cached dev image records its branch but not the repository that
built it (the tag is ``<base_tag>--<sanitized_branch>--<hash>``), and every repo
of a language shares one base image. So whether an image's branch is finished
cannot be asked of one repository: it has to be asked of all of them. This
module answers it — find the repo set, then collect each repo's live branches —
so ``vrg-container-cache prune`` (#3481) and the finalize-time orphan prune
(#3566) share one conservative definition of "live".

A branch is *live* in a repo when it is a local branch there (which covers every
worktree's branch) or exists on the repo's ``origin``. Origin is read with
``ls-remote``, not the possibly-stale remote-tracking refs, so a branch deleted
on GitHub is seen as gone. Every git failure propagates: a repo whose branches
cannot be read must stop the caller, never count as "no live branches".
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.platform_env import Platform, current_platform
from vergil_tooling.lib.vm_cloud import CLOUD_PROJECTS_VOLUME

if TYPE_CHECKING:
    from collections.abc import Iterable

# How deep under a root to look for repositories: the root itself, ``<org>`` and
# ``<org>/<repo>`` — the projects layout every vergil VM uses.
_MAX_DEPTH = 2


class RepoSetError(Exception):
    """The repo set cannot be determined; the message says what to pass instead."""


def default_repo_roots() -> list[Path]:
    """Return the platform's default repo roots.

    * **cloud-vm** — the checkout volume, ``/vergil/projects`` (each checkout is
      ``<volume>/<org>/<repo>``; see ``vm_cloud.ensure_host_path``).
    * **local-vm** — the projects directory Lima mounts path-preserved
      (``lima.create_vm``). The guest is not told that path, so it is derived
      from the current repo, which ``vrg-vm`` opens at ``<projects_dir>/<org>/<repo>``:
      the main worktree's grandparent, accepted only when it is a mount point.
    * **physical-host** — no default; the human names the roots.

    Raises :class:`RepoSetError` when no default can be confirmed.
    """
    plat = current_platform()
    if plat is Platform.CLOUD_VM:
        return [Path(CLOUD_PROJECTS_VOLUME)]
    if plat is Platform.LOCAL_VM:
        try:
            main_root = git.main_worktree_root()
        except subprocess.CalledProcessError as exc:
            msg = (
                "cannot find the Lima projects mount: not inside a git repository. "
                "Run from a checkout under the projects directory, or pass --root DIR."
            )
            raise RepoSetError(msg) from exc
        projects = main_root.parent.parent
        if not projects.is_mount():
            msg = (
                f"cannot find the Lima projects mount: {projects} (two levels above "
                f"{main_root}) is not a mount point. Pass --root DIR."
            )
            raise RepoSetError(msg)
        return [projects]
    msg = "no default repo set on the physical host. Pass --root DIR for each projects directory."
    raise RepoSetError(msg)


def _is_repo(path: Path) -> bool:
    """True when *path* is a checkout root (a ``.git`` directory or worktree file)."""
    return (path / ".git").exists()


def discover_repos(roots: Iterable[Path]) -> list[Path]:
    """Return the git repositories at or below each of *roots*, sorted, deduplicated.

    A root may be a repository itself, a directory of repositories, or a
    projects directory of ``<org>/<repo>`` checkouts. The search does not descend
    into a repository, so a repo's own ``.worktrees/`` are not separate entries
    (their branches are the repo's local branches). A root that does not exist
    or is not a directory raises :class:`RepoSetError` — a typo must not shrink
    the repo set silently.
    """
    found: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            msg = f"repo root {root} does not exist or is not a directory"
            raise RepoSetError(msg)
        frontier = [root]
        for depth in range(_MAX_DEPTH + 1):
            next_frontier: list[Path] = []
            for path in frontier:
                if _is_repo(path):
                    found.add(path.resolve())
                elif depth < _MAX_DEPTH:
                    next_frontier.extend(p for p in sorted(path.iterdir()) if p.is_dir())
            frontier = next_frontier
    return sorted(found)


def repo_live_branches(repo: Path) -> frozenset[str]:
    """Return *repo*'s live branches: its local branches plus ``origin``'s.

    A repository with no ``origin`` remote contributes its local branches only.
    The ``ls-remote`` authenticates as *repo*'s own org, not the CWD's (see
    ``git._git_env``), so a repo set spanning orgs reads each origin with the
    right App installation. Git failures (an unreadable repo, an unreachable or
    unauthorised origin) raise ``CalledProcessError``.
    """
    live = git.local_branches(repo)
    if "origin" in git.remote_names(repo):
        live |= git.remote_branches("origin", repo)
    return frozenset(live)


def live_branches(repos: Iterable[Path]) -> dict[Path, frozenset[str]]:
    """Map each of *repos* to its live branches (see :func:`repo_live_branches`)."""
    return {repo: repo_live_branches(repo) for repo in repos}
