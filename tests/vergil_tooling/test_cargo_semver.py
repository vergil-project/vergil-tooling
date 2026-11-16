"""Tests for vergil_tooling.lib.cargo_semver.

Expected results follow Cargo's documented requirement rules
(https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html) and
the ``semver`` crate's evaluation, which Cargo uses.
"""

from __future__ import annotations

import pytest

from vergil_tooling.lib.cargo_semver import matches


@pytest.mark.parametrize(
    ("req", "version", "expected"),
    [
        # Caret, explicit and the bare default.
        ("^1.2", "1.2.0", True),
        ("^1.2", "1.9.9", True),
        ("^1.2", "1.1.9", False),
        ("^1.2", "2.0.0", False),
        ("1.2", "1.2.0", True),
        ("1.2", "1.99.0", True),
        ("1.2", "1.1.0", False),
        ("1.2", "2.0.0", False),
        ("^1.2.3", "1.2.3", True),
        ("^1.2.3", "1.2.2", False),
        ("^1.2.3", "1.3.0", True),
        ("^1.2.3", "1.2.4", True),
        ("^1", "1.99.99", True),
        ("^1", "2.0.0", False),
        ("^0.2", "0.2.9", True),
        ("^0.2", "0.3.0", False),
        ("^0.1.3", "0.1.3", True),
        ("^0.1.3", "0.1.4", True),
        ("^0.1.3", "0.1.2", False),
        ("^0.1.3", "0.2.0", False),
        ("0.1.3", "0.2.0", False),
        ("^0.0.3", "0.0.3", True),
        ("^0.0.3", "0.0.4", False),
        ("^0.0.3", "0.1.3", False),
        ("^0.0", "0.0.7", True),
        ("^0.0", "0.1.0", False),
        ("^0", "0.9.0", True),
        # Tilde.
        ("~1.2", "1.2.0", True),
        ("~1.2", "1.2.9", True),
        ("~1.2", "1.3.0", False),
        ("~1.2", "1.1.9", False),
        ("~1.2.3", "1.2.3", True),
        ("~1.2.3", "1.2.9", True),
        ("~1.2.3", "1.2.2", False),
        ("~1", "1.9.0", True),
        ("~1", "2.0.0", False),
        # Wildcards.
        ("1.*", "1.0.0", True),
        ("1.*", "1.99.0", True),
        ("1.*", "2.0.0", False),
        ("1.*", "0.9.9", False),
        ("1.2.*", "1.2.7", True),
        ("1.2.*", "1.3.0", False),
        ("1.x", "1.5.0", True),
        ("1.2.X", "1.2.0", True),
        ("*", "0.0.1", True),
        ("*", "99.0.0", True),
        (">=1.*", "1.0.0", True),
        (">=1.*", "0.9.0", False),
        # Comparison operators and comma-joined comparators.
        (">=1.2, <1.5", "1.2.0", True),
        (">=1.2, <1.5", "1.4.99", True),
        (">=1.2, <1.5", "1.1.9", False),
        (">=1.2, <1.5", "1.5.0", False),
        (">= 1.2 , < 1.5", "1.3.0", True),
        (">1.2.3", "1.2.4", True),
        (">1.2.3", "1.2.3", False),
        (">1.2.3", "1.2.2", False),
        (">1.2", "1.3.0", True),
        (">1.2", "1.2.9", False),
        (">1", "2.0.0", True),
        (">1", "1.9.0", False),
        (">1.2.3", "2.0.0", True),
        (">1.2.3", "0.9.0", False),
        (">1.2.3", "1.3.0", True),
        ("<1.2.3", "1.2.2", True),
        ("<1.2.3", "1.2.3", False),
        ("<1.2", "1.1.9", True),
        ("<1.2", "1.2.0", False),
        ("<1", "0.9.0", True),
        ("<1", "1.0.0", False),
        ("<1.2.3", "1.1.0", True),
        ("<1.2.3", "2.0.0", False),
        ("<=1.2.3", "1.2.3", True),
        ("<=1.2.3", "1.2.4", False),
        ("<=1.2.3", "1.2.2", True),
        (">=1.2.3", "1.2.3", True),
        (">=1.2.3", "1.2.2", False),
        # Exact.
        ("=1.2.3", "1.2.3", True),
        ("=1.2.3", "1.2.4", False),
        ("=1.2.3", "1.2.2", False),
        ("=1.2", "1.2.9", True),
        ("=1.2", "1.3.0", False),
        ("=1", "1.7.0", True),
        ("=1", "2.0.0", False),
        # Build metadata is ignored on both sides.
        ("=1.2.3+build.1", "1.2.3", True),
        ("^1.2.3", "1.2.3+build.5", True),
    ],
)
def test_cargo_semver_matches(req: str, version: str, expected: bool) -> None:  # noqa: FBT001
    assert matches(req, version) is expected


