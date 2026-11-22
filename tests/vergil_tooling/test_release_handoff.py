"""The hold rule (spec §4.4 of epic vergil-project/.github#1440)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.release.handoff import THROUGH_CONFIRM_DEVELOP, holds
from vergil_tooling.lib.release.orchestrator import build_stages

_EVERY_FAILURE = [
    "confirm-develop",
    "promote",
    "cd-publish",
    "package-index",
    "finalize",
    "update-deps",
    "install",
]


def test_through_confirm_develop_is_the_head_of_the_stage_list() -> None:
    names = [stage.name for stage in build_stages()]
    assert tuple(names[: names.index("confirm-develop") + 1]) == THROUGH_CONFIRM_DEVELOP


@pytest.mark.parametrize("stage", ["promote", "finalize"])
def test_promote_and_finalize_need_only_the_stages_through_confirm_develop(stage: str) -> None:
    assert holds(stage, _EVERY_FAILURE) == ["confirm-develop"]
    assert holds(stage, ["cd-publish", "package-index", "update-deps"]) == []


def test_install_needs_those_and_promote() -> None:
    assert holds("install", _EVERY_FAILURE) == ["confirm-develop", "promote"]
    assert holds("install", ["promote"]) == ["promote"]
    assert holds("install", ["cd-publish", "package-index", "finalize", "update-deps"]) == []


@pytest.mark.parametrize("stage", ["release-records", "close-tracking"])
def test_records_and_close_need_every_stage_before_them(stage: str) -> None:
    assert holds(stage, _EVERY_FAILURE) == _EVERY_FAILURE
    assert holds(stage, ["update-deps"]) == ["update-deps"]
    assert holds(stage, []) == []


@pytest.mark.parametrize("stage", ["cd-publish", "package-index", "update-deps", "prepare"])
def test_nothing_holds_the_other_stages_back(stage: str) -> None:
    assert holds(stage, _EVERY_FAILURE) == []
