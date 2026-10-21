"""Per-language resolver extensions (epic vergil-project/.github#407, spec §8.3).

The shared resolver has no language branches. A language that needs a rule of
its own registers it here, under its ``primary-language`` value, from a module
of its own (``extensions/<lang>.py``). The hooks are:

* :data:`EXTENSIONS`: narrows the candidate versions; never adds one.
* :data:`FLOOR_CHECKS`: warns about the repo's own declared floor (spec §6.4).
* :data:`TOOLCHAIN_CHECKS`: reports settings that pick the toolchain behind
  Vergil's back (spec §6.5).
* :data:`CONTAINER_ENVS`: hands the resolved version to the language's own
  tooling inside the dev container (spec §6.5).

A language with no entry in a registry has no rule of that kind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.versions.extensions import go, python
from vergil_tooling.lib.versions.extensions.base import (
    ContainerEnv,
    Extension,
    ExtensionContext,
    FloorCheck,
    ResolutionKind,
    ToolchainCheck,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.versions.resolve import Resolution

__all__ = [
    "CONTAINER_ENVS",
    "EXTENSIONS",
    "FLOOR_CHECKS",
    "TOOLCHAIN_CHECKS",
    "ContainerEnv",
    "Extension",
    "ExtensionContext",
    "FloorCheck",
    "ResolutionKind",
    "ToolchainCheck",
    "container_env",
]

EXTENSIONS: dict[str, Extension] = {"python": python.python_extension}

FLOOR_CHECKS: dict[str, FloorCheck] = {
    "python": python.check_floor,
    "go": go.check_floor,
}

TOOLCHAIN_CHECKS: dict[str, ToolchainCheck] = {"python": python.check_toolchain}

CONTAINER_ENVS: dict[str, ContainerEnv] = {"python": python.container_env}


def container_env(resolution: Resolution | None) -> dict[str, str]:
    """The environment that hands *resolution*'s primary version to its tooling.

    Empty for a repo with no language, or a language with no such hook.
    """
    if resolution is None:
        return {}
    hook = CONTAINER_ENVS.get(resolution.language)
    return dict(hook(resolution.primary)) if hook is not None else {}
