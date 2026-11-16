"""Tests for vergil_tooling.lib.typescript_tools."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import typescript_tools
from vergil_tooling.lib.typescript_tools import (
    PRE_GATE_CHECKS,
    check_build_script,
    check_dev_dependencies,
    check_entry_points,
    check_prettier_single_source,
    check_version_matches,
    load_jsonc,
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


def test_pre_gate_checks_include_entry_points_and_prettier() -> None:
    assert check_entry_points in PRE_GATE_CHECKS
    assert check_prettier_single_source in PRE_GATE_CHECKS


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


# -- load_jsonc ---------------------------------------------------------------------


def test_load_jsonc_reads_comments_and_trailing_commas() -> None:
    text = """
    // a line comment
    {
      /* a block
         comment */
      "a": [1, 2,], // trailing comma in an array
      "b": {"c": "x",},
    }
    """
    assert load_jsonc(text) == {"a": [1, 2], "b": {"c": "x"}}


def test_load_jsonc_keeps_comment_markers_and_commas_inside_strings() -> None:
    text = r'{"url": "http://x//y", "glob": "src/**/*.ts", "c": ",]", "q": "a\"//b",}'
    assert load_jsonc(text) == {
        "url": "http://x//y",
        "glob": "src/**/*.ts",
        "c": ",]",
        "q": 'a"//b',
    }


@pytest.mark.parametrize("text", ['{"a": 1 /* never closed', '{"a": }', "{'a': 1}"])
def test_load_jsonc_rejects_what_tsc_rejects(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - json and the comment scanner both raise it
        load_jsonc(text)


# -- check_entry_points ------------------------------------------------------------

_ESLINT_SPREAD = 'import vergil from "./eslint.vergil.config.mjs";\n\nexport default [...vergil];\n'


def _entry_repo(
    tmp_path: Path,
    tsconfig: str | None = '{"extends": "./tsconfig.vergil.json"}',
    eslint: str | None = _ESLINT_SPREAD,
) -> Path:
    if tsconfig is not None:
        (tmp_path / "tsconfig.json").write_text(tsconfig, encoding="utf-8")
    if eslint is not None:
        (tmp_path / "eslint.config.mjs").write_text(eslint, encoding="utf-8")
    return tmp_path


def test_entry_points_built_on_the_bases_pass(tmp_path: Path) -> None:
    assert check_entry_points(_entry_repo(tmp_path)) == []


def test_tsconfig_with_comments_and_trailing_comma_passes(tmp_path: Path) -> None:
    tsconfig = """{
      // Vergil's strict base, written by the fleet sync.
      "extends": "./tsconfig.vergil.json",
      "compilerOptions": {
        /* the repo's own paths */
        "rootDir": "src",
        "outDir": "dist",
      },
      "include": ["src",],
    }
    """
    assert check_entry_points(_entry_repo(tmp_path, tsconfig=tsconfig)) == []


def test_extends_array_containing_base_passes(tmp_path: Path) -> None:
    """Review Focus 1: comments, a trailing comma and an ``extends`` array."""
    tsconfig = """{
      // the base and a community preset
      "extends": ["./tsconfig.vergil.json", "@tsconfig/node24/tsconfig.json"],
      "include": ["src"],
    }
    """
    assert check_entry_points(_entry_repo(tmp_path, tsconfig=tsconfig)) == []


@pytest.mark.parametrize(
    "tsconfig",
    [
        "{}",
        '{"extends": "@tsconfig/node24/tsconfig.json"}',
        '{"extends": "tsconfig.vergil.json"}',
        '{"extends": ["@tsconfig/node24/tsconfig.json"]}',
        '{"compilerOptions": {"extends": "./tsconfig.vergil.json"}}',
        '// "extends": "./tsconfig.vergil.json"\n{}',
    ],
)
def test_tsconfig_without_base_fails(tmp_path: Path, tsconfig: str) -> None:
    assert check_entry_points(_entry_repo(tmp_path, tsconfig=tsconfig)) == [
        'tsconfig.json: does not extend "./tsconfig.vergil.json". Fix: set '
        '"extends": "./tsconfig.vergil.json", or add it to the "extends" array'
    ]


def test_missing_tsconfig_fails(tmp_path: Path) -> None:
    assert check_entry_points(_entry_repo(tmp_path, tsconfig=None)) == [
        'tsconfig.json: missing. Fix: add a tsconfig.json with "extends": "./tsconfig.vergil.json"'
    ]


def test_unparseable_tsconfig_fails(tmp_path: Path) -> None:
    (problem,) = check_entry_points(_entry_repo(tmp_path, tsconfig='{"extends": }'))
    assert problem.startswith("tsconfig.json: cannot be parsed: ")
    assert problem.endswith(". Fix: correct the JSON")


def test_tsconfig_not_an_object_fails(tmp_path: Path) -> None:
    assert check_entry_points(_entry_repo(tmp_path, tsconfig='["./tsconfig.vergil.json"]')) == [
        "tsconfig.json: the top level is not an object. Fix: correct the JSON"
    ]


def test_tsconfig_not_utf8_fails(tmp_path: Path) -> None:
    repo = _entry_repo(tmp_path, tsconfig=None)
    (repo / "tsconfig.json").write_bytes(b'{"extends": "\xff"}')
    (problem,) = check_entry_points(repo)
    assert problem.startswith("tsconfig.json: cannot be parsed: not UTF-8")


def test_unreadable_entry_points_fail(tmp_path: Path) -> None:
    repo = _entry_repo(tmp_path, tsconfig=None, eslint=None)
    (repo / "tsconfig.json").mkdir()
    (repo / "eslint.config.mjs").write_text(_ESLINT_SPREAD, encoding="utf-8")
    with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
        problems = check_entry_points(repo)
    assert problems[0].startswith("tsconfig.json: missing.")
    assert problems[1] == "eslint.config.mjs: cannot be read: Permission denied"


def test_missing_eslint_config_fails(tmp_path: Path) -> None:
    assert check_entry_points(_entry_repo(tmp_path, eslint=None)) == [
        "eslint.config.mjs: missing. Fix: add an eslint.config.mjs that builds on Vergil's "
        'config, for example: import vergil from "./eslint.vergil.config.mjs"; '
        "export default [...vergil];"
    ]


@pytest.mark.parametrize(
    "eslint",
    [
        "export default [];\n",
        'import vergil from "./eslint.vergil.config.js";\nexport default [...vergil];\n',
        '// import vergil from "./eslint.vergil.config.mjs";\nexport default [...vergil];\n',
        '/* import vergil from "./eslint.vergil.config.mjs"; */\nexport default [];\n',
    ],
)
def test_eslint_config_without_base_import_fails(tmp_path: Path, eslint: str) -> None:
    assert check_entry_points(_entry_repo(tmp_path, eslint=eslint)) == [
        'eslint.config.mjs: does not import a binding from "./eslint.vergil.config.mjs". '
        "Fix: import it and spread it, for example: "
        'import vergil from "./eslint.vergil.config.mjs"; '
        "export default [...vergil];"
    ]


@pytest.mark.parametrize(
    "export",
    [
        "const config = [...vergil];",
        "export default [];",
        "export default [vergil];",
        "export default [{ ignores: ['...vergil'] }];",
        "export default [...vergilExtra];",
        "export default [[...vergil]];",
        "export default somethingElse(...vergil);",
        "export default config(vergil);",
        "export default defineConfig([vergil]);",
        "export default [...vergil",
        "export default [...vergil);",
        "const config = [...vergil];\nexport default config;",
        "// export default [...vergil];\nexport default [];",
    ],
)
def test_eslint_config_without_spread_fails(tmp_path: Path, export: str) -> None:
    eslint = f'import vergil from "./eslint.vergil.config.mjs";\n{export}\n'
    assert check_entry_points(_entry_repo(tmp_path, eslint=eslint)) == [
        "eslint.config.mjs: its default export does not include vergil from "
        '"./eslint.vergil.config.mjs". Fix: export default [...vergil, …], or pass vergil '
        "to defineConfig(…) or tseslint.config(…)"
    ]


def test_eslint_config_spreading_base_with_ignores_passes(tmp_path: Path) -> None:
    eslint = """// The repo's ESLint config, built on Vergil's.
