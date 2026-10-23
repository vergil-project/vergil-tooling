"""Vergil ecosystem updater: normalize internal refs, optionally bump the version.

The source of truth is ``[dependencies].vergil`` in ``vergil.toml`` (see
``vergil_tooling.lib.vergil_refs``). Workflow pins (``uses: vergil-*/...@vX.Y``)
must match it. The Claude marketplace ref is decoupled: under the single-channel
model (#1974) it is always ``main``, the plugin's one released channel, never a
version. ``normalize`` rewrites drifting refs; ``bump`` first rewrites the
source of truth, then normalizes. Refs are normalized in workflows and in every
composite ``action.yml``/``action.yaml``.

Either way, a ``[package.python] runtime`` pin (``x.y.z``) is bumped to the
CPython patch its per-series ``vergil-python<x.y>`` package serves on every
build and install-test index (spec §8.2, vergil-tooling#3456). The pin never
leaves its series: a newer served series is reported as held back, never
applied.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.update_deps import vergil_index
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult
from vergil_tooling.lib.vergil_refs import (
    _REF_RE,
    _SOURCE_RE,
    EXPECTED_MARKETPLACE_REF,
    MARKETPLACE_NAME,
    format_version,
    read_source_version,
)
from vergil_tooling.lib.versions.extensions.python import runtime_series

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from vergil_tooling.lib.update_deps.context import UpdateDepsContext
    from vergil_tooling.lib.update_deps.vergil_index import ServedRuntime

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


# Directories never scanned for action files: tool caches and nested checkouts.
_SKIP_DIRS = frozenset({".git", ".venv", ".worktrees", "node_modules", ".tox"})
_ACTION_FILES = frozenset({"action.yml", "action.yaml"})
_RUNTIME_LINE_RE = r'^(runtime\s*=\s*")({old})(")'
_SERIES_NOTE = "[package.python] runtime: a new series is a reviewed change, never applied"


def _ref_files(base: Path) -> Iterator[Path]:
    """Workflow files, then every composite action file outside skipped directories."""
    workflows = base / ".github" / "workflows"
    if workflows.is_dir():
        yield from sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])
    actions: list[Path] = []
    for root, dirs, files in base.walk():
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        actions.extend(root / f for f in files if f in _ACTION_FILES)
    yield from sorted(actions)


def normalize_refs(base: Path, target: str) -> list[Path]:
    """Rewrite drifting ``uses: vergil-*@vX.Y`` refs in workflows and action files.

    Return the changed files.
    """
    changed: list[Path] = []
    for path in _ref_files(base):
        text = path.read_text()
        new = _REF_RE.sub(lambda m: m.group(1) + target, text)
        if new != text:
            path.write_text(new)
            changed.append(path)
    return changed


def _declared_runtime(text: str) -> str | None:
    """The ``[package.python] runtime`` pin in vergil.toml *text*, if declared."""
    data: dict[str, Any] = tomllib.loads(text)
    runtime = data.get("package", {}).get("python", {}).get("runtime")
    return runtime if isinstance(runtime, str) else None


def _patch_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def bump_runtime(base: Path, served: ServedRuntime) -> tuple[str | None, HeldBack | None]:
    """Bump ``[package.python] runtime`` to the served patch; report a newer series.

    Returns the new runtime (``None`` if unchanged) and the held-back entry for a
    newer served series (``None`` if there is none). A served patch on another
    series, or older than the pin, fails loud rather than moving the pin.
    """
    path = base / "vergil.toml"
    text = path.read_text()
    current = _declared_runtime(text)
    if current is None:
        return None, None
    entry = None
    if served.newer_series is not None:
        entry = HeldBack(
            source="vergil",
            name="vergil-python",
            current=current,
            latest=served.newer_series,
            note=_SERIES_NOTE,
        )
    patch = served.patch
    series = runtime_series(current)
    if runtime_series(patch) != series:
        msg = (
            f"vergil-python{series} serves CPython {patch}, which is not on the pin's "
            f"series {series}; refusing to move [package.python] runtime {current} "
            f"across series."
        )
        raise UpdateDepsError(phase="update", command="bump_runtime", message=msg)
    if patch == current:
        return None, entry
    if _patch_key(patch) < _patch_key(current):
        msg = (
            f"[package.python] runtime pins CPython {current}, but vergil-python{series} "
            f"serves the older {patch}, so the package build would fail. Refusing to "
            f"downgrade the pin; set it to {patch} by hand, or remove it so the runtime "
            f"follows the resolved version."
        )
        raise UpdateDepsError(phase="update", command="bump_runtime", message=msg)
    pattern = re.compile(_RUNTIME_LINE_RE.format(old=re.escape(current)), re.MULTILINE)
    new_text, count = pattern.subn(lambda m: m.group(1) + patch + m.group(3), text)
    if count != 1:
        msg = f'Expected exactly one runtime = "{current}" line in vergil.toml, found {count}.'
        raise UpdateDepsError(phase="update", command="bump_runtime", message=msg)
    path.write_text(new_text)
    return patch, entry


def normalize_claude_ref(base: Path, target: str) -> Path | None:
    """Set the marketplace ``source.ref`` in ``.claude/settings.json`` to *target*.

    *target* is the single released channel ``main`` (#1974). The
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

    def __init__(
        self,
        served_runtime: Callable[[Path], ServedRuntime] = vergil_index.served_runtime_for,
    ) -> None:
        self._served_runtime = served_runtime

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return (_base(ctx) / "vergil.toml").is_file()

    def _runtime(self, base: Path) -> tuple[str | None, list[HeldBack]]:
        """Bump the runtime pin from the live index; never query it without a pin."""
        if _declared_runtime((base / "vergil.toml").read_text()) is None:
            return None, []
        new, entry = bump_runtime(base, self._served_runtime(base))
        return new, [entry] if entry is not None else []

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        if ctx.vergil_bump is not None:
            target = format_version(ctx.vergil_bump)
            bumped = set_source_version(base, target)
            normalized = normalize_refs(base, target)
            claude = normalize_claude_ref(base, EXPECTED_MARKETPLACE_REF)
            runtime, held = self._runtime(base)
            parts = [f"bump vergil to {target}"]
            if runtime is not None:
                parts.append(f"vergil-python runtime {runtime}")
            return UpdateResult(
                updater=self.name,
                changed=bumped or bool(normalized) or claude is not None or runtime is not None,
                summary="; ".join(parts),
                commit_message="chore(deps): " + "; ".join(parts),
                held_back=held,
            )
        target = read_source_version(base)
        normalized = normalize_refs(base, target)
        claude = normalize_claude_ref(base, EXPECTED_MARKETPLACE_REF)
        refs_changed = bool(normalized) or claude is not None
        runtime, held = self._runtime(base)
        parts = [f"normalize vergil refs to {target}"] if refs_changed else []
        if runtime is not None:
            parts.append(f"vergil-python runtime {runtime}")
        return UpdateResult(
            updater=self.name,
            changed=bool(parts),
            summary="; ".join(parts) if parts else f"vergil refs already at {target}",
            commit_message="chore(deps): " + ("; ".join(parts) if parts else "vergil refs"),
            held_back=held,
        )
