from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.updater import (
    UpdateResult,
    applicable_updaters,
    select_updaters,
)


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


_REGISTRY = [_Yes(), _No()]  # names: "yes", "no"


def test_select_default_returns_all() -> None:
    assert [u.name for u in select_updaters(_REGISTRY)] == ["yes", "no"]


def test_select_only_keeps_named_in_registry_order() -> None:
    assert [u.name for u in select_updaters(_REGISTRY, only=["no"])] == ["no"]


def test_select_skip_excludes_named() -> None:
    assert [u.name for u in select_updaters(_REGISTRY, skip=["yes"])] == ["no"]


def test_select_unknown_name_raises() -> None:
    with pytest.raises(UpdateDepsError, match="unknown updater"):
        select_updaters(_REGISTRY, only=["nope"])


def test_select_only_and_skip_together_raises() -> None:
    with pytest.raises(UpdateDepsError, match="mutually exclusive"):
        select_updaters(_REGISTRY, only=["yes"], skip=["no"])
