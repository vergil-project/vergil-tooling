"""Tests for vergil_tooling.lib.host_versions (normalize + the single comparator)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import host_versions as hv

# -- normalize ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.40.0", "2.40.0"),
        ("v2.40.0", "2.40.0"),
        ("  2.40.0\n", "2.40.0"),
        ("15", "15"),
        ("3.12.7_1", "3.12.7_1"),
        ("2024.10.04", "2024.10.04"),
        ("gh version 2.40.0", "2.40.0"),
        (
            "gh version 2.40.0 (2023-12-07)\nhttps://github.com/cli/cli/releases/tag/v2.40.0",
            "2.40.0",
        ),
        ("Docker version 27.3.1, build ce12230", "27.3.1"),
        ("uv 0.9.5 (d5f39331a 2025-10-21)", "0.9.5"),
        ("Homebrew 4.4.2", "4.4.2"),
        ("4.34.2,167172", "4.34.2"),
        ("tool v1.2.3 is installed", "1.2.3"),
        ("1.2.3 and again 1.2.3", "1.2.3"),
    ],
)
def test_normalize_extracts_bare_version(raw: str, expected: str) -> None:
    assert hv.normalize(raw) == expected


def test_gh_version_prose_normalizes_equal_to_bare() -> None:
    """Spec §8: "gh version 2.40.0" and "2.40.0" compare equal (no false behind)."""
    assert hv.compare(hv.normalize("gh version 2.40.0"), hv.normalize("2.40.0")) == 0


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("", "no version"),
        ("nightly", "no version"),
        ("built 2025-10-21", "no version"),
        ("Google Cloud SDK 496.0.0\nbq 2.1.9\ncore 2024.10.04", "several versions"),
        ("2.0-beta", "no version"),
    ],
)
def test_normalize_refuses_ambiguous_or_absent(raw: str, match: str) -> None:
    with pytest.raises(hv.UnrecognizedVersionError, match=match):
        hv.normalize(raw)


def test_unrecognized_is_value_error() -> None:
    assert issubclass(hv.UnrecognizedVersionError, ValueError)
    assert issubclass(hv.IncomparableVersionError, ValueError)


# -- compare -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("2.0", "2.0.0", 0),
        ("v2.1", "2.0", 1),
        ("1.9.9", "2.0", -1),
        ("2.10", "2.9", 1),
        ("2024.10.04", "2024.10.4", 0),
        ("2024.10.18", "2024.10.04", 1),
        ("3.12.7_1", "3.12.7", 1),
        ("3.12.7", "3.12.7_1", -1),
        ("3.12.7_1", "3.12.7_1", 0),
        ("3.12.8", "3.12.7_2", 1),
    ],
)
def test_compare(left: str, right: str, expected: int) -> None:
    assert hv.compare(left, right) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [("2.0-beta", "2.0"), ("2.0", "4.34.2,167172"), ("", "1"), ("nightly", "1.0")],
)
def test_compare_refuses_non_numeric(left: str, right: str) -> None:
    with pytest.raises(hv.IncomparableVersionError, match="cannot compare version"):
        hv.compare(left, right)


# -- upstream ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [("3.12.7_1", "3.12.7"), ("3.12.7", "3.12.7"), ("nightly", "nightly")],
)
def test_upstream_drops_brew_revision(version: str, expected: str) -> None:
    assert hv.upstream(version) == expected


# -- parse_bare and update_level (#3269) ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("26.7.1", "26.7.1"), ("v27.0.1", "27.0.1"), (" 15 ", "15"), ("3.12.7_1", "3.12.7_1")],
)
def test_parse_bare(raw: str, expected: str) -> None:
    assert hv.parse_bare(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "macOS 26.7.1", "26.7.1 (a)", "2.0-beta", "4.94.0,1"])
def test_parse_bare_refuses_anything_but_a_bare_version(raw: str) -> None:
    with pytest.raises(hv.UnrecognizedVersionError, match="is not a bare version"):
        hv.parse_bare(raw)


@pytest.mark.parametrize(
    ("installed", "offered", "expected"),
    [
        ("26.5.2", "26.5.3", "patch"),
        ("26.5.2", "26.7.1", "minor"),
        ("26.5.2", "27.0.1", "major"),
        ("26.5.2", "27.0", "major"),
        ("26", "26.0.1", "patch"),  # missing components are 0
        ("26.5.2", "26.5.2.1", "patch"),  # a fourth component is below minor
        ("3.12.7", "3.12.7_1", "patch"),  # a revision-only rebuild
        ("26.5.2", "26.5.2", None),  # equal is not an update
        ("26.5.2", "26.5.0", None),  # older is not an update
        ("26.5.2", "25.9.9", None),
    ],
)
def test_update_level(installed: str, offered: str, expected: str | None) -> None:
    assert hv.update_level(installed, offered) == expected


def test_update_level_incomparable_raises() -> None:
    with pytest.raises(hv.IncomparableVersionError):
        hv.update_level("26.5.2", "27.0-beta")


def test_levels_are_least_to_most_significant() -> None:
    assert hv.LEVELS == ("patch", "minor", "major")
