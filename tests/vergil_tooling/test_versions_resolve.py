"""Tests for vergil_tooling.lib.versions.resolve (epic vergil-project/.github#407).

One test per row of the resolution table (spec §6.3), against a window of
python 3.15-3.12, go 1.27/1.26, and cpp clang 20/19 plus gcc 14.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.config import ConfigError, VergilConfig, _parse_raw_config
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions import EXTENSIONS, ExtensionContext
from vergil_tooling.lib.versions.resolve import (
    Resolution,
    ResolutionError,
    ci_outputs,
    matrix_versions,
    resolution_kind,
    resolve,
    resolve_config,
    resolve_language,
    resolve_repo,
)
from vergil_tooling.lib.versions.window import WINDOW_ENV, WindowError, parse_window

if TYPE_CHECKING:
    from pathlib import Path


def _family(key: str, prefix: str, versions: list[str], *, runtime: bool | None = None) -> Any:
    return {
        "key": key,
        "token_prefix": prefix,
        "versions": [
            {"version": v, "eol": None, "in_policy": True, "runtime_published": runtime}
            for v in versions
        ],
    }


_WINDOW_DOC: dict[str, Any] = {
    "schema": 1,
    "languages": {
        "python": {
            "families": [_family("python", "", ["3.15", "3.14", "3.13", "3.12"], runtime=True)]
        },
        "go": {"families": [_family("go", "", ["1.27", "1.26"])]},
        "cpp": {
            "families": [
                _family("cpp-clang", "clang-", ["20", "19"]),
                _family("cpp-gcc", "gcc-", ["14"]),
            ]
        },
        "typescript": {"families": [_family("ts-node", "node-", ["24", "22"])]},
    },
}

WINDOW = parse_window(json.dumps(_WINDOW_DOC).encode(), source="fixture-window")


def _cfg(
    language: str | None = "python",
    *,
    repo_type: str = "application",
    versions: list[str] | None = None,
    primary: str | None = None,
) -> VergilConfig:
    ci: dict[str, Any] = {}
    if versions is not None:
        ci["versions"] = versions
    if primary is not None:
        ci["primary-version"] = primary
    project: dict[str, Any] = {
        "repository-type": repo_type,
        "versioning-scheme": "semver",
        "branching-model": "library-release",
        "release-model": "tagged-release",
    }
    if language is not None:
        project["primary-language"] = language
    return _parse_raw_config({"project": project, "ci": ci, "dependencies": {"vergil": "v2.1"}})


def _resolve(cfg: VergilConfig) -> Resolution:
    result = resolve(cfg, WINDOW, repo_root=None)
    assert result is not None
    return result


# -- kind --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo_type", "kind"),
    [
        ("library", "library"),
        ("application", "app"),
        ("tooling", "app"),
        ("infrastructure", "app"),
        ("documentation", "app"),
    ],
)
def test_resolution_kind(repo_type: str, kind: str) -> None:
    assert resolution_kind(repo_type) == kind


# -- no pin ------------------------------------------------------------------


def test_library_no_pin_gets_whole_window() -> None:
    result = _resolve(_cfg(repo_type="library"))
    assert result.kind == "library"
    assert result.versions == ("3.15", "3.14", "3.13", "3.12")
    assert result.primary == "3.15"


def test_app_no_pin_gets_newest() -> None:
    result = _resolve(_cfg())
    assert result.kind == "app"
    assert result.versions == ("3.15",)
    assert result.primary == "3.15"
    assert result.container_tags == {"3.15": "3.15"}
    assert result.container_suffixes == {"3.15": "python"}


def test_cpp_unpinned_is_fatal() -> None:
    with pytest.raises(ResolutionError) as exc:
        _resolve(_cfg("cpp", repo_type="library"))
    message = str(exc.value)
    assert "cpp must pin [ci].versions until its lifecycle turn" in message
    assert 'add [ci].versions = ["clang-20"]' in message
    assert "clang-20, clang-19, gcc-14" in message


def test_prefixed_single_family_unpinned_is_fatal() -> None:
    # typescript has one family, but its tokens carry a prefix (node-24).
    with pytest.raises(ResolutionError, match="typescript must pin"):
        _resolve(_cfg("typescript"))


# -- pins --------------------------------------------------------------------


def test_pin_outside_window_is_fatal() -> None:
    with pytest.raises(ResolutionError) as exc:
        _resolve(_cfg("go", repo_type="library", versions=["1.25", "1.26"]))
    message = str(exc.value)
    assert "1.25" in message
    assert "1.27, 1.26" in message
    assert "fixture-window" in message
    assert "remove it from [ci].versions" in message


def test_library_pin_resolves_newest_first() -> None:
    result = _resolve(_cfg(repo_type="library", versions=["3.12", "3.14", "3.12"]))
    assert result.versions == ("3.14", "3.12")
    assert result.primary == "3.14"


def test_cpp_pin_tags_strip_prefix() -> None:
    result = _resolve(_cfg("cpp", repo_type="library", versions=["gcc-14", "clang-20"]))
    assert result.versions == ("clang-20", "gcc-14")
    assert result.container_tags == {"clang-20": "20", "gcc-14": "14"}
    assert result.container_suffixes == {"clang-20": "cpp-clang", "gcc-14": "cpp-gcc"}
    assert result.primary_container_tag == "20"
    assert result.primary_container_suffix == "cpp-clang"


def test_app_single_pin_is_clean() -> None:
    result = _resolve(_cfg(versions=["3.13"]))
    assert result.versions == ("3.13",)


def test_app_multi_pin_is_fatal() -> None:
    # Every caller (vrg-ci-resolve, vrg-validate, vrg-container-run, repo init)
    # resolves through here.
    with pytest.raises(ResolutionError) as exc:
        resolve(_cfg(repo_type="tooling", versions=["3.13", "3.14"]), WINDOW, repo_root=None)
    message = str(exc.value)
    assert "[ci].versions pins 2 versions (3.14, 3.13)" in message
    assert "repository-type 'tooling' resolves as an app" in message
    assert "builds exactly one version" in message
    assert "Fix: pin a single version (keep one entry in [ci].versions" in message
    assert 'set [project].repository-type = "library"' in message
    assert "installed there as a tool" in message


# -- primary-version ---------------------------------------------------------


def test_primary_override_is_honoured() -> None:
    result = _resolve(_cfg(repo_type="library", primary="3.13"))
    assert result.primary == "3.13"
    assert result.primary_container_tag == "3.13"


def test_primary_override_must_be_resolved() -> None:
    # An app resolves to 3.15 only, so a 3.13 primary names no resolved version.
    with pytest.raises(ResolutionError) as exc:
        _resolve(_cfg(primary="3.13"))
    message = str(exc.value)
    assert "[ci].primary-version 3.13 is not a resolved version (3.15)" in message


def test_primary_override_must_be_a_string() -> None:
    with pytest.raises(ConfigError):
        _parse_raw_config(
            {
                "project": {
                    "repository-type": "library",
                    "versioning-scheme": "semver",
                    "branching-model": "library-release",
                    "release-model": "tagged-release",
                },
                "ci": {"primary-version": 3},
                "dependencies": {"vergil": "v2.1"},
            }
        )


# -- language ----------------------------------------------------------------


def test_no_language_returns_none() -> None:
    assert resolve(_cfg(None), WINDOW, repo_root=None) is None


def test_language_absent_from_window_is_fatal() -> None:
    cfg = _cfg()
    cfg.project.primary_language = "ruby"
    with pytest.raises(ResolutionError) as exc:
        resolve(cfg, WINDOW, repo_root=None)
    message = str(exc.value)
    assert message.startswith("ruby is not in the supported window (fixture-window")
    assert "cpp, go, python, typescript" in message


def test_resolve_language_uses_the_given_language() -> None:
    result = resolve_language(_cfg(None), WINDOW, "go", repo_root=None)
    assert result.versions == ("1.27",)


def test_resolution_error_is_a_versions_error() -> None:
    assert issubclass(ResolutionError, VersionsError)


# -- extensions --------------------------------------------------------------


def test_extension_narrows_candidates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[ExtensionContext, tuple[str, ...]]] = []

    def drop_newest(ctx: ExtensionContext, candidates: tuple[str, ...]) -> tuple[str, ...]:
        seen.append((ctx, candidates))
        return candidates[1:]

    monkeypatch.setitem(EXTENSIONS, "python", drop_newest)
    cfg = _cfg()
    result = resolve(cfg, WINDOW, repo_root=tmp_path)

    assert result is not None
    # An app takes the newest candidate left after the extension narrows.
    assert result.versions == ("3.14",)
    ((ctx, candidates),) = seen
    assert candidates == ("3.15", "3.14", "3.13", "3.12")
    assert (ctx.cfg, ctx.window, ctx.language) == (cfg, WINDOW, "python")
    assert (ctx.kind, ctx.pinned, ctx.repo_root) == ("app", False, tmp_path)


def test_extension_sees_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    contexts: list[ExtensionContext] = []

    def keep(ctx: ExtensionContext, candidates: tuple[str, ...]) -> tuple[str, ...]:
        contexts.append(ctx)
        return candidates

    monkeypatch.setitem(EXTENSIONS, "python", keep)
    result = _resolve(_cfg(repo_type="library", versions=["3.13", "3.14"]))
    assert result.versions == ("3.14", "3.13")
    assert contexts[0].pinned is True


@pytest.mark.parametrize("returned", [(), ("3.11",)])
def test_extension_may_only_narrow(
    monkeypatch: pytest.MonkeyPatch, returned: tuple[str, ...]
) -> None:
    monkeypatch.setitem(EXTENSIONS, "python", lambda _ctx, _candidates: returned)
    with pytest.raises(ResolutionError, match="may only narrow them to a non-empty subset"):
        _resolve(_cfg())


def test_extension_registry_has_python_only() -> None:
    # Task 9 registered python; no other language narrows its candidates.
    assert set(EXTENSIONS) == {"python"}


# -- resolve_config / resolve_repo ---------------------------------------------


def _write_window(tmp_path: Path) -> Path:
    path = tmp_path / "window.json"
    path.write_text(json.dumps(_WINDOW_DOC))
    return path


def test_resolve_config_loads_the_window(tmp_path: Path) -> None:
    env = {WINDOW_ENV: str(_write_window(tmp_path))}
    result = resolve_config(_cfg(repo_type="library"), repo_root=None, env=env)
    assert result is not None
    assert result.versions == ("3.15", "3.14", "3.13", "3.12")


def test_resolve_config_no_language_skips_the_window() -> None:
    # No language: nothing to resolve, and the window is never read.
    env = {WINDOW_ENV: "/nonexistent/window.json"}
    assert resolve_config(_cfg(None), repo_root=None, env=env) is None


def test_resolve_config_detected_language_stands_in(tmp_path: Path) -> None:
    env = {WINDOW_ENV: str(_write_window(tmp_path))}
    result = resolve_config(_cfg(None), repo_root=None, language="go", env=env)
    assert result is not None
    assert result.versions == ("1.27",)


def test_resolve_config_window_failure_propagates(tmp_path: Path) -> None:
    env = {WINDOW_ENV: str(tmp_path / "absent.json")}
    with pytest.raises(WindowError):
        resolve_config(_cfg(), repo_root=None, env=env)


def _write_config(root: Path, body: str) -> None:
    (root / "vergil.toml").write_text(
        "[project]\n"
        'repository-type = "library"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        f"{body}"
        '[dependencies]\nvergil = "v2.1"\n'
    )


def test_resolve_repo_reads_vergil_toml(tmp_path: Path) -> None:
    _write_config(tmp_path, 'primary-language = "go"\n[ci]\n')
    env = {WINDOW_ENV: str(_write_window(tmp_path))}
    result = resolve_repo(tmp_path, env=env)
    assert result is not None
    assert result.versions == ("1.27", "1.26")


def test_resolve_repo_no_config_no_language_is_none(tmp_path: Path) -> None:
    assert resolve_repo(tmp_path) is None


def test_resolve_repo_no_config_with_language_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(ResolutionError, match=r"no vergil\.toml at .*Fix: add a vergil\.toml"):
        resolve_repo(tmp_path, language="python")


# -- outputs -----------------------------------------------------------------


def test_matrix_versions() -> None:
    assert matrix_versions(None) == ("latest",)
    assert matrix_versions(_resolve(_cfg())) == ("3.15",)


def test_ci_outputs_resolved() -> None:
    result = _resolve(_cfg("cpp", repo_type="library", versions=["clang-20", "gcc-14"]))
    assert ci_outputs(result) == {
        "versions": ["clang-20", "gcc-14"],
        "primary-version": "clang-20",
        "primary-container-tag": "20",
        "container-tags": {"clang-20": "20", "gcc-14": "14"},
        "container-suffixes": {"clang-20": "cpp-clang", "gcc-14": "cpp-gcc"},
    }


def test_ci_outputs_no_language_is_latest() -> None:
    assert ci_outputs(None) == {
        "versions": ["latest"],
        "primary-version": "latest",
        "primary-container-tag": "latest",
        "container-tags": {"latest": "latest"},
        "container-suffixes": {"latest": "base"},
    }
