"""Updater interface, result type, and registry for vrg-update-deps.

An updater upgrades one dependency category at its source of truth. It never
runs validation, commits, or touches git history — the driver owns those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext


@dataclass
class UpdateResult:
    """What an updater changed (or didn't) in one run."""

    updater: str
    changed: bool
    summary: str
    commit_message: str
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class Updater(Protocol):
    """One dependency-category updater."""

    name: str

    def applies(self, ctx: UpdateDepsContext) -> bool:
        """True when this repo has the surface this updater handles."""
        ...  # pragma: no cover

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        """Upgrade at the source of truth; report what changed."""
        ...  # pragma: no cover


def applicable_updaters(
    ctx: UpdateDepsContext,
    *,
    registry: list[Updater],
) -> list[Updater]:
    """Return registry members whose ``applies`` is true for this repo."""
    return [u for u in registry if u.applies(ctx)]


def select_updaters(
    registry: list[Updater],
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
) -> list[Updater]:
    """Filter the registry by name via ``only`` / ``skip`` (registry order kept).

    ``only`` and ``skip`` are mutually exclusive; an unknown name fails loud.
    Neither set returns the full registry. Applicability (``applies``) is a
    separate, later filter — this is purely the by-name selection.
    """
    if only is not None and skip is not None:
        msg = "--only and --skip are mutually exclusive."
        raise UpdateDepsError(phase="select", command="select_updaters", message=msg)

    known = {u.name for u in registry}
    for name in (only or []) + (skip or []):
        if name not in known:
            valid = ", ".join(sorted(known))
            msg = f"unknown updater '{name}'. Valid updaters: {valid}."
            raise UpdateDepsError(phase="select", command="select_updaters", message=msg)

    if only is not None:
        wanted = set(only)
        return [u for u in registry if u.name in wanted]
    if skip is not None:
        unwanted = set(skip)
        return [u for u in registry if u.name not in unwanted]
    return list(registry)
