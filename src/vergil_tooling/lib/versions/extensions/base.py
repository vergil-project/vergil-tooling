"""The types every language extension is written against.

They live apart from the registry in :mod:`.` so an extension module can import
them while the registry imports the extension modules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from vergil_tooling.lib.config import VergilConfig
    from vergil_tooling.lib.versions.window import Window

ResolutionKind = Literal["library", "app"]


@dataclass(frozen=True)
class ExtensionContext:
    cfg: VergilConfig
    window: Window
    language: str
    kind: ResolutionKind
    # True when the candidates are the repo's [ci].versions pin, False when they
    # are the window.
    pinned: bool
    # The repo checkout, or None when the config was read from a remote ref.
    repo_root: Path | None


# Narrows the candidate versions (newest first); never adds one.
Extension = Callable[[ExtensionContext, tuple[str, ...]], tuple[str, ...]]

# (repo root, the window's oldest version, its newest version) -> warnings about
# the repo's own declared language floor (spec §6.4).
FloorCheck = Callable[[Path, str, str], list[str]]

# repo root -> errors, one per setting that selects the toolchain behind
# Vergil's back (spec §6.5).
ToolchainCheck = Callable[[Path], list[str]]

# The resolved primary version -> the environment that hands it to the
# language's own tooling inside the dev container (spec §6.5, requirement 1).
ContainerEnv = Callable[[str], Mapping[str, str]]
