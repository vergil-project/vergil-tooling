"""Shared derivation of vergil-ecosystem version references.

The source of truth is ``[dependencies].vergil`` in ``vergil.toml``. The
reusable-workflow pins must equal the version computed from it. The Claude
plugin marketplace ref is the exception: under the single-channel distribution
model (epic vergil-project/.github#45) the plugin has exactly one released
channel on ``main``, so *every* repo — consumers and the plugin source repo
alike — pins the marketplace at ``main``. The old model (source repo on
``develop``, consumers on the version tag) is deprecated; a repo still carrying
one of those refs warns-then-enforces toward ``main`` (#1974).

This module holds the pure derivation and read helpers shared by the
update_deps writer (``vergil_eco``) and the repo_config auditor, so the two can
never disagree about what a ref should be. It imports only the lightweight
``UpdateDepsError`` type for back-compatible error semantics.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: A vergil-internal reusable-workflow ref: owner starts with ``vergil-``,
#: pinned to a ``vX.Y`` tag. Group 1 is the prefix through ``@``; group 2 is the
#: version. Third-party actions (actions/..., docker/...) do not match.
_REF_RE = re.compile(r"(uses:\s*vergil-[\w.-]+/[^@\s]+@)(v\d+\.\d+)")

#: The ``vergil = "..."`` line in vergil.toml's [dependencies] table.
_SOURCE_RE = re.compile(r'(?m)^(vergil\s*=\s*)"[^"]*"')

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)$")

#: The marketplace name keyed under ``extraKnownMarketplaces``.
MARKETPLACE_NAME = "vergil-marketplace"

#: The single released channel every repo pins the marketplace at. Under the
#: single-channel model (#1974) there is no per-repo or source-repo variation.
EXPECTED_MARKETPLACE_REF = "main"

#: A pre-single-channel marketplace ref: a bare version (``2.1``, ``2.0.7``) or
#: a ``vX.Y[.Z]`` tag. ``develop`` is handled separately in
#: ``is_deprecated_marketplace_ref``.
_VERSION_REF_RE = re.compile(r"^v?\d+\.\d+(?:\.\d+)?$")


def is_deprecated_marketplace_ref(ref: str) -> bool:
    """True if *ref* is a pre-single-channel marketplace ref.

    The old model pinned the source repo at ``develop`` and consumers at the
    version tag. Either earns a deprecation warning (not a hard failure) during
    the bridge period while repos migrate to ``main`` (#1974).
    """
    return ref == "develop" or bool(_VERSION_REF_RE.match(ref))


def format_version(raw: str) -> str:
    """Normalize a user-supplied version (``2.2`` or ``v2.2``) to ``vX.Y``."""
    match = _VERSION_RE.match(raw.strip())
    if match is None:
        raise UpdateDepsError(
            phase="vergil",
            command="format_version",
            message=f"invalid vergil version '{raw}' (expected X.Y, e.g. 2.2).",
        )
    return f"v{match.group(1)}.{match.group(2)}"


def read_source_version(base: Path) -> str:
    """Return ``[dependencies].vergil`` (the source of truth) from vergil.toml."""
    with (base / "vergil.toml").open("rb") as handle:
        raw = tomllib.load(handle)
    try:
        value: str = raw["dependencies"]["vergil"]
    except KeyError as exc:
        raise UpdateDepsError(
            phase="vergil",
            command="read_source_version",
            message="vergil.toml [dependencies].vergil not found.",
        ) from exc
    return value


def iter_workflow_refs(base: Path) -> Iterator[tuple[Path, str]]:
    """Yield ``(workflow_file, ref_version)`` for each vergil-* reusable-workflow
    pin under ``.github/workflows``."""
    workflows = base / ".github" / "workflows"
    if not workflows.is_dir():
        return
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        text = path.read_text(encoding="utf-8")
        for match in _REF_RE.finditer(text):
            yield path, match.group(2)
