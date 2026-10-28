"""Vergil ecosystem updater: normalize internal refs, optionally bump the version.

The source of truth is ``[dependencies].vergil`` in ``vergil.toml`` (see
``vergil_tooling.lib.vergil_refs``). Workflow pins (``uses: vergil-*/...@vX.Y``)
must match it. ``normalize`` rewrites drifting refs; ``bump`` first rewrites
the source of truth, then normalizes. Refs are normalized in workflows and in
every composite ``action.yml``/``action.yaml``. ``.claude/settings.json`` is
never touched: it carries no marketplace entry (epic vergil-project/.github#558).

Either way, a ``[package.python] runtime`` pin (``x.y.z``) is bumped to the
CPython patch its per-series ``vergil-python<x.y>`` package serves on every
build and install-test index (spec §8.2, vergil-tooling#3456). The pin never
leaves its series: a newer served series is reported as held back, never
applied. While the indexes serve different patches, the bump is held back and
reported until they agree.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.package.runtime import SEVERAL_PINS
from vergil_tooling.lib.update_deps import vergil_index
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult
from vergil_tooling.lib.vergil_refs import (
    _REF_RE,
    _SOURCE_RE,
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
    """The ``[package.python] runtime`` pin in vergil.toml *text*, if declared.

    ``package`` is a single ``[package]`` table or a ``[[package]]`` list
    (spec §5.2). At most one ``python``-builder entry may pin a runtime; more
    than one is an ``UpdateDepsError`` rather than a guess. ``[package.python]``
    is valid only with ``builder = "python"`` (``lib.config`` refuses it
    otherwise), so every entry carrying a pin is a ``python``-builder entry.
    """
    data: dict[str, Any] = tomllib.loads(text)
    raw = data.get("package", {})
    entries = raw if isinstance(raw, list) else [raw]
    pins = [
        runtime
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(runtime := entry.get("python", {}).get("runtime"), str)
    ]
    if len(pins) > 1:
        raise UpdateDepsError(phase="update", command="read runtime pin", message=SEVERAL_PINS)
    return pins[0] if pins else None


def _patch_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def bump_runtime(base: Path, served: ServedRuntime) -> tuple[str | None, list[HeldBack]]:
    """Bump ``[package.python] runtime`` to the served patch; report what is held back.

    Returns the new runtime (``None`` if unchanged) and the held-back entries: a
    newer served series, and a bump held while the indexes serve different
    patches (the deb and rpm indexes publish separately, so they briefly
    disagree after each vergil-python release). A served patch on another
    series, or an agreed patch older than the pin, fails loud rather than
    moving the pin.
    """
    path = base / "vergil.toml"
    text = path.read_text()
    current = _declared_runtime(text)
    if current is None:
        return None, []
    held: list[HeldBack] = []
    if served.newer_series is not None:
        held.append(
            HeldBack(
                source="vergil",
                name="vergil-python",
                current=current,
                latest=served.newer_series,
                note=_SERIES_NOTE,
            )
        )
    series = runtime_series(current)
    for index_patch in served.patches.values():
        if runtime_series(index_patch) != series:
            msg = (
                f"{served.package} serves CPython {index_patch}, which is not on the pin's "
                f"series {series}; refusing to move [package.python] runtime {current} "
                f"across series."
            )
            raise UpdateDepsError(phase="update", command="bump_runtime", message=msg)
    patch = served.patch
    if patch is None:
        held.append(
            HeldBack(
                source="vergil",
                name=served.package,
                current=current,
                latest=max(served.patches.values(), key=_patch_key),
                note=f"indexes disagree ({served.disagreement()}); bump held until they agree",
            )
        )
        return None, held
    if patch == current:
        return None, held
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
    return patch, held


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
        return bump_runtime(base, self._served_runtime(base))

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        if ctx.vergil_bump is not None:
            target = format_version(ctx.vergil_bump)
            bumped = set_source_version(base, target)
            normalized = normalize_refs(base, target)
            runtime, held = self._runtime(base)
            parts = [f"bump vergil to {target}"]
            if runtime is not None:
                parts.append(f"vergil-python runtime {runtime}")
            return UpdateResult(
                updater=self.name,
                changed=bumped or bool(normalized) or runtime is not None,
                summary="; ".join(parts),
                commit_message="chore(deps): " + "; ".join(parts),
                held_back=held,
            )
        target = read_source_version(base)
        normalized = normalize_refs(base, target)
        refs_changed = bool(normalized)
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
