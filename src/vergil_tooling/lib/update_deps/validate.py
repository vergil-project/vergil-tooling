"""Single-shot validation stage for vrg-update-deps."""

from __future__ import annotations

import subprocess

from vergil_tooling.lib import progress
from vergil_tooling.lib.update_deps.context import UpdateDepsError

_VALIDATE_CMD = ["vrg-container-run", "--", "vrg-validate"]


def run_validation() -> None:
    """Run the canonical validation command in the cwd (the worktree); raise on failure."""
    try:
        progress.run(_VALIDATE_CMD)
    except subprocess.CalledProcessError as exc:
        raise UpdateDepsError(
            phase="validate",
            command=" ".join(_VALIDATE_CMD),
            message="Validation failed after dependency updates.",
            detail=(exc.stderr or exc.output or ""),
        ) from exc
