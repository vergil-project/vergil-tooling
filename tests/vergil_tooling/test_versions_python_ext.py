"""Tests for the Python resolver extension (epic vergil-project/.github#407, Task 9).

Window fixture: python 3.15 (no runtime yet), 3.14 (runtime published), 3.13 and
3.12 (no runtime), so a packaged repo must fall back past the newest version.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_container_run import main as container_run_main
from vergil_tooling.lib.config import VergilConfig, _parse_raw_config
from vergil_tooling.lib.versions import ResolutionError, VersionsError
from vergil_tooling.lib.versions.extensions import (
    CONTAINER_ENVS,
    EXTENSIONS,
    container_env,
)
from vergil_tooling.lib.versions.extensions import python as py_ext
from vergil_tooling.lib.versions.resolve import Resolution, resolve
from vergil_tooling.lib.versions.window import WINDOW_ENV, Window, parse_window

_RUNTIMES = {"3.15": False, "3.14": True, "3.13": False, "3.12": None}


def _window(runtimes: dict[str, bool | None] = _RUNTIMES) -> Window:
    doc: dict[str, Any] = {
        "schema": 1,
        "languages": {
            "python": {
                "families": [
                    {
                        "key": "python",
                        "token_prefix": "",
                        "versions": [
                            {"version": v, "eol": None, "in_policy": True, "runtime_published": r}
                            for v, r in runtimes.items()
                        ],
                    }
                ]
            }
        },
    }
    return parse_window(json.dumps(doc).encode(), source="fixture-window")


_PACKAGE = {"builder": "python", "vendor": "vergil", "summary": "s", "smoke": "true"}


def _cfg(
    *,
    repo_type: str = "application",
    versions: list[str] | None = None,
    packaged: bool = True,
    runtime: str | None = None,
) -> VergilConfig:
    raw: dict[str, Any] = {
        "project": {
            "repository-type": repo_type,
            "versioning-scheme": "semver",
            "branching-model": "library-release",
            "release-model": "tagged-release",
            "primary-language": "python",
        },
        "dependencies": {"vergil": "v2.1"},
        "ci": {} if versions is None else {"versions": versions},
    }
    if packaged:
        package: dict[str, Any] = dict(_PACKAGE)
        if runtime is not None:
            package["python"] = {"runtime": runtime}
        raw["package"] = package
    return _parse_raw_config(raw)


def _resolve(cfg: VergilConfig, window: Window | None = None) -> Resolution:
    result = resolve(cfg, window or _window(), repo_root=None)
    assert result is not None
    return result


def test_python_is_registered() -> None:
    assert EXTENSIONS["python"] is py_ext.python_extension
    assert CONTAINER_ENVS["python"] is py_ext.container_env


# -- packaged runtime (Review Focus 4) ------------------------------------------


def test_python_packaged_falls_back_to_newest_with_runtime() -> None:
    # 3.15 is in the window but vergil-python has not published it yet.
    result = _resolve(_cfg())
    assert result.versions == ("3.14",)
    assert result.primary == "3.14"


def test_python_packaged_no_runtime_fails_loud() -> None:
    window = _window({"3.15": False, "3.14": None})
    with pytest.raises(ResolutionError, match="no published vergil-python runtime for any of"):
        _resolve(_cfg(), window)


def test_python_packaged_pin_without_runtime_fails_loud() -> None:
    with pytest.raises(ResolutionError, match=r"runtime for any of 3\.13\b"):
        _resolve(_cfg(versions=["3.13"]))


def test_python_packaged_app_multi_pin_is_fatal_before_the_extension() -> None:
    # An app may pin only one version, so the extension never sees a list.
    with pytest.raises(ResolutionError, match="builds exactly one version"):
        _resolve(_cfg(versions=["3.15", "3.14", "3.13"]))


def test_python_packaged_library_keeps_every_version() -> None:
    # A library's tests cover every version its consumers build on; only its
    # deployed runtime needs a published package (derived_runtime).
    result = _resolve(_cfg(repo_type="library"))
    assert result.versions == ("3.15", "3.14", "3.13", "3.12")
    assert result.primary == "3.15"


def test_python_packaged_library_needs_some_runtime() -> None:
    with pytest.raises(ResolutionError, match="no published vergil-python runtime"):
        _resolve(_cfg(repo_type="library", versions=["3.13", "3.12"]))


def test_python_unpackaged_ignores_runtime() -> None:
    result = _resolve(_cfg(packaged=False))
    assert result.versions == ("3.15",)
    window = _window({"3.15": False, "3.14": None})
    assert _resolve(_cfg(packaged=False, repo_type="library"), window).versions == (
        "3.15",
        "3.14",
    )


def test_python_staged_builder_ignores_runtime() -> None:
    cfg = _cfg(packaged=False)
    raw_pkg = {**_PACKAGE, "builder": "staged", "name": "thing"}
    staged = _parse_raw_config(
        {
            "project": {
                "repository-type": "application",
                "versioning-scheme": "semver",
                "branching-model": "library-release",
                "release-model": "tagged-release",
                "primary-language": "python",
            },
            "dependencies": {"vergil": "v2.1"},
            "ci": {},
            "package": {**raw_pkg, "staged": {"build-command": "true"}},
        }
    )
    assert _resolve(staged).versions == _resolve(cfg).versions == ("3.15",)


# -- the derived runtime --------------------------------------------------------


def test_runtime_derived_from_resolution() -> None:
    window = _window()
    assert py_ext.derived_runtime(_resolve(_cfg(), window), window) == "3.14"
    library = _resolve(_cfg(repo_type="library"), window)
    assert py_ext.derived_runtime(library, window) == "3.14"


def test_derived_runtime_without_a_published_runtime_fails() -> None:
    window = _window()
    unpackaged = _resolve(_cfg(packaged=False, versions=["3.13"]), window)
    with pytest.raises(ResolutionError, match="Fix: pin \\[package.python\\].runtime"):
        py_ext.derived_runtime(unpackaged, window)


def test_explicit_runtime_inside_window_resolves() -> None:
    assert _resolve(_cfg(runtime="3.14.8")).versions == ("3.14",)


def test_explicit_runtime_outside_window_fatal() -> None:
    with pytest.raises(ResolutionError) as exc:
        _resolve(_cfg(runtime="3.11.9"))
    message = str(exc.value)
    assert "[package.python].runtime 3.11.9 is not in the python window" in message
    assert "3.15, 3.14, 3.13, 3.12" in message
    assert "Fix: remove [package.python].runtime" in message


@pytest.mark.parametrize(("runtime", "series"), [("3.14", "3.14"), ("3.14.8", "3.14")])
def test_runtime_series(runtime: str, series: str) -> None:
    assert py_ext.runtime_series(runtime) == series


def test_runtime_series_rejects_garbage() -> None:
    with pytest.raises(ResolutionError, match="'3' is not an x.y or x.y.z version"):
        py_ext.runtime_series("3")


# -- floors (spec §6.4) ---------------------------------------------------------


def _pyproject(root: Path, body: str) -> Path:
    (root / "pyproject.toml").write_text(body)
    return root


def test_floor_below_oldest_warns(tmp_path: Path) -> None:
    root = _pyproject(tmp_path, '[project]\nrequires-python = ">=3.11"\n')
    assert py_ext.check_floor(root, "3.12", "3.15") == [
        "requires-python floor 3.11 is below the oldest supported version 3.12. "
        "Fix: raise it to >=3.12"
    ]


def test_upper_bound_excluding_newest_warns(tmp_path: Path) -> None:
    # The lab's pin, which would hold an app back from 3.15.
    root = _pyproject(tmp_path, '[project]\nrequires-python = ">=3.14,<3.15"\n')
    (warning,) = py_ext.check_floor(root, "3.12", "3.15")
    assert warning.startswith("requires-python upper bound <3.15 excludes 3.15")


@pytest.mark.parametrize("spec", [">=3.14", ">=3.12,<4.0", ">=3.12,<=3.15", "~=3.12"])
def test_floor_above_oldest_is_silent(tmp_path: Path, spec: str) -> None:
    root = _pyproject(tmp_path, f'[project]\nrequires-python = "{spec}"\n')
    assert py_ext.check_floor(root, "3.12", "3.15") == []


def test_exact_series_floor_and_bound_both_warn(tmp_path: Path) -> None:
    root = _pyproject(tmp_path, '[project]\nrequires-python = "==3.11.*"\n')
    floor, bound = py_ext.check_floor(root, "3.12", "3.15")
    assert floor.startswith("requires-python floor 3.11 is below")
    assert bound.startswith("requires-python upper bound ==3.11.* excludes 3.15")


@pytest.mark.parametrize(
    "body", ["", "[project]\n", '[project]\nname = "x"\n', '[tool.x]\nrequires-python = "<3"\n']
)
def test_no_requires_python_is_silent(tmp_path: Path, body: str) -> None:
    assert py_ext.check_floor(_pyproject(tmp_path, body), "3.12", "3.15") == []


def test_no_pyproject_is_silent(tmp_path: Path) -> None:
    assert py_ext.check_floor(tmp_path, "3.12", "3.15") == []


def test_upper_bound_only_has_no_floor_warning(tmp_path: Path) -> None:
    root = _pyproject(tmp_path, '[project]\nrequires-python = "<4"\n')
    assert py_ext.check_floor(root, "3.12", "3.15") == []


@pytest.mark.parametrize("value", ['"not a spec"', "3"])
def test_invalid_requires_python_is_fatal(tmp_path: Path, value: str) -> None:
    root = _pyproject(tmp_path, f"[project]\nrequires-python = {value}\n")
    with pytest.raises(VersionsError, match="is not a valid version specifier"):
        py_ext.check_floor(root, "3.12", "3.15")


def test_invalid_pyproject_is_fatal(tmp_path: Path) -> None:
    root = _pyproject(tmp_path, "[project\n")
    with pytest.raises(VersionsError, match="is not valid TOML"):
        py_ext.check_floor(root, "3.12", "3.15")


def test_malformed_window_newest_is_fatal(tmp_path: Path) -> None:
    root = _pyproject(tmp_path, '[project]\nrequires-python = ">=3.12"\n')
    with pytest.raises(VersionsError, match="newest version 'next' is not a version"):
        py_ext.check_floor(root, "3.12", "next")


# -- toolchain selection (spec §6.5) --------------------------------------------


def test_python_version_file_is_error(tmp_path: Path) -> None:
    (tmp_path / ".python-version").write_text("3.14\n")
    (error,) = py_ext.check_toolchain(tmp_path)
    assert error.startswith(".python-version selects the Python interpreter")
    assert "Fix: delete .python-version" in error


def test_python_preference_is_allowed(tmp_path: Path) -> None:
    # python-preference picks the interpreter's source (managed or system), not
    # its version, so a repo may keep it (spec §6.5 amendment, issue #3410).
    _pyproject(tmp_path, '[tool.uv]\npython-preference = "only-managed"\n')
    (tmp_path / "uv.toml").write_text('python-preference = "only-managed"\n')
    assert py_ext.check_toolchain(tmp_path) == []


def test_python_version_file_fails_beside_python_preference(tmp_path: Path) -> None:
    _pyproject(tmp_path, '[tool.uv]\npython-preference = "only-managed"\n')
    (tmp_path / ".python-version").write_text("3.14\n")
    (error,) = py_ext.check_toolchain(tmp_path)
    assert error.startswith(".python-version selects the Python interpreter")


def test_clean_toolchain_has_no_errors(tmp_path: Path) -> None:
    _pyproject(tmp_path, '[tool.uv]\nconstraint-dependencies = ["x>=1"]\n')
    (tmp_path / "uv.toml").write_text("native-tls = true\n")
    assert py_ext.check_toolchain(tmp_path) == []
    assert py_ext.check_toolchain(tmp_path / "missing") == []


# -- the container environment (spec §6.5, requirement 1) -----------------------


def test_container_env_sets_uv_python_and_pins_the_source() -> None:
    assert container_env(_resolve(_cfg(packaged=False))) == {
        "UV_PYTHON": "3.15",
        "UV_PYTHON_PREFERENCE": "only-system",
    }
    assert container_env(None) == {}


def test_source_env_pins_only_the_interpreter_source() -> None:
    # vergil-tooling#3414: a docs build resolves no version, so it takes the
    # source pin alone; container_env is that pin plus UV_PYTHON.
    assert py_ext.source_env() == {"UV_PYTHON_PREFERENCE": "only-system"}
    assert py_ext.container_env("3.15") == {"UV_PYTHON": "3.15", **py_ext.source_env()}


def test_container_env_empty_for_a_language_without_a_hook() -> None:
    go = Resolution(
        language="go",
        kind="app",
        versions=("1.26",),
        primary="1.26",
        container_tags={"1.26": "1.26"},
        container_suffixes={"1.26": "go"},
    )
    assert container_env(go) == {}


def test_container_run_passes_uv_python(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        '[project]\nrepository-type = "application"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        'primary-language = "python"\n[dependencies]\nvergil = "v2.1"\n[ci]\n'
    )
    mod = "vergil_tooling.bin.vrg_container_run"
    with (
        patch(f"{mod}.git.repo_root", return_value=tmp_path),
        patch(f"{mod}.assert_runtime_available"),
        patch(f"{mod}.ensure_cached_image", return_value="img"),
        patch(f"{mod}.detect_runtime", return_value="docker"),
        patch(f"{mod}.os.execvp") as mock_exec,
        patch.dict("os.environ", {WINDOW_ENV: _fixture_window_path()}, clear=True),
    ):
        container_run_main(["--", "true"])
    # The fixture window's newest python is 3.14.
    assert "UV_PYTHON=3.14" in mock_exec.call_args[0][1]


def test_container_run_image_override_sets_no_uv_python(tmp_path: Path) -> None:
    mod = "vergil_tooling.bin.vrg_container_run"
    with (
        patch(f"{mod}.git.repo_root", return_value=tmp_path),
        patch(f"{mod}.assert_runtime_available"),
        patch(f"{mod}.detect_runtime", return_value="docker"),
        patch(f"{mod}.os.execvp") as mock_exec,
        patch.dict("os.environ", {"DOCKER_DEV_IMAGE": "custom:img"}, clear=True),
    ):
        container_run_main(["--", "true"])
    assert not any(a.startswith("UV_PYTHON=") for a in mock_exec.call_args[0][1])


def _fixture_window_path() -> str:
    return str(Path(__file__).parent / "fixtures" / "versions" / "window.json")
