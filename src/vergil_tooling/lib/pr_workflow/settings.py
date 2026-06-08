"""Per-repo settings for the PR workflow oracle, read from vergil.toml.

A small dedicated reader: the structured VergilConfig dataclass does not model a
[pr-workflow] stanza, and this keeps the dependency one-way (the oracle reads its
own optional knobs). Falls back to the default when the file or key is absent.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from vergil_tooling.lib.pr_workflow.errors import WorkflowError

if TYPE_CHECKING:
    from pathlib import Path

_DEFAULT_MAX_ROUNDS = 10


def max_rounds(worktree_root: Path) -> int:
    """Return ``[pr-workflow].max-rounds`` from vergil.toml, or the default (10)."""
    path = worktree_root / "vergil.toml"
    if not path.is_file():
        return _DEFAULT_MAX_ROUNDS
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    value = data.get("pr-workflow", {}).get("max-rounds", _DEFAULT_MAX_ROUNDS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkflowError(f"[pr-workflow].max-rounds must be a positive integer, got {value!r}")
    return value
