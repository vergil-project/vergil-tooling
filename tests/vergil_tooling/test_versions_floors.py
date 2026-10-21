"""Tests for the floor and toolchain-selection checks (epic vergil-project/.github#407).

:mod:`vergil_tooling.lib.versions.floors` only dispatches; the per-language rules
live in the extensions (``python.py``, ``go.py``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions import FLOOR_CHECKS, TOOLCHAIN_CHECKS, go
from vergil_tooling.lib.versions.floors import check_floors, check_toolchain_selection
from vergil_tooling.lib.versions.resolve import Resolution
from vergil_tooling.lib.versions.window import parse_window

if TYPE_CHECKING:
    from pathlib import Path


def _family(key: str, versions: list[str]) -> dict[str, Any]:
    return {
        "key": key,
        "token_prefix": "",
        "versions": [
            {"version": v, "eol": None, "in_policy": True, "runtime_published": None}
            for v in versions
        ],
    }


WINDOW = parse_window(
    json.dumps(
        {
            "schema": 1,
            "languages": {
                # Deliberately not newest-first: the oldest/newest come from the
                # versions, not their listed order.
                "python": {"families": [_family("python", ["3.13", "3.15", "3.12", "3.14"])]},
                "go": {"families": [_family("go", ["1.27", "1.26"])]},
                "rust": {"families": [_family("rust", ["1.93"])]},
            },
        }
    ).encode(),
    source="fixture-window",
)


def _resolution(language: str, version: str) -> Resolution:
    return Resolution(
        language=language,
        kind="app",
        versions=(version,),
        primary=version,
        container_tags={version: version},
        container_suffixes={version: language},
    )


def test_registries() -> None:
    assert set(FLOOR_CHECKS) == {"python", "go"}
    assert set(TOOLCHAIN_CHECKS) == {"python"}


def test_floor_below_oldest_warns(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')
    (warning,) = check_floors(tmp_path, _resolution("python", "3.15"), WINDOW)
    assert warning.startswith("requires-python floor 3.11 is below")
    assert "the oldest supported version 3.12" in warning


def test_upper_bound_excluding_newest_warns(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.14,<3.15"\n')
    (warning,) = check_floors(tmp_path, _resolution("python", "3.15"), WINDOW)
    assert warning.startswith("requires-python upper bound <3.15 excludes 3.15")


def test_floor_above_oldest_is_silent(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.14"\n')
    assert check_floors(tmp_path, _resolution("python", "3.15"), WINDOW) == []


def test_go_directive_floor_warns(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.25.3\n\ntoolchain go1.25.3\n")
    assert check_floors(tmp_path, _resolution("go", "1.27"), WINDOW) == [
        "go.mod go directive floor 1.25 is below the oldest supported version 1.26. "
        "Fix: raise it to go 1.26"
    ]


@pytest.mark.parametrize("directive", ["go 1.26", "go 1.27.1 // the newest", "go\t1.26.0"])
def test_go_directive_at_or_above_oldest_is_silent(tmp_path: Path, directive: str) -> None:
    (tmp_path / "go.mod").write_text(f"module example.com/x\n{directive}\n")
    assert go.check_floor(tmp_path, "1.26", "1.27") == []


def test_go_without_directive_or_go_mod_is_silent(tmp_path: Path) -> None:
    assert go.check_floor(tmp_path, "1.26", "1.27") == []
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    assert go.check_floor(tmp_path, "1.26", "1.27") == []


def test_go_malformed_directive_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\ngo latest\n")
    with pytest.raises(VersionsError, match="the go directive 'latest' is not a Go version"):
        go.check_floor(tmp_path, "1.26", "1.27")


def test_language_without_floor_check_is_silent(tmp_path: Path) -> None:
    assert check_floors(tmp_path, _resolution("rust", "1.93"), WINDOW) == []


def test_no_resolution_is_silent(tmp_path: Path) -> None:
    assert check_floors(tmp_path, None, WINDOW) == []


def test_toolchain_selection_dispatches(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.14\n")
    (error,) = check_toolchain_selection(tmp_path, "python")
    assert error.startswith(".python-version selects the Python interpreter")
    # Go has no toolchain-selection setting for Vergil to own.
    assert check_toolchain_selection(tmp_path, "go") == []
