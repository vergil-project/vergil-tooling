"""Vergil ecosystem updater: normalize internal refs, optionally bump the version.

The source of truth is ``[dependencies].vergil`` in ``vergil.toml``. Every
secondary reference — workflow ``uses: vergil-*/...@vX.Y`` — must match it.
``normalize`` rewrites drifting refs to the source-of-truth version; ``bump``
first rewrites the source of truth, then normalizes.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

# A vergil-internal reusable-workflow ref: owner starts with ``vergil-`` (e.g.
# vergil-project), pinned to a ``vX.Y`` tag. Third-party actions (actions/...,
# docker/..., github/...) do not match and are left alone.
_REF_RE = re.compile(r"(uses:\s*vergil-[\w.-]+/[^@\s]+@)v\d+\.\d+")

# The ``vergil = "..."`` line in vergil.toml's [dependencies] table.
_SOURCE_RE = re.compile(r'(?m)^(vergil\s*=\s*)"[^"]*"')

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)$")


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
        return raw["dependencies"]["vergil"]
    except KeyError as exc:
        raise UpdateDepsError(
            phase="vergil",
            command="read_source_version",
            message="vergil.toml [dependencies].vergil not found.",
        ) from exc


def set_source_version(base: Path, target: str) -> bool:
    """Rewrite the ``vergil = "..."`` line in vergil.toml. Return True if changed."""
    path = base / "vergil.toml"
    text = path.read_text()
    new = _SOURCE_RE.sub(lambda _: f'vergil = "{target}"', text, count=1)
    if new == text:
        return False
    path.write_text(new)
    return True


def normalize_refs(base: Path, target: str) -> list[Path]:
    """Rewrite drifting ``uses: vergil-*@vX.Y`` refs to *target*. Return changed files."""
    workflows = base / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    changed: list[Path] = []
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        text = path.read_text()
        new = _REF_RE.sub(lambda m: m.group(1) + target, text)
        if new != text:
            path.write_text(new)
            changed.append(path)
    return changed


def _base(ctx: UpdateDepsContext) -> Path:
    """The directory updaters operate on — the worktree once preflight made it."""
    return ctx.worktree_path if ctx.worktree_path is not None else ctx.repo_root


class VergilUpdater:
    """Keep vergil-ecosystem references consistent; bump the version on request."""

    name = "vergil"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return (_base(ctx) / "vergil.toml").is_file()

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        if ctx.vergil_bump is not None:
            target = format_version(ctx.vergil_bump)
            bumped = set_source_version(base, target)
            normalized = normalize_refs(base, target)
            return UpdateResult(
                updater=self.name,
                changed=bumped or bool(normalized),
                summary=f"bump vergil to {target}",
                commit_message=f"chore(deps): bump vergil to {target}",
            )
        target = read_source_version(base)
        normalized = normalize_refs(base, target)
        changed = bool(normalized)
        return UpdateResult(
            updater=self.name,
            changed=changed,
            summary=(
                f"normalize vergil refs to {target}"
                if changed
                else f"vergil refs already at {target}"
            ),
            commit_message=f"chore(deps): normalize vergil ecosystem refs ({target})",
        )
