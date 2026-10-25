"""Make room for a cached-image build by pruning finished branches' images.

Per-branch cached images pile up until someone runs ``vrg-container-cache
prune``; on 2026-10-24 a Lima VM's root disk filled completely and every
``vrg-container-run`` failed (#3571). :func:`ensure_build_space` is the
automatic trigger: just before a cache build it reads the free space of the
image store's filesystem and, below a threshold, runs the same prune as
``vrg-container-cache prune --apply``
(:func:`~vergil_tooling.lib.cache_prune.prune_finished_images`) against the
same repo set (:func:`~vergil_tooling.lib.live_branches.default_repo_roots`).

Where no repo set can be confirmed (the physical host has no default) nothing
is guessed: the prune is skipped and says so. A prune that fails, or fails to
remove an image, is reported loudly. Either way the build then goes ahead only
if the store still has room for one image; otherwise it stops with the free-space
numbers and the manual command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib.cache_prune import prune_finished_images
from vergil_tooling.lib.live_branches import RepoSetError, default_repo_roots, discover_repos
from vergil_tooling.lib.process_errors import error_text

# The free space, in GB, below which a cache build first prunes finished
# branches' images. ``0`` turns the auto-prune off.
PRUNE_FREE_GB_ENV = "VRG_CACHE_PRUNE_FREE_GB"
DEFAULT_PRUNE_FREE_GB = 10.0

# The free space, in GB, a cache build needs after a prune to go ahead at all
# (capped at the threshold): one cached image is about 1.3-2 GB, so below this
# the build would fail part way, with a less useful error, after minutes of work.
BUILD_FREE_GB_ENV = "VRG_CACHE_BUILD_FREE_GB"
DEFAULT_BUILD_FREE_GB = 3.0

_GB = 1_000_000_000

_MANUAL_PRUNE = "vrg-container-cache prune --apply (add --root DIR on the physical host)"


class CacheSpaceError(RuntimeError):
    """A cache build cannot go ahead: too little free space, or a bad setting."""


def _env_gb(name: str, default: float) -> float:
    """Return environment variable *name* as a number of GB, or *default*.

    Unset or empty means *default*. A value that is not a non-negative number
    raises :class:`CacheSpaceError`: a typo must not silently change a limit.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not value >= 0:  # also rejects nan
        msg = f"{name}={raw!r} is not a non-negative number of GB"
        raise CacheSpaceError(msg)
    return value


def prune_threshold_gb() -> float:
    """Return the auto-prune threshold in GB, from ``VRG_CACHE_PRUNE_FREE_GB``.

    Unset or empty means :data:`DEFAULT_PRUNE_FREE_GB`. A value that is not a
    non-negative number raises :class:`CacheSpaceError`: a typo must not
    silently turn the prune off or set it to something else.
    """
    return _env_gb(PRUNE_FREE_GB_ENV, DEFAULT_PRUNE_FREE_GB)


def build_floor_gb() -> float:
    """Return the post-prune build floor in GB, from ``VRG_CACHE_BUILD_FREE_GB``.

    Unset or empty means :data:`DEFAULT_BUILD_FREE_GB`. A value that is not a
    non-negative number raises :class:`CacheSpaceError`: a typo must not
    silently let a build start without room for it, or refuse one that has it.
    """
    return _env_gb(BUILD_FREE_GB_ENV, DEFAULT_BUILD_FREE_GB)


