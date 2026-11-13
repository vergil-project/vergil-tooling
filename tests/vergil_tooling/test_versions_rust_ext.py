"""Tests for the Rust resolver extension (epic vergil-project/.github#1095, spec §6.4).

The floor is the root ``Cargo.toml``'s ``rust-version``; the toolchain files are
``rust-toolchain.toml`` and the legacy ``rust-toolchain``. Every comparison is
numeric, so ``1.100`` sits above ``1.99``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions import CONTAINER_ENVS
from vergil_tooling.lib.versions.extensions import rust as rust_ext
from vergil_tooling.lib.versions.floors import check_floors, check_toolchain_selection
from vergil_tooling.lib.versions.resolve import Resolution
from vergil_tooling.lib.versions.window import parse_window

if TYPE_CHECKING:
    from pathlib import Path


def _cargo(tmp_path: Path, text: str) -> None:
    (tmp_path / "Cargo.toml").write_text(text)


def _warning(floor: str, oldest: str) -> str:
    return (
        f"Cargo.toml rust-version floor {floor} is below the oldest supported version "
        f"{oldest}. Fix: raise it to {oldest}"
    )


# --- floor: [package].rust-version -------------------------------------------


def test_package_floor_below_oldest_warns(tmp_path: Path) -> None:
    _cargo(tmp_path, '[package]\nname = "x"\nrust-version = "1.85"\n')
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == [_warning("1.85", "1.99")]


def test_patch_floor_is_compared_on_x_y(tmp_path: Path) -> None:
    _cargo(tmp_path, '[package]\nname = "x"\nrust-version = "1.85.0"\n')
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == [_warning("1.85", "1.99")]


@pytest.mark.parametrize("floor", ["1.99", "1.99.1", "1.100", "1.100.0"])
def test_floor_at_or_above_oldest_is_silent(tmp_path: Path, floor: str) -> None:
    _cargo(tmp_path, f'[package]\nname = "x"\nrust-version = "{floor}"\n')
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == []


def test_floor_below_three_digit_oldest_warns(tmp_path: Path) -> None:
    _cargo(tmp_path, '[package]\nname = "x"\nrust-version = "1.99"\n')
    assert rust_ext.check_floor(tmp_path, "1.100", "1.101") == [_warning("1.99", "1.100")]


# --- floor: [workspace.package].rust-version ----------------------------------


def test_inherited_floor_reads_workspace_package(tmp_path: Path) -> None:
    _cargo(
        tmp_path,
        '[workspace]\nmembers = ["a"]\n\n[workspace.package]\nrust-version = "1.85"\n\n'
        '[package]\nname = "x"\nrust-version.workspace = true\n',
    )
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == [_warning("1.85", "1.99")]


def test_inherited_floor_at_oldest_is_silent(tmp_path: Path) -> None:
    _cargo(
        tmp_path,
        '[workspace.package]\nrust-version = "1.100"\n\n'
        '[package]\nname = "x"\nrust-version = { workspace = true }\n',
    )
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == []


def test_virtual_workspace_reads_workspace_package(tmp_path: Path) -> None:
    _cargo(
        tmp_path, '[workspace]\nmembers = ["a"]\n\n[workspace.package]\nrust-version = "1.85.0"\n'
    )
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == [_warning("1.85", "1.99")]


def test_package_string_wins_over_workspace_package(tmp_path: Path) -> None:
    _cargo(
        tmp_path,
        '[workspace.package]\nrust-version = "1.85"\n\n'
        '[package]\nname = "x"\nrust-version = "1.99"\n',
    )
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == []


def test_package_without_floor_does_not_read_workspace(tmp_path: Path) -> None:
    # A [package] with no rust-version declares no floor; cargo does not inherit
    # [workspace.package] keys implicitly.
    _cargo(tmp_path, '[workspace.package]\nrust-version = "1.85"\n\n[package]\nname = "x"\n')
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == []


@pytest.mark.parametrize(
    "text",
    [
        '[package]\nname = "x"\n',
        '[workspace]\nmembers = ["a"]\n',
        '[workspace]\nmembers = ["a"]\n\n[workspace.package]\nedition = "2024"\n',
    ],
)
def test_no_floor_anywhere_is_silent(tmp_path: Path, text: str) -> None:
    _cargo(tmp_path, text)
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == []


def test_no_cargo_toml_is_silent(tmp_path: Path) -> None:
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == []


def test_workspace_members_are_not_checked(tmp_path: Path) -> None:
    _cargo(tmp_path, '[workspace]\nmembers = ["a"]\n')
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "Cargo.toml").write_text('[package]\nname = "a"\nrust-version = "1.85"\n')
    assert rust_ext.check_floor(tmp_path, "1.99", "1.100") == []


# --- floor: fatal shapes ------------------------------------------------------


def test_invalid_toml_is_fatal(tmp_path: Path) -> None:
    _cargo(tmp_path, "[package\n")
    with pytest.raises(VersionsError, match="is not valid TOML"):
        rust_ext.check_floor(tmp_path, "1.99", "1.100")


@pytest.mark.parametrize(
    ("value", "shown"),
    [("185", "185"), ('"one"', "'one'"), ('"1"', "'1'"), ('"1.85.0.1"', "'1.85.0.1'")],
)
def test_malformed_rust_version_is_fatal(tmp_path: Path, value: str, shown: str) -> None:
    _cargo(tmp_path, f'[package]\nname = "x"\nrust-version = {value}\n')
    with pytest.raises(
        VersionsError,
        match=rf"\[package\]\.rust-version {shown} is not an x\.y or x\.y\.z Rust version",
    ):
        rust_ext.check_floor(tmp_path, "1.99", "1.100")


def test_malformed_workspace_rust_version_is_fatal(tmp_path: Path) -> None:
    _cargo(
        tmp_path,
        '[workspace.package]\nrust-version = 185\n\n'
        '[package]\nname = "x"\nrust-version.workspace = true\n',
    )
    with pytest.raises(VersionsError, match=r"\[workspace\.package\]\.rust-version 185 is not"):
        rust_ext.check_floor(tmp_path, "1.99", "1.100")


def test_inherited_floor_without_workspace_value_is_fatal(tmp_path: Path) -> None:
    _cargo(tmp_path, '[package]\nname = "x"\nrust-version.workspace = true\n')
    with pytest.raises(
        VersionsError, match=r"\[workspace\.package\]\.rust-version is not set"
    ):
        rust_ext.check_floor(tmp_path, "1.99", "1.100")


def test_other_table_rust_version_is_fatal(tmp_path: Path) -> None:
    _cargo(tmp_path, '[package]\nname = "x"\nrust-version.workspace = false\n')
    with pytest.raises(VersionsError, match=r"neither a version nor \{ workspace = true \}"):
        rust_ext.check_floor(tmp_path, "1.99", "1.100")


def test_non_table_package_is_fatal(tmp_path: Path) -> None:
    _cargo(tmp_path, 'package = "x"\n')
    with pytest.raises(VersionsError, match=r"\[package\] is not a table"):
        rust_ext.check_floor(tmp_path, "1.99", "1.100")


# --- toolchain files ----------------------------------------------------------


def _toolchain_error(name: str) -> str:
    return (
        f"{name} selects the Rust toolchain, which belongs to Vergil (the Vergil Rust "
        f"image pins it). Fix: delete {name}"
    )


@pytest.mark.parametrize("name", ["rust-toolchain.toml", "rust-toolchain"])
def test_each_toolchain_file_alone_errors(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text('[toolchain]\nchannel = "1.99"\n')
    assert rust_ext.check_toolchain(tmp_path) == [_toolchain_error(name)]


def test_both_toolchain_files_give_two_errors(tmp_path: Path) -> None:
    (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.99"\n')
    (tmp_path / "rust-toolchain").write_text("1.99\n")
    assert rust_ext.check_toolchain(tmp_path) == [
        _toolchain_error("rust-toolchain.toml"),
        _toolchain_error("rust-toolchain"),
    ]


def test_no_toolchain_file_gives_none(tmp_path: Path) -> None:
    assert rust_ext.check_toolchain(tmp_path) == []


# --- dispatch -----------------------------------------------------------------


_WINDOW = parse_window(
    json.dumps(
        {
            "schema": 1,
            "languages": {
                "rust": {
                    "families": [
                        {
                            "key": "rust",
                            "token_prefix": "",
                            "versions": [
                                {
                                    "version": v,
                                    "eol": None,
                                    "in_policy": True,
                                    "runtime_published": None,
                                }
                                for v in ["1.100", "1.99"]
                            ],
                        }
                    ]
                }
            },
        }
    ).encode(),
    source="fixture-window",
)


def test_floor_dispatches_with_numeric_window_order(tmp_path: Path) -> None:
    # The window's oldest is 1.99, not 1.100: a string sort would get this wrong.
    resolution = Resolution(
        language="rust",
        kind="library",
        versions=("1.100", "1.99"),
        primary="1.100",
        container_tags={"1.100": "1.100", "1.99": "1.99"},
        container_suffixes={"1.100": "rust", "1.99": "rust"},
    )
    _cargo(tmp_path, '[package]\nname = "x"\nrust-version = "1.99"\n')
    assert check_floors(tmp_path, resolution, _WINDOW) == []
    _cargo(tmp_path, '[package]\nname = "x"\nrust-version = "1.98"\n')
    assert check_floors(tmp_path, resolution, _WINDOW) == [_warning("1.98", "1.99")]


def test_toolchain_selection_dispatches(tmp_path: Path) -> None:
    (tmp_path / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.99"\n')
    assert check_toolchain_selection(tmp_path, "rust") == [
        _toolchain_error("rust-toolchain.toml")
    ]


def test_no_container_env_hook() -> None:
    # The Vergil Rust image pins its own toolchain (spec §6.3).
    assert "rust" not in CONTAINER_ENVS
