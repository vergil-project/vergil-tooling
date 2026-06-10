from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.update_deps.context import UpdateDepsContext
from vergil_tooling.lib.update_deps.updater import UpdateResult, applicable_updaters


class _Yes:
    name = "yes"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        return UpdateResult(updater=self.name, changed=False, summary="", commit_message="")


class _No:
    name = "no"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return False

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        raise AssertionError("should not run")


def test_applicable_filters_by_applies() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    picked = applicable_updaters(ctx, registry=[_Yes(), _No()])
    assert [u.name for u in picked] == ["yes"]


def test_update_result_defaults() -> None:
    result = UpdateResult(updater="x", changed=True, summary="s", commit_message="m")
    assert result.warnings == []