import base from './eslint.vergil.config.mjs';

export default [
  { ignores: ["src/generated/", "*.min.js"] },
  ...base,
  {
    files: ["src/**/*.ts"],
    rules: { "no-console": "error" },
  },
];
"""
    assert check_entry_points(_entry_repo(tmp_path, eslint=eslint)) == []


def test_eslint_define_config_with_base_passes(tmp_path: Path) -> None:
    eslint = """import { defineConfig } from "eslint/config";
import vergil from "./eslint.vergil.config.mjs";

export default defineConfig(
  vergil,
  { ignores: ["dist/", "a,b"] },
);
"""
    assert check_entry_points(_entry_repo(tmp_path, eslint=eslint)) == []


def test_eslint_define_config_with_array_argument_passes(tmp_path: Path) -> None:
    eslint = (
        'import { defineConfig } from "eslint/config";\n'
        'import vergil from "./eslint.vergil.config.mjs";\n'
        'export default defineConfig([...vergil, { ignores: ["gen/"] }]);\n'
    )
    assert check_entry_points(_entry_repo(tmp_path, eslint=eslint)) == []


def test_eslint_tseslint_config_with_base_passes(tmp_path: Path) -> None:
    eslint = """import tseslint from "typescript-eslint";
import vergil, { other } from "./eslint.vergil.config.mjs";

