"""Repo-hook updater: run ``[dependency-update].hook`` in the dev container.

Spec §6.2 (epic vergil-project/.github#381). Last in the registry, so the hook
sees lockfiles the built-in updaters already refreshed. The hook may edit files
in the worktree; it must not commit or otherwise move HEAD. Held-back entries
are appended to ``$VRG_DEPS_REPORT``, a file inside the worktree (``.vergil/``
is gitignored fleet-wide) so the container sees it at ``/workspace/...``.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, progress
from vergil_tooling.lib.update_deps import report
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

_REPORT_REL = ".vergil/deps-report.jsonl"
_COMMIT_MESSAGE = "chore(deps): repo hook"


def _base(ctx: UpdateDepsContext) -> Path:
    return ctx.worktree_path if ctx.worktree_path is not None else ctx.repo_root


class RepoHookUpdater:
    """Run the repo-owned dependency-update hook (tier 2 of the spec §4 taxonomy)."""

    name = "hook"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        base = _base(ctx)
        if not (base / "vergil.toml").is_file():
            return False
        return config.dependency_update_hook(base) is not None

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        hook = config.dependency_update_hook(base)
        if hook is None:  # applies() gates this; re-checked so a mismatch fails loud
            raise UpdateDepsError(
                phase="update",
                command="hook",
                message="[dependency-update].hook is not set.",
            )
        # Start from an empty report so a stale file never leaks old entries.
        report_path = base / _REPORT_REL
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("")
        cmd = [
            "vrg-container-run",
            "--",
            "env",
            f"VRG_DEPS_REPORT=/workspace/{_REPORT_REL}",
            *shlex.split(hook),
        ]
        head_before = git.read_output("rev-parse", "HEAD")
        try:
            progress.run(cmd)
        except subprocess.CalledProcessError as exc:
            raise UpdateDepsError(
                phase="update",
                command=" ".join(cmd),
                message=f"Repository hook failed (exit {exc.returncode}): {hook}",
                detail=(exc.stderr or exc.output or ""),
            ) from exc
        head_after = git.read_output("rev-parse", "HEAD")
        if head_after != head_before:
            raise UpdateDepsError(
                phase="update",
                command=hook,
                message=(
                    f"Repository hook moved HEAD ({head_before[:8]} -> {head_after[:8]}). "
                    "Hooks may edit files but must not commit; the driver commits."
                ),
            )
        entries = report.parse_report_lines(report_path.read_text(), origin=hook)
        report_path.unlink()
        changed = bool(git.read_output("status", "--porcelain").strip())
        return UpdateResult(
            updater=self.name,
            changed=changed,
            summary=f"repo hook `{hook}`" + ("" if changed else " (no changes)"),
            commit_message=_COMMIT_MESSAGE,
            held_back=entries,
        )
