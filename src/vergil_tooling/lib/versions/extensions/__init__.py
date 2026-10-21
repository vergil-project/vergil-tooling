"""Per-language resolver extensions (epic vergil-project/.github#407, spec §8.3).

The shared resolver has no language branches. A language that needs a rule of
its own registers an :data:`Extension` here, under its ``primary-language``
value, in a module of its own (``extensions/<lang>.py``). An extension narrows
the candidate versions; it never adds one. The registry starts empty.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

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


Extension = Callable[[ExtensionContext, tuple[str, ...]], tuple[str, ...]]

EXTENSIONS: dict[str, Extension] = {}
