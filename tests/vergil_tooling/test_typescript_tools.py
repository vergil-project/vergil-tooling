"""Tests for vergil_tooling.lib.typescript_tools."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import typescript_tools
from vergil_tooling.lib.typescript_tools import (
    PRE_GATE_CHECKS,
    check_build_script,
    check_dev_dependencies,
    check_version_matches,
    load_required,
    run_pre_gate_checks,
)

_ACCEPTED = {
    "typescript": 6,
    "typescript-eslint": 8,
    "eslint": 10,
    "@eslint/js": 10,
    "vitest": 5,
    "@vitest/coverage-v8": 5,
}


def _repo(
    tmp_path: Path,
    declared: dict[str, object] | None = None,
    installed: dict[str, str] | None = None,
) -> Path:
    """A repo declaring *declared* and with *installed* under ``node_modules``.

    Both default to every required package at its accepted major.
    """
    if declared is None:
        declared = {name: f"^{major}.0.0" for name, major in _ACCEPTED.items()}
    if installed is None:
        installed = {name: f"{major}.1.0" for name, major in _ACCEPTED.items()}
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "devDependencies": declared}), encoding="utf-8"
    )
    for name, version in installed.items():
        pkg = tmp_path / "node_modules" / name
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(
            json.dumps({"name": name, "version": version}), encoding="utf-8"
        )
    return tmp_path


def _declared(**overrides: object) -> dict[str, object]:
    declared: dict[str, object] = {name: f"^{major}.0.0" for name, major in _ACCEPTED.items()}
    for name, value in overrides.items():
        declared[name.replace("_", "-")] = value
    return declared


# -- load_required ---------------------------------------------------------------


def test_load_required_reads_the_packaged_majors() -> None:
    assert load_required() == _ACCEPTED


def test_standards_page_table_mirrors_the_data_file() -> None:
    """The overview page's table of accepted majors matches the data file."""
    page = (
        Path(__file__).resolve().parents[2]
        / "docs/site/docs/standards/development/typescript/overview.md"
    )
    rows = re.findall(r"^\| `([^`]+)` \| (\d+) \|$", page.read_text(encoding="utf-8"), re.M)
    assert {name: int(major) for name, major in rows} == load_required()


def _fake_resource(text: str) -> MagicMock:
    resource = MagicMock()
    resource.joinpath.return_value.read_text.return_value = text
    return resource


@pytest.mark.parametrize(
    "text",
    ["[]", "{}", '{"typescript": "6"}', '{"typescript": 0}', '{"typescript": true}'],
)
def test_load_required_rejects_a_malformed_data_file(text: str) -> None:
    with (
        patch.object(typescript_tools, "files", return_value=_fake_resource(text)),
        pytest.raises(ValueError, match="dev-dependencies.json"),
    ):
        load_required()


# -- check_dev_dependencies -------------------------------------------------------


def test_all_required_present_passes(tmp_path: Path) -> None:
    assert check_dev_dependencies(_repo(tmp_path)) == []


@pytest.mark.parametrize("declared", ["~6.0.3", "6.0.3", "6", "^6", "^6.0", "6.x", "6.0.*"])
def test_accepted_range_forms_pass(tmp_path: Path, declared: str) -> None:
    repo = _repo(tmp_path, _declared(typescript=declared))
    assert check_dev_dependencies(repo) == []


def test_missing_package_named_in_problem(tmp_path: Path) -> None:
    declared = _declared()
    del declared["vitest"]
    problems = check_dev_dependencies(_repo(tmp_path, declared))
    assert problems == [
        "vitest: not in devDependencies",
        "Fix: npm install --save-dev vitest@^5",
    ]


def test_no_dev_dependencies_names_every_package(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    problems = check_dev_dependencies(tmp_path)
    assert problems[:-1] == [f"{name}: not in devDependencies" for name in _ACCEPTED]


def test_typescript_5_range_rejected(tmp_path: Path) -> None:
    problems = check_dev_dependencies(_repo(tmp_path, _declared(typescript="^5.9.0")))
    assert problems == [
        'typescript: declares "^5.9.0", outside the accepted major 6',
        "Fix: npm install --save-dev typescript@^6",
    ]


@pytest.mark.parametrize(
    "declared", ["latest", "*", ">=5", "^60.0.0", "7.x", ">=6 <7", "^6.0.0-beta", 6]
)
def test_latest_star_and_open_ranges_rejected(tmp_path: Path, declared: object) -> None:
    problems = check_dev_dependencies(_repo(tmp_path, _declared(typescript=declared)))
    assert problems == [
        f"typescript: declares {json.dumps(declared)}, outside the accepted major 6",
        "Fix: npm install --save-dev typescript@^6",
    ]


def test_installed_major_mismatch_rejected(tmp_path: Path) -> None:
    installed = {name: f"{major}.1.0" for name, major in _ACCEPTED.items()}
    installed["typescript"] = "7.0.2"
    problems = check_dev_dependencies(_repo(tmp_path, _declared(), installed))
    assert problems == [
        "typescript: installed 7.0.2, outside the accepted major 6",
        "Fix: npm install --save-dev typescript@^6",
    ]


def test_declared_and_locked_but_not_installed_rejected(tmp_path: Path) -> None:
    installed = {name: f"{major}.1.0" for name, major in _ACCEPTED.items()}
    del installed["@eslint/js"]
    repo = _repo(tmp_path, _declared(), installed)
    lock = {"lockfileVersion": 3, "packages": {"node_modules/@eslint/js": {"version": "10.0.1"}}}
    (repo / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    problems = check_dev_dependencies(repo)
    assert problems == [
        "@eslint/js: declared but not installed (node_modules/@eslint/js/package.json is absent)",
        "Fix: npm install --save-dev @eslint/js@^10",
    ]


def test_installed_manifest_without_a_version_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "node_modules" / "eslint" / "package.json").write_text('{"name": "eslint"}')
    problems = check_dev_dependencies(repo)
    assert problems[0] == "eslint: node_modules/eslint/package.json has no version string"


def test_installed_manifest_unparseable_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "node_modules" / "eslint" / "package.json").write_text("{not json")
    problems = check_dev_dependencies(repo)
    assert problems[0].startswith("eslint: package.json at ")
    assert "cannot be parsed" in problems[0]
    assert problems[-1] == "Fix: npm install --save-dev eslint@^10"


