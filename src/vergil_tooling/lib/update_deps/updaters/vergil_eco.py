"""Vergil ecosystem updater: normalize internal refs, optionally bump the version.

The source of truth is ``[dependencies].vergil`` in ``vergil.toml`` (see
``vergil_tooling.lib.vergil_refs``). Every secondary reference — workflow
``uses: vergil-*/...@vX.Y`` and the Claude marketplace ref — must match it.
``normalize`` rewrites drifting refs to the source-of-truth version; ``bump``
first rewrites the source of truth, then normalizes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.updater import UpdateResult
from vergil_tooling.lib.vergil_refs import (
    _REF_RE,
    _SOURCE_RE,
    MARKETPLACE_NAME,
    format_version,
    is_marketplace_source_repo,
    read_source_version,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

# Re-exported from vergil_refs for back-compatibility with existing importers.
__all__ = [
    "VergilUpdater",
    "format_version",
    "normalize_refs",
    "read_source_version",
    "set_source_version",
]


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


def normalize_claude_ref(base: Path, target: str) -> Path | None:
    """Set the marketplace ``source.ref`` in ``.claude/settings.json`` to *target*.

    *target* is the derived ``vX.Y`` (or ``develop`` for the source repo). The
    file is edited structurally (parsed JSON, re-dumped at indent 2) because the
    ref may need to be *inserted* where none exists — a regex cannot do that
    safely. Returns the path if changed, else ``None``. A missing file or
    missing marketplace entry is a clean no-op.
    """
    settings_path = base / ".claude" / "settings.json"
    if not settings_path.is_file():
        return None
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    try:
        source = data["extraKnownMarketplaces"][MARKETPLACE_NAME]["source"]
    except (KeyError, TypeError):
        return None
    if not isinstance(source, dict) or source.get("ref") == target:
        return None
    source["ref"] = target
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return settings_path


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
        is_self = is_marketplace_source_repo(base)
        if ctx.vergil_bump is not None:
            target = format_version(ctx.vergil_bump)
            bumped = set_source_version(base, target)
            normalized = normalize_refs(base, target)
            claude = normalize_claude_ref(base, "develop" if is_self else target)
            return UpdateResult(
                updater=self.name,
                changed=bumped or bool(normalized) or claude is not None,
                summary=f"bump vergil to {target}",
                commit_message=f"chore(deps): bump vergil to {target}",
            )
        target = read_source_version(base)
        normalized = normalize_refs(base, target)
        claude = normalize_claude_ref(base, "develop" if is_self else target)
        changed = bool(normalized) or claude is not None
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
