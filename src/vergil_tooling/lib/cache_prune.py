"""Decide which per-branch cached dev images are finished, and remove them.

``vrg-container-run`` builds one cached image per branch, and on an agent VM
nothing removed them: ``vrg-finalize-pr`` cleans the image store of the machine
it runs on, which is the human's Mac, not the VM (#3481). This is the sweep
behind ``vrg-container-cache prune`` and ``vrg-finalize-pr``'s orphan sweep
(#3566), and the home of every cached-image removal, so each one reports a
failed ``rmi`` rather than ignoring it.

The tag does not record the repository, and the branch in it is sanitized
(``/`` becomes ``-``), so an image cannot be mapped back to exactly one branch.
The rule is therefore deliberately one-sided: an image is removed only when its
sanitized branch matches **no** live branch in **any** repo of the repo set
(:mod:`vergil_tooling.lib.live_branches`). A name collision, a merged branch not
yet deleted, or a stale local branch all keep the image; the cost of a wrong
keep is disk, the cost of a wrong removal is a cold rebuild of live work.
Protected branches and images any container references are never removed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib.container import detect_runtime
from vergil_tooling.lib.container_cache import parse_cache_image_ref, sanitize_branch
from vergil_tooling.lib.live_branches import live_branches

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

# Long-lived branches whose images are never pruned, whatever the repo set says
# (the eternal branches of every branching model, as in vrg-finalize-pr).
PROTECTED_BRANCHES = frozenset({"develop", "main", "release"})

_SHA_PREFIX = "sha256:"


@dataclass(frozen=True)
class CacheImage:
    """A cached per-branch image: its reference, content id and sanitized branch."""

    ref: str
    image_id: str
    branch: str


@dataclass(frozen=True)
class PruneVerdict:
    """Whether to remove *image*, and why."""

    image: CacheImage
    remove: bool
    reason: str


def _runtime_lines(runtime: str, *args: str) -> list[str]:
    """Run ``<runtime> <args>`` and return its stdout lines; raise on failure."""
    result = subprocess.run(  # noqa: S603
        [runtime, *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = f"'{runtime} {' '.join(args)}' failed: {result.stderr.strip() or 'no output'}"
        raise RuntimeError(msg)
    return [line for line in result.stdout.splitlines() if line.strip()]


def list_cache_images(runtime: str) -> list[CacheImage]:
    """Return the cached per-branch images in the local image store.

    Only references shaped exactly like a cached image are returned (see
    :func:`~vergil_tooling.lib.container_cache.parse_cache_image_ref`); base and
    foreign images are never candidates. Raises ``RuntimeError`` when the
    runtime cannot list images.
    """
    images: list[CacheImage] = []
    for line in _runtime_lines(runtime, "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}"):
        ref, _tab, image_id = line.partition("\t")
        parsed = parse_cache_image_ref(ref.strip())
        if parsed is not None:
            images.append(CacheImage(ref=ref.strip(), image_id=image_id.strip(), branch=parsed[1]))
    return images


def images_in_use(runtime: str) -> set[str]:
    """Return the image of every container, running or stopped.

    Stopped containers count too: ``rmi`` refuses an image a stopped container
    still references, so leaving those alone is both safer and quieter. The
    runtime reports the reference the container was created from, or an id when
    that reference has since moved. Raises ``RuntimeError`` on failure.
    """
    return {line.strip() for line in _runtime_lines(runtime, "ps", "-a", "--format", "{{.Image}}")}


def _strip_sha(value: str) -> str:
    return value.removeprefix(_SHA_PREFIX)


def _is_in_use(image: CacheImage, in_use: Iterable[str]) -> bool:
    """True when a container references *image* by its ref or (short or full) id."""
    image_id = _strip_sha(image.image_id)
    for used in in_use:
        if used == image.ref:
            return True
        used_id = _strip_sha(used)
        if image_id and used_id and (image_id.startswith(used_id) or used_id.startswith(image_id)):
            return True
    return False


def _repo_label(repo: Path) -> str:
    return f"{repo.parent.name}/{repo.name}"


def plan_prune(
    images: Iterable[CacheImage],
    live: Mapping[Path, frozenset[str]],
    in_use: Iterable[str],
) -> list[PruneVerdict]:
    """Return a keep-or-remove verdict, with its reason, for every image.

    Checked in order: a protected branch keeps; a container referencing the
    image keeps; a sanitized match against any repo's live branch keeps.
    Only an image that passes none of these is removed.
    """
    used = list(in_use)
    live_in: dict[str, list[str]] = {}
    for repo, branches in live.items():
        for branch in branches:
            live_in.setdefault(sanitize_branch(branch), []).append(_repo_label(repo))

    verdicts: list[PruneVerdict] = []
    for image in images:
        if image.branch in PROTECTED_BRANCHES:
            verdicts.append(PruneVerdict(image, False, f"protected branch '{image.branch}'"))
        elif _is_in_use(image, used):
            verdicts.append(PruneVerdict(image, False, "in use by a container"))
        elif image.branch in live_in:
            repos = ", ".join(sorted(set(live_in[image.branch])))
            verdicts.append(PruneVerdict(image, False, f"branch '{image.branch}' live in {repos}"))
        else:
            reason = f"branch '{image.branch}' matches no live branch in {len(live)} repo(s)"
            verdicts.append(PruneVerdict(image, True, reason))
    return verdicts


def remove_images(refs: Iterable[str], runtime: str) -> list[tuple[str, str]]:
    """Remove each of *refs* with ``<runtime> rmi``; return ``(ref, error)`` failures.

    No ``--force``: if a container started using an image since the plan was
    made, the runtime refuses and the refusal is reported rather than overridden.
    Every ref is attempted, so one failure does not strand the rest.
    """
    failures: list[tuple[str, str]] = []
    for ref in refs:
        result = subprocess.run(  # noqa: S603
            [runtime, "rmi", ref],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append((ref, result.stderr.strip() or f"exit status {result.returncode}"))
    return failures


def clean_branch_images(branch: str, *, runtime: str = "") -> tuple[int, list[tuple[str, str]]]:
    """Remove every cached image of *branch*; return ``(removed, failures)``.

    *failures* are ``(ref, error)`` pairs from :func:`remove_images`, for the
    caller to report: a failed ``rmi`` is never ignored (#3566). Raises
    ``RuntimeError`` when the runtime cannot list images.
    """
    rt = runtime or detect_runtime()
    sanitized = sanitize_branch(branch)
    refs = [image.ref for image in list_cache_images(rt) if image.branch == sanitized]
    failures = remove_images(refs, rt)
    return len(refs) - len(failures), failures


def prune_orphan_branch_images(
    repos: Iterable[Path], *, runtime: str = ""
) -> tuple[int, list[tuple[str, str]]]:
    """Remove cached images whose branch is finished in every one of *repos*.

    The unattended form of ``vrg-container-cache prune --apply``, for
    ``vrg-finalize-pr``'s swept safety net (#2600): the same
    :func:`plan_prune` verdicts against the live branches of the whole repo
    set, not just the finalized repo's, because the tag does not record its
    repo and every repo of a language shares the base image name (#3566).
    Returns ``(removed, failures)`` as :func:`clean_branch_images`. A failure to
    read the images, the containers or any repo's branches raises
    (``RuntimeError`` or ``CalledProcessError``) before anything is removed.
    """
    rt = runtime or detect_runtime()
    images = list_cache_images(rt)
    if not images:
        return 0, []
    verdicts = plan_prune(images, live_branches(repos), images_in_use(rt))
    doomed = [verdict.image.ref for verdict in verdicts if verdict.remove]
    failures = remove_images(doomed, rt)
    return len(doomed) - len(failures), failures