def _runtime_output(runtime: str, *args: str) -> str:
    """Run ``<runtime> <args>`` and return its stripped stdout; raise on failure."""
    result = subprocess.run(  # noqa: S603
        [runtime, *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = f"'{runtime} {' '.join(args)}' failed: {result.stderr.strip() or 'no output'}"
        raise RuntimeError(msg)
    return result.stdout.strip()


def image_store_path(runtime: str) -> Path:
    """Return the directory the runtime keeps its images under.

    * **docker** — its reported ``DockerRootDir``.
    * **nerdctl** — containerd's root: ``$XDG_DATA_HOME/containerd`` (default
      ``~/.local/share/containerd``) when rootless, as on every vergil VM, else
      ``/var/lib/containerd``. nerdctl does not report the root, so rootless is
      read from its security options.

    Raises ``RuntimeError`` when the runtime cannot be asked.
    """
    if runtime == "nerdctl":
        info = _runtime_output(runtime, "info", "--format", "{{json .SecurityOptions}}")
        options = json.loads(info)
        if "name=rootless" in (options or []):
            data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
            return Path(data_home) / "containerd"
        return Path("/var/lib/containerd")
    return Path(_runtime_output(runtime, "info", "--format", "{{.DockerRootDir}}"))


def free_gb(path: Path) -> float:
    """Return the free space, in GB, of the filesystem holding *path*."""
    return shutil.disk_usage(path).free / _GB


def _prune(runtime: str) -> bool:
    """Prune finished branches' images, printing what was removed; True on success.

    Every way the prune can fail — no repo set, an empty one, an unreadable
    image list or repo, a refused ``rmi`` — is printed loudly to stderr.
    """
    try:
        roots = default_repo_roots()
        repos = discover_repos(roots)
    except RepoSetError as exc:
        print(f"WARNING: cannot auto-prune cached images: {exc}", file=sys.stderr)
        return False
    if not repos:
        where = ", ".join(str(r) for r in roots)
        print(
            f"WARNING: cannot auto-prune cached images: no git repositories under {where}; "
            "refusing to prune against an empty repo set.",
            file=sys.stderr,
        )
        return False
    try:
        removed, failures = prune_finished_images(repos, runtime=runtime)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(
            f"WARNING: auto-prune of cached images failed: {' '.join(error_text(exc).split())}",
            file=sys.stderr,
        )
        return False
    print(f"Auto-prune removed {len(removed)} cached image(s) of finished branches.")
    for ref in removed:
        print(f"  removed {ref}")
    for ref, error in failures:
        print(f"WARNING: auto-prune could not remove {ref}: {error}", file=sys.stderr)
    return not failures


def ensure_build_space(runtime: str) -> None:
    """Before a cache build, prune finished images if the image store runs low.

    Silent when the store has at least :func:`prune_threshold_gb` free. Below
    that it runs the prune (see :func:`_prune`), then measures again: the build
    goes ahead with at least :func:`build_floor_gb` free (or the threshold, if
    that is lower), with a warning while still under the threshold; with less
    it raises :class:`CacheSpaceError` naming the numbers and the manual
    command. When the store cannot be located or measured — Docker Desktop keeps
    it inside its own VM — the check is skipped with a note, never guessed. Both
    settings are read first, so a bad value stops every build, not only a low one.
    """
    threshold = prune_threshold_gb()
    build_floor = build_floor_gb()
    if threshold == 0:
        return
    try:
        store = image_store_path(runtime)
        free = free_gb(store)
    except (RuntimeError, ValueError, OSError) as exc:
        print(
            f"note: cannot measure the image store's free space ({error_text(exc)}); "
            "skipping the low-disk auto-prune.",
            file=sys.stderr,
        )
        return
    if free >= threshold:
        return

    print(
        f"Low disk: {free:.1f} GB free under {store} (auto-prune threshold "
        f"{threshold:g} GB, {PRUNE_FREE_GB_ENV}). Pruning finished branches' cached images..."
    )
    pruned = _prune(runtime)
    free = free_gb(store)
    if free >= threshold:
        print(f"Free space now {free:.1f} GB.")
        return
    # The floor never exceeds the threshold; name whichever setting set it.
    floor, floor_env = min((build_floor, BUILD_FREE_GB_ENV), (threshold, PRUNE_FREE_GB_ENV))
    if free < floor:
        what = "the auto-prune failed and " if not pruned else ""
        msg = (
            f"not enough disk space to build the cached image: {what}{free:.1f} GB is free "
            f"under {store}, and a build needs at least {floor:g} GB ({floor_env}). "
            f"Free space with {_MANUAL_PRUNE}, or remove other images."
        )
        raise CacheSpaceError(msg)
    print(
        f"WARNING: still only {free:.1f} GB free under {store} (below {threshold:g} GB); "
        f"building anyway. Free more with {_MANUAL_PRUNE}.",
        file=sys.stderr,
    )