@pytest.mark.parametrize(
    ("req", "version", "expected"),
    [
        # A pre-release version matches only a comparator naming the same
        # major.minor.patch with a pre-release of its own.
        ("^1.2.3", "1.3.0-alpha.1", False),
        ("*", "1.0.0-alpha", False),
        ("^1.2.3-alpha.1", "1.2.3-alpha.2", True),
        ("^1.2.3-alpha.1", "1.2.3-alpha.1", True),
        ("^1.2.3-alpha.2", "1.2.3-alpha.1", False),
        ("^1.2.3-alpha.1", "1.2.3", True),
        ("^1.2.3-alpha.1", "1.2.4-alpha.1", False),
        ("^1.2.3-alpha", "1.2.3-beta", True),
        ("^1.2.3-beta", "1.2.3-alpha", False),
        ("^1.2.3-alpha", "1.2.3-alpha.1", True),
        ("^1.2.3-alpha.1", "1.2.3-alpha", False),
        ("^1.2.3-alpha.9", "1.2.3-alpha.10", True),
        ("^1.2.3-1", "1.2.3-alpha", True),
        ("^1.2.3-alpha", "1.2.3-1", False),
        ("^0.1.3-rc.1", "0.1.3-rc.2", True),
        ("^0.0.3-rc.1", "0.0.3-rc.2", True),
        ("~1.2.3-rc.1", "1.2.3-rc.2", True),
        ("~1.2.3-rc.2", "1.2.3-rc.1", False),
        ("=1.2.3-rc.1", "1.2.3-rc.1", True),
        ("=1.2.3-rc.1", "1.2.3", False),
        (">1.2.3-rc.1", "1.2.3-rc.2", True),
        (">1.2.3-rc.1", "1.2.3-rc.1", False),
        ("<1.2.3-rc.2", "1.2.3-rc.1", True),
        ("<1.2.3", "1.2.3-rc.1", False),
        (">=1.2.3-rc.1, <1.2.3", "1.2.3-rc.5", True),
        ("^1.2.3", "1.2.3-rc.1", False),
    ],
)
def test_cargo_semver_prerelease(req: str, version: str, expected: bool) -> None:  # noqa: FBT001
    assert matches(req, version) is expected


@pytest.mark.parametrize(
    "req",
    [
        "",
        "   ",
        "foo",
        "1.2.3.4",
        "^",
        ">=1.2,",
        ",1.2",
        "*, >=1.0",
        "1.*.3",
        "1.2.*-alpha",
        "01.2.3",
        "1.2.3-01",
        "v1.2.3",
        "1.2-alpha",
    ],
)
def test_cargo_semver_unparseable_requirement(req: str) -> None:
    with pytest.raises(ValueError, match="requirement|pre-release"):
        matches(req, "1.2.3")


@pytest.mark.parametrize("version", ["1.2", "1.2.3.4", "v1.2.3", "1.2.3-01", ""])
def test_cargo_semver_unparseable_version(version: str) -> None:
    with pytest.raises(ValueError, match="semver|pre-release"):
        matches("^1.2", version)