export default tseslint.config({ ignores: ["(" + ")"] }, ...vergil, other);
"""
    assert check_entry_points(_entry_repo(tmp_path, eslint=eslint)) == []


def test_eslint_default_as_import_passes(tmp_path: Path) -> None:
    eslint = (
        'import { default as vergil } from "./eslint.vergil.config.mjs";\n'
        "export default [...vergil];\n"
    )
    assert check_entry_points(_entry_repo(tmp_path, eslint=eslint)) == []


def test_eslint_config_shadowed_by_another_fails(tmp_path: Path) -> None:
    repo = _entry_repo(tmp_path)
    (repo / "eslint.config.js").write_text("export default [];\n", encoding="utf-8")
    (repo / "eslint.config.ts").write_text("export default [];\n", encoding="utf-8")
    assert check_entry_points(repo) == [
        "eslint.config.js: ESLint may read it instead of eslint.config.mjs. "
        "Fix: delete it and keep the repo's ESLint config in eslint.config.mjs",
        "eslint.config.ts: ESLint may read it instead of eslint.config.mjs. "
        "Fix: delete it and keep the repo's ESLint config in eslint.config.mjs",
    ]


def test_entry_points_report_both_problems(tmp_path: Path) -> None:
    problems = check_entry_points(_entry_repo(tmp_path, tsconfig="{}", eslint="export default [];"))
    assert [problem.split(":")[0] for problem in problems] == [
        "tsconfig.json",
        "eslint.config.mjs",
    ]


# -- check_prettier_single_source --------------------------------------------------


def _prettier_repo(tmp_path: Path, manifest: dict[str, object] | None = None) -> Path:
    """A real git repository with a package.json and the managed .prettierrc.json."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".worktrees/\nbuild/\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps(manifest if manifest is not None else {"name": "x"}), encoding="utf-8"
    )
    (tmp_path / ".prettierrc.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_one_prettier_config_passes(tmp_path: Path) -> None:
    repo = _prettier_repo(tmp_path)
    # Not Prettier configs: the ignore file, and configs inside node_modules.
    (repo / ".prettierignore").write_text("dist\n", encoding="utf-8")
    pkg = repo / "node_modules" / "some-pkg"
    pkg.mkdir(parents=True)
    (pkg / ".prettierrc").write_text("{}", encoding="utf-8")
    assert check_prettier_single_source(repo) == []


def test_gitignored_prettier_configs_are_not_searched(tmp_path: Path) -> None:
    """A worktree's managed copy, or build scratch, is no second config.

    Prettier skips gitignored files too, so they never change what the gate checks.
    """
    repo = _prettier_repo(tmp_path)
    for path in (".worktrees/issue-1-x/.prettierrc.json", "build/tmp/.prettierrc"):
        (repo / path).parent.mkdir(parents=True)
        (repo / path).write_text("{}", encoding="utf-8")
    assert check_prettier_single_source(repo) == []


def test_tracked_prettier_config_deleted_from_disk_is_dropped(tmp_path: Path) -> None:
    repo = _prettier_repo(tmp_path)
    (repo / ".prettierrc").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".prettierrc"], check=True, capture_output=True)
    (repo / ".prettierrc").unlink()
    assert check_prettier_single_source(repo) == []


def test_prettier_check_outside_a_repo_raises(tmp_path: Path) -> None:
    """Outside a repository ls-files fails: an error, never 'check less'."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        check_prettier_single_source(tmp_path)


def test_prettier_key_in_package_json_fails(tmp_path: Path) -> None:
    """Review Focus 3: a ``"prettier"`` key fails even with ``.prettierrc.json`` present."""
    repo = _prettier_repo(tmp_path, {"name": "x", "prettier": {"semi": False}})
    assert check_prettier_single_source(repo) == [
        'package.json: has a "prettier" key, a second Prettier config. '
        "Fix: delete it; the repo's one Prettier config is .prettierrc.json"
    ]


@pytest.mark.parametrize(
    "path",
    [
        ".prettierrc",
        ".prettierrc.yaml",
        ".prettierrc.json5",
        ".prettierrc.cjs",
        "prettier.config.js",
        "prettier.config.mjs",
        "src/.prettierrc.json",
        "packages/a/.prettierrc.toml",
    ],
)
def test_extra_prettierrc_fails(tmp_path: Path, path: str) -> None:
    repo = _prettier_repo(tmp_path)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("{}", encoding="utf-8")
    assert check_prettier_single_source(repo) == [
        f"{path}: a second Prettier config. "
        "Fix: delete it; the repo's one Prettier config is .prettierrc.json"
    ]


def test_every_extra_prettier_source_is_reported(tmp_path: Path) -> None:
    repo = _prettier_repo(tmp_path, {"name": "x", "prettier": "@acme/prettier-config"})
    for path in ("prettier.config.js", ".prettierrc"):
        (repo / path).write_text("{}", encoding="utf-8")
    problems = check_prettier_single_source(repo)
    assert [problem.split(":")[0] for problem in problems] == [
        "package.json",
        ".prettierrc",
        "prettier.config.js",
    ]


def test_unparseable_package_json_is_reported(tmp_path: Path) -> None:
    repo = _prettier_repo(tmp_path)
    (repo / "package.json").write_text("{", encoding="utf-8")
    (problem,) = check_prettier_single_source(repo)
    assert problem.startswith('cannot check package.json for a "prettier" key: package.json at ')
