"""Updater interface, result type, and registry for vrg-update-deps.

An updater upgrades one dependency category at its source of truth. It never
runs validation, commits, or touches git history — the driver owns those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

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
        ...

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        """Upgrade at the source of truth; report what changed."""
        ...


def applicable_updaters(
    ctx: UpdateDepsContext,
    *,
    registry: list[Updater],
) -> list[Updater]:
    """Return registry members whose ``applies`` is true for this repo."""
    return [u for u in registry if u.applies(ctx)]
