"""Tests for vergil_tooling.lib.version_divergence."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.version_divergence import (
    DivergenceResult,
    DivergenceStatus,
    compare_versions,
)


def test_diverged() -> None:
    result = compare_versions("1.2.0", "1.1.0")
    assert result == DivergenceResult(
        status=DivergenceStatus.DIVERGED,
        head_version="1.2.0",
        main_version="1.1.0",
    )


def test_equal() -> None:
    result = compare_versions("1.1.0", "1.1.0")
    assert result == DivergenceResult(
        status=DivergenceStatus.EQUAL,
        head_version="1.1.0",
        main_version="1.1.0",
    )


def test_first_release_none() -> None:
    result = compare_versions("0.1.0", None)
    assert result == DivergenceResult(
        status=DivergenceStatus.FIRST_RELEASE,
        head_version="0.1.0",
        main_version="",
    )


def test_first_release_empty() -> None:
    result = compare_versions("0.1.0", "")
    assert result == DivergenceResult(
        status=DivergenceStatus.FIRST_RELEASE,
        head_version="0.1.0",
        main_version="",
    )


@pytest.mark.parametrize(
    ("head", "main"),
    [
        ("2.0.0", "1.0.0"),
        ("1.0.1", "1.0.0"),
        ("1.1.0", "1.0.0"),
        ("0.2.0-rc1", "0.1.0"),
    ],
)
def test_diverged_parametrized(head: str, main: str) -> None:
    result = compare_versions(head, main)
    assert result.status == DivergenceStatus.DIVERGED


def test_status_values() -> None:
    assert DivergenceStatus.DIVERGED.value == "diverged"
    assert DivergenceStatus.FIRST_RELEASE.value == "first-release"
    assert DivergenceStatus.EQUAL.value == "equal"
