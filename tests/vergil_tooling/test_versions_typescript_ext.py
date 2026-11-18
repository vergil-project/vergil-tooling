"""Tests for the TypeScript resolver extension (epic vergil-project/.github#1281, spec §6.2).

Every setting that names a Node or package-manager version is an error: the
Node runtime and npm belong to Vergil. ``engines.node`` and ``devEngines``'
``os``, ``cpu`` and ``libc`` entries are allowed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.container import PackageJsonError, read_package_json
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions import TOOLCHAIN_CHECKS
from vergil_tooling.lib.versions.extensions import typescript as ts_ext
from vergil_tooling.lib.versions.floors import check_toolchain_selection

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
        ({"devEngines": 1}, "devEngines"),
        ({"devEngines": {"runtime": [1]}}, "devEngines.runtime"),
        ({"devEngines": {"os": "linux"}}, "devEngines.os"),
    ],
)
def test_malformed_fields_are_versions_errors(tmp_path: Path, data: object, field: str) -> None:
    _package(tmp_path, data)
    with pytest.raises(VersionsError, match=rf"package\.json: {field} .*Fix: \w+ {field}"):
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
