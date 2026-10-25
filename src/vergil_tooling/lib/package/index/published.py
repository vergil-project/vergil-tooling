"""Refuse to release a package identity that an earlier release already published (#3309).

The index refuses two collected artifacts that share an identity
``(fmt, name, full_version, arch)`` but differ in bytes (:mod:`.retention`).
Package builds are not byte-reproducible, so *any* rebuild at a published
identity is such a conflict. The index is built from every release of every
repo, so that refusal — correct as it is — fires only after the offending
release has published, and then fails every repo's index publish until the
duplicate assets are removed by hand (the vergil-python v1.0.1 incident).

This module applies the same rule to a release *before* it publishes:

- **What would be published** is fully known without building: the package
  name (:func:`naming.package_name`), the package version
  (:func:`naming.package_version` — exactly what ``vrg-package build`` uses), and one
  ``(fmt, arch, suite)`` per artifact from :func:`matrix.manifest` (the
  release's own ``packages-manifest.json``), whose revision comes from
  :class:`collect.ManifestEntry` — the same rule the index checks a release's
  files against.
- **What is already published** is every stable release of the repo that the
  index collects (:func:`collect.stable_tags`, :func:`collect.release_packages`),
  identified from its attached package file names. nFPM names each file from
  the very name/version/release/arch the index later reads from the package
  metadata, and the index pairs files with manifest entries by that same name
  shape. Reading names, not downloading packages, keeps the check cheap and
  free of ``dpkg-deb``/``rpm`` on the release host.

Where it runs, earliest first:

1. ``vrg-release`` preflight, on a fresh start, before anything is branched,
   tagged or published — the earliest point that knows the release version.
2. ``vrg-package check-unpublished``, for the CD packaging path to run before
   building — the backstop for a release that did not go through
   ``vrg-release``.

The release's own tag is never "earlier": it is excluded, so re-running the
check for a release that already published is not a self-conflict.

It fails closed: a ``gh`` error (after the shared transient retry), or an
attached package whose file name does not parse, raises — the check never
skips.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError, matrix, naming
from vergil_tooling.lib.package.index import collect
from vergil_tooling.lib.package.index.collect import Identity, ManifestEntry
from vergil_tooling.lib.package.repo_setup import Run, local_run

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.config import VergilConfig

# nFPM file names. A Debian name or version never contains ``_``; an rpm
# version or release never contains ``-`` (``[package].version`` allows neither).
_DEB_FILE = re.compile(r"(?P<name>[^_]+)_(?P<ver>[^_]+)_(?P<arch>[^_]+)\.deb")
_RPM_FILE = re.compile(r"(?P<name>.+)-(?P<ver>[^-]+-[^-]+)\.(?P<arch>[^.]+)\.rpm")


def planned(repo_root: Path, cfg: VergilConfig, version: str) -> list[Identity]:
    """The identities a release at repo ``version`` would publish.

    Every ``[[package]]`` entry, in entry order; each entry's identities in
    manifest order.
    """
    out: list[Identity] = []
    for pkg in cfg.packages:
        name = naming.package_name(repo_root, pkg)
        pkg_version = naming.package_version(pkg, version)
        for raw in matrix.manifest(matrix.resolve(pkg, name))["artifacts"]:
            entry = ManifestEntry(raw["fmt"], raw["arch"], raw["suite"])
            out.append(Identity(entry.fmt, name, f"{pkg_version}-{entry.revision}", entry.arch))
    return out


def parse_filename(filename: str) -> Identity:
    """The identity of an nFPM-named ``.deb``/``.rpm``; anything else raises."""
    if m := _DEB_FILE.fullmatch(filename):
        return Identity("deb", m["name"], m["ver"], m["arch"])
    if (m := _RPM_FILE.fullmatch(filename)) and m["arch"] in collect.RPM_ARCH_TO_ARCH:
        return Identity("rpm", m["name"], m["ver"], collect.RPM_ARCH_TO_ARCH[m["arch"]])
    msg = f"cannot read a package identity from the file name {filename!r}"
    raise PackageError(msg)


def published(product: str, exclude_tag: str, gh: Run = local_run) -> dict[Identity, str]:
    """Every identity ``product`` has published, mapped to its earliest release tag.

    Only the releases the index collects count; ``exclude_tag`` (the release
    being checked) never does.
    """
    out: dict[Identity, str] = {}
    tags = [t for t in collect.stable_tags(product, gh) if t != exclude_tag]
    # Oldest first, so each identity maps to the release that first published it.
    for tag in sorted(tags, key=lambda t: tuple(int(x) for x in t[1:].split("."))):
        for filename in collect.release_packages(product, tag, gh) or []:
            try:
                ident = parse_filename(filename)
            except PackageError as exc:
                msg = f"{product}@{tag}: {exc}"
                raise PackageError(msg) from exc
            out.setdefault(ident, tag)
    return out


def check_unpublished(
    product: str, tag: str, identities: list[Identity], gh: Run = local_run
) -> None:
    """Raise if any of ``identities`` was already published by another release of ``product``."""
    prior = published(product, tag, gh)
    clashes = [i for i in identities if i in prior]
    if not clashes:
        return
    lines = "\n".join(f"  - {i}: already published by {product}@{prior[i]}" for i in clashes)
    msg = (
        f"{product}@{tag} would re-publish {len(clashes)} package(s) at a version an "
        f"earlier release already published:\n{lines}\n"
        "Package builds are not byte-reproducible, so the rebuilt files would differ "
        "and the shared package index would refuse them, failing every repo's index "
        "publish. Bump the package version before releasing: a new "
        "[package].upstream-version in vergil.toml, or a new repo release for an "
        "upstream-version package (its version ends in the repo version); for a "
        "[package].version override, set a new version."
    )
    raise PackageError(msg)


def check_release(
    repo_root: Path, cfg: VergilConfig, product: str, version: str, gh: Run = local_run
) -> list[Identity]:
    """Check a release at repo ``version`` of ``product``; return what it would publish.

    A repo without ``[package]`` publishes no packages: nothing to check.
    """
    if not cfg.packages:
        return []
    identities = planned(repo_root, cfg, version)
    check_unpublished(product, f"v{version}", identities, gh)
    return identities
