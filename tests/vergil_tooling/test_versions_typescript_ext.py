"""Tests for the TypeScript resolver extension (epic vergil-project/.github#1281, spec §6.2).

Every setting that names a Node or package-manager version is an error: the
Node runtime and npm belong to Vergil. ``engines.node`` and ``devEngines``'
``os``, ``cpu`` and ``libc`` entries are allowed.

``engines.node`` is a range: :func:`check_floor` warns when it admits a major
below the window's oldest, or no release of its newest. The window in these
tests is 26/24/22.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.container import PackageJsonError, read_package_json
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions import FLOOR_CHECKS, TOOLCHAIN_CHECKS
from vergil_tooling.lib.versions.extensions import typescript as ts_ext
from vergil_tooling.lib.versions.floors import check_floors, check_toolchain_selection
from vergil_tooling.lib.versions.resolve import Resolution
from vergil_tooling.lib.versions.window import parse_window

if TYPE_CHECKING:
    from pathlib import Path


def _package(tmp_path: Path, data: object) -> None:
    (tmp_path / "package.json").write_text(json.dumps(data), encoding="utf-8")


# (setting, how to write it into the repo, the fix the error ends with)
_SETTINGS: list[tuple[str, object, str]] = [
    (".nvmrc", ".nvmrc", "Fix: delete .nvmrc"),
    (".node-version", ".node-version", "Fix: delete .node-version"),
    ("packageManager", {"packageManager": "pnpm@9.0.0"}, 'Fix: remove the "packageManager" field'),
    ("volta", {"volta": {"node": "24.1.0"}}, 'Fix: remove the "volta" field'),
    ("engines.npm", {"engines": {"npm": ">=10"}}, "Fix: remove engines.npm"),
    ("engines.pnpm", {"engines": {"pnpm": ">=9"}}, "Fix: remove engines.pnpm"),
    ("engines.yarn", {"engines": {"yarn": ">=4"}}, "Fix: remove engines.yarn"),
    (
        "devEngines.runtime",
        {"devEngines": {"runtime": {"name": "node", "version": "24"}}},
        "Fix: remove devEngines.runtime",
    ),
    (
        "devEngines.packageManager",
        {"devEngines": {"packageManager": {"name": "npm", "version": "11"}}},
        "Fix: remove devEngines.packageManager",
    ),
]


@pytest.mark.parametrize(("setting", "content", "fix"), _SETTINGS, ids=[s[0] for s in _SETTINGS])
def test_each_setting_is_one_error(tmp_path: Path, setting: str, content: object, fix: str) -> None:
    if isinstance(content, str):
        (tmp_path / content).write_text("24\n", encoding="utf-8")
    else:
        _package(tmp_path, content)
    (error,) = ts_ext.check_toolchain(tmp_path)
    assert error.startswith(setting if isinstance(content, str) else "package.json ")
    assert setting in error
    assert "belong" in error
    assert "Vergil" in error
    assert error.endswith(fix)


def test_every_setting_reported_in_order(tmp_path: Path) -> None:
    (tmp_path / ".nvmrc").write_text("24\n", encoding="utf-8")
    (tmp_path / ".node-version").write_text("24\n", encoding="utf-8")
    _package(
        tmp_path,
        {
            "devEngines": {"packageManager": {"name": "npm"}, "runtime": {"name": "node"}},
            "engines": {"yarn": "4", "pnpm": "9", "npm": "11", "node": ">=22"},
            "volta": {"node": "24"},
            "packageManager": "npm@11.6.0",
        },
    )
    fixes = [error.rsplit("Fix: ", 1)[1] for error in ts_ext.check_toolchain(tmp_path)]
    assert fixes == [
        "delete .nvmrc",
        "delete .node-version",
        'remove the "packageManager" field',
        'remove the "volta" field',
        "remove engines.npm",
        "remove engines.pnpm",
        "remove engines.yarn",
        "remove devEngines.runtime",
        "remove devEngines.packageManager",
    ]


def test_package_manager_naming_npm_is_an_error(tmp_path: Path) -> None:
    _package(tmp_path, {"packageManager": "npm@11.6.0"})
    (error,) = ts_ext.check_toolchain(tmp_path)
    assert error.endswith('Fix: remove the "packageManager" field')


@pytest.mark.parametrize("entry", ["os", "cpu", "libc"])
@pytest.mark.parametrize("form", ["object", "array"])
def test_dev_engines_allowed_entries(tmp_path: Path, entry: str, form: str) -> None:
    # Review Focus 3: os, cpu and libc name no version Vergil owns.
    value: object = {"name": "linux"}
    _package(tmp_path, {"devEngines": {entry: value if form == "object" else [value]}})
    assert ts_ext.check_toolchain(tmp_path) == []


def test_dev_engines_runtime_array_form(tmp_path: Path) -> None:
    _package(tmp_path, {"devEngines": {"runtime": [{"name": "node", "version": "24"}]}})
    (error,) = ts_ext.check_toolchain(tmp_path)
    assert error.endswith("Fix: remove devEngines.runtime")


def test_engines_node_alone_is_not_an_error(tmp_path: Path) -> None:
    _package(tmp_path, {"engines": {"node": ">=24"}})
    assert ts_ext.check_toolchain(tmp_path) == []


@pytest.mark.parametrize(
    ("data", "field"),
    [
        ({"engines": "x"}, "engines"),
        ({"engines": None}, "engines"),
        ({"devEngines": 1}, "devEngines"),
        ({"devEngines": None}, "devEngines"),
        ({"devEngines": {"runtime": None}}, "devEngines.runtime"),
        ({"devEngines": {"runtime": [1]}}, "devEngines.runtime"),
        ({"devEngines": {"os": "linux"}}, "devEngines.os"),
    ],
)
def test_malformed_fields_are_versions_errors(tmp_path: Path, data: object, field: str) -> None:
    _package(tmp_path, data)
    with pytest.raises(VersionsError, match=rf"package\.json: {field} .*Fix: \w+ {field}"):
        ts_ext.check_toolchain(tmp_path)


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (None, "null"),
        (True, "boolean"),
        (1, "number"),
        (1.5, "number"),
        ("x", "string"),
        ([], "array"),
        ({}, "object"),
    ],
)
def test_json_type_names(value: object, name: str) -> None:
    assert ts_ext._json_type(value) == name


def test_malformed_field_names_its_json_type(tmp_path: Path) -> None:
    _package(tmp_path, {"engines": None})
    with pytest.raises(VersionsError, match=r"engines is a JSON null, not an object"):
        ts_ext.check_toolchain(tmp_path)


@pytest.mark.parametrize("text", ["[]", "{"])
def test_malformed_package_json_is_versions_error(tmp_path: Path, text: str) -> None:
    path = tmp_path / "package.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PackageJsonError) as expected:
        read_package_json(path)
    with pytest.raises(VersionsError) as excinfo:
        ts_ext.check_toolchain(tmp_path)
    assert not isinstance(excinfo.value, PackageJsonError)
    assert str(excinfo.value) == str(expected.value)


def test_no_package_json_no_files(tmp_path: Path) -> None:
    assert ts_ext.check_toolchain(tmp_path) == []


def test_package_json_returns_the_object_or_none(tmp_path: Path) -> None:
    assert ts_ext._package_json(tmp_path) is None
    _package(tmp_path, {"name": "x"})
    assert ts_ext._package_json(tmp_path) == {"name": "x"}


def test_registered_for_typescript(tmp_path: Path) -> None:
    assert TOOLCHAIN_CHECKS["typescript"] is ts_ext.check_toolchain
    (tmp_path / ".nvmrc").write_text("24\n", encoding="utf-8")
    (error,) = check_toolchain_selection(tmp_path, "typescript")
    assert error.endswith("Fix: delete .nvmrc")


# ---------------------------------------------------------------------------
# check_floor: the two-sided engines.node warning (spec §6.2)
# ---------------------------------------------------------------------------

OLDEST = "22"
NEWEST = "26"


def _floor(range_text: str, major: int) -> str:
    return (
        f"engines.node {range_text} admits Node {major}, below the oldest supported "
        f"major {OLDEST}. Fix: raise it to >={OLDEST}"
    )


def _upper(range_text: str) -> str:
    return (
        f"engines.node {range_text} excludes Node {NEWEST}. Fix: drop the upper bound, "
        f"or widen it to admit {NEWEST}"
    )


def _check(tmp_path: Path, node: object) -> list[str]:
    _package(tmp_path, {"engines": {"node": node}})
    return ts_ext.check_floor(tmp_path, OLDEST, NEWEST)


# (range, the lowest major it admits below 22 or None, whether it excludes 26)
_FLOOR_TABLE: list[tuple[str, int | None, bool]] = [
    (">=24", None, False),  # Review Focus 1: vergil-fixture-ts-app's real range
    (">=22", None, False),
    (">=20", 20, False),
    ("^22 || ^24", None, True),  # Review Focus 2: the upper bound only
    ("^20 || ^24", 20, True),
    ("~24.1", None, True),
    ("22.x", None, True),
    ("20 - 26", 20, False),
    (">=22.12.0 <27", None, False),
    ("*", 0, False),
    ("<27", 0, False),
    (">=24 <26", None, True),
]


@pytest.mark.parametrize(("range_text", "floor", "excludes"), _FLOOR_TABLE)
def test_floor_and_upper_bound(
    tmp_path: Path, range_text: str, floor: int | None, excludes: bool
) -> None:
    expected = ([_floor(range_text, floor)] if floor is not None else []) + (
        [_upper(range_text)] if excludes else []
    )
    assert _check(tmp_path, range_text) == expected


@pytest.mark.parametrize(
    "data", [{}, {"name": "x"}, {"engines": {}}, {"engines": {"os": ["linux"]}}]
)
def test_no_engines_node_is_silent(tmp_path: Path, data: object) -> None:
    _package(tmp_path, data)
    assert ts_ext.check_floor(tmp_path, OLDEST, NEWEST) == []


def test_no_package_json_has_no_floor(tmp_path: Path) -> None:
    assert ts_ext.check_floor(tmp_path, OLDEST, NEWEST) == []


@pytest.mark.parametrize("range_text", [">=26 <24", "<0.0.0", ">x", "=26.0.0-rc.1"])
def test_range_admitting_nothing_is_an_error(tmp_path: Path, range_text: str) -> None:
    with pytest.raises(VersionsError, match=r"admits no Node release") as excinfo:
        _check(tmp_path, range_text)
    assert f"engines.node {range_text} admits no Node release" in str(excinfo.value)


@pytest.mark.parametrize(
    "range_text",
    [
        "latest",
        "24 ||| 26",
        ">=24.1.2.3",
        "01.2.3",
        ">=22<27",
        "24 -26",
        "v 24",
        ">=24 foo",
        ">=9007199254740992",  # above Number.MAX_SAFE_INTEGER
    ],
)
def test_invalid_range_is_an_error(tmp_path: Path, range_text: str) -> None:
    with pytest.raises(VersionsError, match=r"is not an npm semver range") as excinfo:
        _check(tmp_path, range_text)
    assert f"engines.node {json.dumps(range_text)} is not" in str(excinfo.value)


@pytest.mark.parametrize(("value", "shown"), [(24, "24"), (None, "null"), ([">=24"], '[">=24"]')])
def test_engines_node_not_a_string_is_an_error(tmp_path: Path, value: object, shown: str) -> None:
    with pytest.raises(VersionsError, match=r"engines\.node is a JSON \w+") as excinfo:
        _check(tmp_path, value)
    assert shown in str(excinfo.value)


def test_engines_not_an_object_is_an_error(tmp_path: Path) -> None:
    _package(tmp_path, {"engines": ">=24"})
    with pytest.raises(VersionsError, match=r"engines is a JSON string, not an object"):
        ts_ext.check_floor(tmp_path, OLDEST, NEWEST)


def test_malformed_package_json_fails_the_floor_check(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("[]", encoding="utf-8")
    with pytest.raises(VersionsError, match=r"package\.json"):
        ts_ext.check_floor(tmp_path, OLDEST, NEWEST)


def test_prerelease_ignored(tmp_path: Path) -> None:
    assert _check(tmp_path, ">=26.0.0-rc.1") == []
    node_range = ts_ext._parse_range(">=26.0.0-rc.1")
    assert node_range.lowest() == (26, 0, 0)
    assert node_range.admits_major(26)
    assert not node_range.admits_major(25)


def test_window_that_is_not_bare_majors_is_an_error(tmp_path: Path) -> None:
    _package(tmp_path, {"engines": {"node": ">=22"}})
    with pytest.raises(VersionsError, match=r"not a Node major"):
        ts_ext.check_floor(tmp_path, "22.1", NEWEST)


# What node-semver gives for each range (semver 7.7.4, bundled with npm 10.9.9 and Node 22):
# ``semver.minVersion(range)`` among releases, and the majors among 0, 20, 22,
# 24 and 26 that ``semver.intersects(range, ">=N.0.0 <N+1.0.0")`` admits.
_GRAMMAR: list[tuple[str, tuple[int, int, int], tuple[int, ...]]] = [
    # x-ranges, and an empty range, which is "*"
    ("", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("x", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("X", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("24.*.*", (24, 0, 0), (24,)),
    ("24.x.3", (24, 0, 0), (24,)),
    ("v24", (24, 0, 0), (24,)),
    ("=24", (24, 0, 0), (24,)),
    ("=v24.1", (24, 1, 0), (24,)),
    ("=24.1.2", (24, 1, 2), (24,)),
    ("24.1.2+build.5", (24, 1, 2), (24,)),
    # primitives over partial versions
    (">=x", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("<=x", (0, 0, 0), (0, 20, 22, 24, 26)),
    (">= 24", (24, 0, 0), (24, 26)),
    ("  >=24  ", (24, 0, 0), (24, 26)),
    (">24", (25, 0, 0), (26,)),
    ("> 24", (25, 0, 0), (26,)),
    (">24.x", (25, 0, 0), (26,)),
    (">24.1", (24, 2, 0), (24, 26)),
    (">24.1.2", (24, 1, 3), (24, 26)),
    ("<24.x", (0, 0, 0), (0, 20, 22)),
    ("<=24", (0, 0, 0), (0, 20, 22, 24)),
    ("<=24.1", (0, 0, 0), (0, 20, 22, 24)),
    ("<=0.0.0", (0, 0, 0), (0,)),
    ("<0.0.1", (0, 0, 0), (0,)),
    ("<26.0.0-0", (0, 0, 0), (0, 20, 22, 24)),
    (">=25.5", (25, 5, 0), (26,)),
    ("<22 >=20", (20, 0, 0), (20,)),
    # tilde
    ("~24", (24, 0, 0), (24,)),
    ("~ 24", (24, 0, 0), (24,)),
    ("~>24", (24, 0, 0), (24,)),
    ("~24.x", (24, 0, 0), (24,)),
    ("~0", (0, 0, 0), (0,)),
    ("~*", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("~24.1.2", (24, 1, 2), (24,)),
    # caret
    ("^24", (24, 0, 0), (24,)),
    ("^ 24", (24, 0, 0), (24,)),
    ("^24.x", (24, 0, 0), (24,)),
    ("^0.0", (0, 0, 0), (0,)),
    ("^0.0.1", (0, 0, 1), (0,)),
    ("^0.1", (0, 1, 0), (0,)),
    ("^0.x", (0, 0, 0), (0,)),
    ("^*", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("^24.0.0-rc.1", (24, 0, 0), (24,)),
    # hyphen ranges
    ("1.2.3 - 2", (1, 2, 3), ()),
    ("24 - 26.1", (24, 0, 0), (24, 26)),
    ("x - 26", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("22 - x", (22, 0, 0), (22, 24, 26)),
    ("20.0.0 - 24.0.0-rc.1", (20, 0, 0), (20, 22)),
    ("24.x - 26.x", (24, 0, 0), (24, 26)),
    # prerelease comparators bound the releases they sit before
    (">=24.1.2-beta <25", (24, 1, 2), (24,)),
    ("~24.1.2-rc.1", (24, 1, 2), (24,)),
    ("24.1.x-rc.1", (24, 1, 0), (24,)),
    ("<=24.1.2", (0, 0, 0), (0, 20, 22, 24)),
    (">26.0.0-rc.1", (26, 0, 0), (26,)),
    ("<=26.0.0-rc.1", (0, 0, 0), (0, 20, 22, 24)),
    # an empty alternative is "*", as in npm
    ("24 || ", (0, 0, 0), (0, 20, 22, 24, 26)),
    ("||", (0, 0, 0), (0, 20, 22, 24, 26)),
    # an alternative that admits nothing is dropped
    (">=26 <24 || ^22", (22, 0, 0), (22,)),
]


@pytest.mark.parametrize(("range_text", "lowest", "majors"), _GRAMMAR)
def test_range_grammar_matches_node_semver(
    range_text: str, lowest: tuple[int, int, int], majors: tuple[int, ...]
) -> None:
    node_range = ts_ext._parse_range(range_text)
    assert node_range.lowest() == lowest
    assert tuple(n for n in (0, 20, 22, 24, 26) if node_range.admits_major(n)) == majors


def test_registered_floor_check(tmp_path: Path) -> None:
    assert FLOOR_CHECKS["typescript"] is ts_ext.check_floor
    window = parse_window(
        json.dumps(
            {
                "schema": 1,
                "languages": {
                    "typescript": {
                        "families": [
                            {
                                "key": "node",
                                "token_prefix": "",
                                "versions": [
                                    {
                                        "version": v,
                                        "eol": None,
                                        "in_policy": True,
                                        "runtime_published": None,
                                    }
                                    for v in ("24", "26", "22")
                                ],
                            }
                        ]
                    }
                },
            }
        ).encode(),
        source="fixture-window",
    )
    resolution = Resolution(
        language="typescript",
        kind="app",
        versions=("26",),
        primary="26",
        container_tags={"26": "26"},
        container_suffixes={"26": "typescript"},
    )
    _package(tmp_path, {"engines": {"node": "^20"}})
    assert check_floors(tmp_path, resolution, window) == [_floor("^20", 20), _upper("^20")]
