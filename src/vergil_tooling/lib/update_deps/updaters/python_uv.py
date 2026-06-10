"""Python language-library updater: ``uv lock --upgrade`` in the dev container."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git, progress
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

_COMMIT_MESSAGE = "chore(deps): uv lock --upgrade"


class PythonUvUpdater:
    """Upgrade Python dependencies within their declared constraints."""

    name = "python"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        root = ctx.repo_root
        return (root / "pyproject.toml").is_file() and (root / "uv.lock").is_file()

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:  # noqa: ARG002
        progress.run(["vrg-container-run", "--", "uv", "lock", "--upgrade"])
        dirty = bool(git.read_output("status", "--porcelain", "uv.lock").strip())
        return UpdateResult(
            updater=self.name,
            changed=dirty,
            summary="uv lock --upgrade" if dirty else "uv lock --upgrade (no changes)",
            commit_message=_COMMIT_MESSAGE,
        )