def test_unparseable_package_json_is_the_one_problem(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("[]", encoding="utf-8")
    problems = check_dev_dependencies(tmp_path)
    assert len(problems) == 1
    assert "cannot be parsed" in problems[0]


def test_fix_line_lists_every_problem_package(tmp_path: Path) -> None:
    declared = _declared(typescript="latest", eslint="^9.0.0")
    del declared["vitest"]
    installed = {name: f"{major}.1.0" for name, major in _ACCEPTED.items()}
    installed["typescript-eslint"] = "7.18.0"
    problems = check_dev_dependencies(_repo(tmp_path, declared, installed))
    assert problems == [
        'typescript: declares "latest", outside the accepted major 6',
        "typescript-eslint: installed 7.18.0, outside the accepted major 8",
        'eslint: declares "^9.0.0", outside the accepted major 10',
        "vitest: not in devDependencies",
        "Fix: npm install --save-dev typescript@^6 typescript-eslint@^8 eslint@^10 vitest@^5",
    ]


# -- PRE_GATE_CHECKS ----------------------------------------------------------------


def test_pre_gate_checks_include_dev_dependencies() -> None:
    assert check_dev_dependencies in PRE_GATE_CHECKS


def test_pre_gate_checks_include_version_matches() -> None:
    assert check_version_matches in PRE_GATE_CHECKS


def _versioned_repo(tmp_path: Path, version: str, package_version: str) -> Path:
    (tmp_path / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        'primary-language = "typescript"\n\n[dependencies]\nvergil = "v2.0"\n'
        '\n[ci]\nversions = ["node-24"]\n',
        encoding="utf-8",
    )
    (tmp_path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "version": package_version}), encoding="utf-8"
    )
    return tmp_path


def test_pre_gate_passes_on_matching_version(tmp_path: Path) -> None:
    assert check_version_matches(_versioned_repo(tmp_path, "0.1.0", "0.1.0")) == []


def test_pre_gate_fails_on_version_mismatch(tmp_path: Path) -> None:
    repo = _versioned_repo(tmp_path, "0.1.0", "1.0.0")
    assert check_version_matches(repo) == [
        "VERSION contains 0.1.0 but package.json contains 1.0.0. "
        "Fix: set it to the VERSION file's 0.1.0"
    ]


def test_pre_gate_fails_on_missing_version_file(tmp_path: Path) -> None:
    repo = _versioned_repo(tmp_path, "0.1.0", "0.1.0")
    (repo / "VERSION").unlink()
    problems = check_version_matches(repo)
    assert len(problems) == 1
    assert "VERSION file not found" in problems[0]


def test_run_pre_gate_checks_reports_every_check(tmp_path: Path) -> None:
    def first(_root: Path) -> list[str]:
        return ["a"]

    def second(_root: Path) -> list[str]:
        return ["b", "c"]

    with patch.object(typescript_tools, "PRE_GATE_CHECKS", (first, second)):
        assert run_pre_gate_checks(tmp_path) == ["a", "b", "c"]


def test_run_pre_gate_checks_reports_a_shared_problem_once(tmp_path: Path) -> None:
    # Two checks that both read a broken package.json report its error once.
    def first(_root: Path) -> list[str]:
        return ["broken", "a"]

    def second(_root: Path) -> list[str]:
        return ["broken", "b"]

    with patch.object(typescript_tools, "PRE_GATE_CHECKS", (first, second)):
        assert run_pre_gate_checks(tmp_path) == ["broken", "a", "b"]


# -- check_build_script -------------------------------------------------------------

_BUILD_FIX = 'Fix: add a "build" script to package.json (for example "tsc -p tsconfig.json")'


def _manifest(tmp_path: Path, manifest: object) -> Path:
    (tmp_path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_pre_gate_checks_include_build_script() -> None:
    assert check_build_script in PRE_GATE_CHECKS


def test_build_script_present_passes(tmp_path: Path) -> None:
    repo = _manifest(tmp_path, {"name": "x", "scripts": {"build": "tsc -p tsconfig.json"}})
    assert check_build_script(repo) == []


def test_missing_build_script_fails(tmp_path: Path) -> None:
    repo = _manifest(tmp_path, {"name": "x", "scripts": {"test": "vitest run"}})
    assert check_build_script(repo) == [
        'package.json: no "build" script (the BUILD check and the release build run it)',
        _BUILD_FIX,
    ]


@pytest.mark.parametrize(
    "manifest",
    [
        {"name": "x"},
        {"name": "x", "scripts": "tsc"},
        {"name": "x", "scripts": {"build": ""}},
        {"name": "x", "scripts": {"build": "   "}},
        {"name": "x", "scripts": {"build": ["tsc"]}},
    ],
)
def test_absent_or_empty_build_script_fails(tmp_path: Path, manifest: object) -> None:
    assert check_build_script(_manifest(tmp_path, manifest))[-1] == _BUILD_FIX


def test_build_script_unparseable_package_json_is_the_one_problem(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    problems = check_build_script(tmp_path)
    assert len(problems) == 1
    assert "package.json" in problems[0]
