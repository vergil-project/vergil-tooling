"""Tests for vergil_tooling.lib.container."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.container import (
    _DEFAULT_TEST_COMMANDS,
    _DEFAULT_VERSIONS,
    _NPM_GLOBAL_ROOT,
    assert_docker_available,
    build_container_args,
    build_docker_args,
    container_platform,
    default_image,
    detect_language,
    detect_runtime,
    docker_platform,
    resolve_language,
    workspace_mount_args,
    worktree_parent_gitdir,
)

if TYPE_CHECKING:
    from pathlib import Path


# -- detect_runtime -----------------------------------------------------------


class TestDetectRuntime:
    def test_prefers_nerdctl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        assert detect_runtime() == "nerdctl"

    def test_falls_back_to_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "shutil.which", lambda cmd: "/usr/bin/docker" if cmd == "docker" else None
        )
        assert detect_runtime() == "docker"

    def test_exits_if_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        with pytest.raises(SystemExit):
            detect_runtime()


# -- detect_language ----------------------------------------------------------


def test_detect_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert detect_language(tmp_path) == "python"


def test_detect_ruby(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    assert detect_language(tmp_path) == "ruby"


def test_detect_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example\n")
    assert detect_language(tmp_path) == "go"


def test_detect_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_language(tmp_path) == "rust"


def test_detect_java_pom(tmp_path: Path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>\n")
    assert detect_language(tmp_path) == "java"


def test_detect_java_mvnw(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/bash\n")
    assert detect_language(tmp_path) == "java"


def test_detect_none(tmp_path: Path) -> None:
    assert detect_language(tmp_path) == ""


def test_detect_ruby_priority(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("")
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_language(tmp_path) == "ruby"


def test_detect_cpp_conanfile_py(tmp_path: Path) -> None:
    # C++ needs BOTH a CMakeLists.txt and a conanfile — the pair is the marker.
    (tmp_path / "CMakeLists.txt").write_text("")
    (tmp_path / "conanfile.py").write_text("")
    assert detect_language(tmp_path) == "cpp"


def test_detect_cpp_conanfile_txt(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("")
    (tmp_path / "conanfile.txt").write_text("")
    assert detect_language(tmp_path) == "cpp"


def test_detect_cpp_requires_cmakelists(tmp_path: Path) -> None:
    # A conanfile alone (no CMakeLists.txt) is not enough to claim cpp.
    (tmp_path / "conanfile.py").write_text("")
    assert detect_language(tmp_path) == ""


def test_detect_cpp_requires_conanfile(tmp_path: Path) -> None:
    # A CMakeLists.txt alone (no conanfile) is not enough to claim cpp.
    (tmp_path / "CMakeLists.txt").write_text("")
    assert detect_language(tmp_path) == ""


def test_detect_typescript_tsconfig(tmp_path: Path) -> None:
    # A tsconfig.json is a sufficient TypeScript marker on its own.
    (tmp_path / "tsconfig.json").write_text("{}")
    assert detect_language(tmp_path) == "typescript"


def test_detect_typescript_package_json_dev_dep(tmp_path: Path) -> None:
    # A package.json carrying a `typescript` devDependency is a TypeScript marker.
    (tmp_path / "package.json").write_text('{"devDependencies": {"typescript": "^5.4.0"}}')
    assert detect_language(tmp_path) == "typescript"


def test_detect_typescript_package_json_without_ts_dep_not_detected(tmp_path: Path) -> None:
    # A plain (JS-only) package.json with no `typescript` devDependency must NOT
    # misdetect as TypeScript.
    (tmp_path / "package.json").write_text('{"devDependencies": {"eslint": "^9.0.0"}}')
    assert detect_language(tmp_path) == ""


def test_detect_typescript_bare_package_json_not_detected(tmp_path: Path) -> None:
    # A package.json with no devDependencies block at all is not a TS marker.
    (tmp_path / "package.json").write_text('{"name": "example"}')
    assert detect_language(tmp_path) == ""


def test_detect_typescript_malformed_package_json_not_detected(tmp_path: Path) -> None:
    # A malformed package.json must not crash detection nor claim TypeScript.
    (tmp_path / "package.json").write_text("{not json")
    assert detect_language(tmp_path) == ""


def test_detect_typescript_non_object_package_json_not_detected(tmp_path: Path) -> None:
    # A package.json that is valid JSON but not an object (e.g. an array) is not
    # a TypeScript marker and must not crash detection.
    (tmp_path / "package.json").write_text('["typescript"]')
    assert detect_language(tmp_path) == ""


# -- resolve_language ---------------------------------------------------------

# A minimal valid vergil.toml whose [project].primary-language we vary per test.
_RESOLVE_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "{lang}"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["{version}"]
"""


def _write_vergil(tmp_path: Path, *, lang: str, version: str = "3.14") -> None:
    (tmp_path / "vergil.toml").write_text(_RESOLVE_TOML.format(lang=lang, version=version))


def test_resolve_language_asserted_wins_without_markers(tmp_path: Path) -> None:
    # The bootstrap case (issue #2858): a repo declares cpp but has none of the
    # C++ filesystem markers yet. The asserted language must still resolve to cpp
    # so the C++ image is selected instead of the base image.
    _write_vergil(tmp_path, lang="cpp", version="clang-20")
    assert resolve_language(tmp_path) == "cpp"


def test_resolve_language_asserted_wins_over_conflicting_markers(tmp_path: Path) -> None:
    # An asserted language overrides a conflicting filesystem marker: the repo
    # declares go but also carries a pyproject.toml.
    _write_vergil(tmp_path, lang="go")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert resolve_language(tmp_path) == "go"


def test_resolve_language_falls_back_to_detection_without_config(tmp_path: Path) -> None:
    # No vergil.toml: fall back to filesystem detection.
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert resolve_language(tmp_path) == "python"


def test_resolve_language_falls_back_when_language_unset(tmp_path: Path) -> None:
    # A config that omits primary-language asserts nothing; detection wins.
    toml = (
        "[project]\n"
        'repository-type = "infrastructure"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        "\n[dependencies]\n"
        'vergil = "v2.0"\n'
        '\n[ci]\nversions = ["3.14"]\n'
    )
    (tmp_path / "vergil.toml").write_text(toml)
    (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
    assert resolve_language(tmp_path) == "ruby"


def test_resolve_language_empty_when_neither_asserted_nor_detected(tmp_path: Path) -> None:
    # No config and no markers: empty, exactly like detect_language.
    assert resolve_language(tmp_path) == ""


# -- default_image ------------------------------------------------------------


def test_default_image_known_lang() -> None:
    assert "prod-python" in default_image("python")


def test_default_image_unknown_no_fallback() -> None:
    assert default_image("unknown") == ""


def test_default_image_unknown_with_fallback() -> None:
    assert default_image("unknown", fallback=True) == "ghcr.io/vergil-project/prod-base:latest"


def test_default_image_empty_with_fallback() -> None:
    assert default_image("", fallback=True) == "ghcr.io/vergil-project/prod-base:latest"


# -- prefix-aware default_image -----------------------------------------------


def test_default_image_prod_prefix() -> None:
    img = default_image("python", prefix="prod")
    assert "prod-python" in img
    assert "dev-python" not in img


def test_default_image_dev_prefix() -> None:
    img = default_image("python", prefix="dev")
    assert "dev-python" in img


def test_default_image_default_prefix_is_prod() -> None:
    img = default_image("python")
    assert "prod-python" in img


def test_default_image_fallback_respects_prefix() -> None:
    img = default_image("unknown", fallback=True, prefix="prod")
    assert "prod-base" in img


def test_default_image_fallback_dev_prefix() -> None:
    img = default_image("unknown", fallback=True, prefix="dev")
    assert "dev-base" in img


# -- container_platform -------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
        ("x86_64", "linux/amd64"),
        ("AMD64", "linux/amd64"),
    ],
)
def test_container_platform_known(machine: str, expected: str) -> None:
    with patch("vergil_tooling.lib.container.platform.machine", return_value=machine):
        assert container_platform() == expected


def test_container_platform_unknown_defaults_to_amd64() -> None:
    with patch("vergil_tooling.lib.container.platform.machine", return_value="riscv64"):
        assert container_platform() == "linux/amd64"


# -- docker_platform alias ----------------------------------------------------


def test_docker_platform_alias() -> None:
    assert docker_platform is container_platform


def test_default_image_version_override() -> None:
    # The repo's declared [ci].versions primary wins over _DEFAULT_VERSIONS (#2468).
    assert default_image("python", version="3.12") == "ghcr.io/vergil-project/prod-python:3.12"


def test_default_image_version_none_uses_builtin_default() -> None:
    assert default_image("python", version=None) == "ghcr.io/vergil-project/prod-python:3.14"


def test_default_image_empty_version_uses_builtin_default() -> None:
    # An empty string is "no declared version" (falsy), not a literal tag.
    assert default_image("python", version="") == "ghcr.io/vergil-project/prod-python:3.14"


def test_default_image_no_language_falls_back_despite_declared_version() -> None:
    # A language-less repo (lang="") that declares [ci].versions must still fall
    # back to the base image, not build a malformed prod-:<ver> tag (#2475: the
    # declared version must not resurrect a per-language image when there is none).
    assert default_image("", fallback=True, version="latest") == (
        "ghcr.io/vergil-project/prod-base:latest"
    )


def test_default_image_no_language_no_fallback_ignores_version() -> None:
    assert default_image("", version="latest") == ""


# -- cpp compiler-family image resolution -------------------------------------


def test_default_image_cpp_uses_builtin_clang_default() -> None:
    # _DEFAULT_VERSIONS["cpp"] is the primary Clang tag (clang-20), so the
    # family rides the image suffix and the numeric major is the tag.
    assert default_image("cpp") == "ghcr.io/vergil-project/prod-cpp-clang:20"


def test_default_image_cpp_clang_declared_version() -> None:
    assert default_image("cpp", version="clang-20") == "ghcr.io/vergil-project/prod-cpp-clang:20"


def test_default_image_cpp_gcc_declared_version() -> None:
    # gcc-15 → prod-cpp-gcc:15, matching the image names T1/T2 produce.
    assert default_image("cpp", version="gcc-15") == "ghcr.io/vergil-project/prod-cpp-gcc:15"


def test_default_image_cpp_respects_prefix() -> None:
    assert default_image("cpp", prefix="dev", version="clang-20") == (
        "ghcr.io/vergil-project/dev-cpp-clang:20"
    )


def test_default_image_cpp_unparseable_version_no_fallback() -> None:
    # A malformed cpp version tag (no clang-/gcc- prefix) must not build a
    # malformed prod-cpp-:<ver> image — it returns empty without fallback.
    assert default_image("cpp", version="latest") == ""


def test_default_image_cpp_unparseable_version_with_fallback() -> None:
    assert default_image("cpp", version="latest", fallback=True) == (
        "ghcr.io/vergil-project/prod-base:latest"
    )


def test_cpp_default_version_is_primary_clang() -> None:
    # The built-in default drives default_image("cpp") when no [ci].versions
    # is declared — it must be the primary Clang tag.
    assert _DEFAULT_VERSIONS["cpp"] == "clang-20"


def test_cpp_default_test_command_is_conan_cmake_ctest() -> None:
    # vrg-container-test runs this via `bash -c`, so `&&` chaining is valid.
    cmd = _DEFAULT_TEST_COMMANDS["cpp"]
    assert "conan install" in cmd
    assert "cmake" in cmd
    assert "ctest" in cmd
    # Conan must resolve deps in the same build_type as the CMake Debug build,
    # or the Debug config finds no matching binary (fmt/format.h not found). (#2572)
    assert "conan install . -s build_type=Debug --build=missing" in cmd
    assert "-DCMAKE_BUILD_TYPE=Debug" in cmd


# -- typescript node image resolution -----------------------------------------


def test_default_image_typescript_uses_builtin_node_default() -> None:
    # _DEFAULT_VERSIONS["typescript"] is the primary Node tag (node-24), so the
    # runtime family rides the image suffix and the numeric major is the tag.
    assert default_image("typescript") == "ghcr.io/vergil-project/prod-ts-node:24"


def test_default_image_typescript_node24_declared_version() -> None:
    assert default_image("typescript", version="node-24") == (
        "ghcr.io/vergil-project/prod-ts-node:24"
    )


def test_default_image_typescript_node22_declared_version() -> None:
    # node-22 → prod-ts-node:22, matching the second image T2 produces.
    assert default_image("typescript", version="node-22") == (
        "ghcr.io/vergil-project/prod-ts-node:22"
    )


def test_default_image_typescript_respects_prefix() -> None:
    assert default_image("typescript", prefix="dev", version="node-24") == (
        "ghcr.io/vergil-project/dev-ts-node:24"
    )


def test_default_image_typescript_unparseable_version_no_fallback() -> None:
    # A malformed node tag (no node- prefix) must not build a malformed
    # prod-ts-node: image — it returns empty without fallback.
    assert default_image("typescript", version="latest") == ""


def test_default_image_typescript_empty_major_no_fallback() -> None:
    # A `node-` prefix with no major must not build prod-ts-node: with an empty
    # major — it falls back like an unknown language.
    assert default_image("typescript", version="node-") == ""


def test_default_image_typescript_unparseable_version_with_fallback() -> None:
    assert default_image("typescript", version="latest", fallback=True) == (
        "ghcr.io/vergil-project/prod-base:latest"
    )


def test_typescript_default_version_is_primary_node() -> None:
    # The built-in default drives default_image("typescript") when no [ci].versions
    # is declared — it must be the primary Node tag.
    assert _DEFAULT_VERSIONS["typescript"] == "node-24"


def test_typescript_default_test_command_is_npm_ci_vitest() -> None:
    # vrg-container-test runs this via `bash -c`, so `&&` chaining is valid.
    cmd = _DEFAULT_TEST_COMMANDS["typescript"]
    assert "npm ci" in cmd
    assert "vitest run" in cmd


# -- workspace_mount_args -----------------------------------------------------


def test_workspace_mount_args_python_masks_venv(tmp_path: Path) -> None:
    # A Python repo gets the bind mount, workdir, AND the anonymous `.venv`
    # mask together — one source of truth shared by the run and cache-build
    # paths so a mount site can't reintroduce host-venv corruption (#2495).
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    args = workspace_mount_args(tmp_path)
    assert args == ["-v", f"{tmp_path}:/workspace", "-w", "/workspace", "-v", "/workspace/.venv"]


def test_workspace_mount_args_non_python_omits_venv_mask(tmp_path: Path) -> None:
    # A non-Python repo has no `.venv` to protect, so no mask is added (#2495).
    (tmp_path / "go.mod").write_text("module example\n")
    args = workspace_mount_args(tmp_path)
    assert args == ["-v", f"{tmp_path}:/workspace", "-w", "/workspace"]
    assert "/workspace/.venv" not in args


def test_workspace_mount_args_asserted_python_masks_venv_without_markers(tmp_path: Path) -> None:
    # The venv mask keys off the asserted language too (issue #2858): a repo that
    # declares python but has no pyproject.toml marker yet still gets the mask, so
    # the mount decision matches the image the same asserted language selects.
    _write_vergil(tmp_path, lang="python")
    args = workspace_mount_args(tmp_path)
    assert "/workspace/.venv" in args


# -- build_container_args -----------------------------------------------------


def test_build_container_args_basic(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["echo", "hello"], runtime="docker")
    assert args[0:3] == ["docker", "run", "--rm"]
    assert any(a.startswith("--platform=linux/") for a in args)
    assert "--pull=always" in args
    assert f"{tmp_path}:/workspace" in args
    assert "img:1" in args
    assert args[-2:] == ["echo", "hello"]


def test_build_container_args_nerdctl(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["echo", "hello"], runtime="nerdctl")
    assert args[0:3] == ["nerdctl", "run", "--rm"]


def _env_value(args: list[str], name: str) -> str | None:
    """Return the value passed via ``-e NAME=value``, or None if absent."""
    for i, a in enumerate(args):
        if a == "-e" and i + 1 < len(args) and args[i + 1].startswith(f"{name}="):
            return args[i + 1].split("=", 1)[1]
    return None


def test_build_container_args_defaults_uv_link_mode_copy(tmp_path: Path) -> None:
    # The uv cache and the /workspace venv target are on different filesystems,
    # so uv falls back to copy; we pin it to copy to silence the warning (#2461).
    with patch.dict("os.environ", {}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert _env_value(args, "UV_LINK_MODE") == "copy"


def test_build_container_args_respects_host_uv_link_mode(tmp_path: Path) -> None:
    # An operator's explicit UV_LINK_MODE wins over the copy default.
    with patch.dict("os.environ", {"UV_LINK_MODE": "hardlink"}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert _env_value(args, "UV_LINK_MODE") == "hardlink"


def test_build_container_args_masks_venv_for_python(tmp_path: Path) -> None:
    # A Python repo gets an anonymous volume masking the bind-mounted host
    # `.venv`, so in-container `uv sync` builds a throwaway venv that cannot
    # corrupt the host venv (#2486).
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with patch.dict("os.environ", {}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert "/workspace/.venv" in args
    idx = args.index("/workspace/.venv")
    assert args[idx - 1] == "-v"


def test_build_container_args_omits_venv_mask_for_non_python(tmp_path: Path) -> None:
    # A non-Python repo has no `.venv` to protect, so no mask is added (#2486).
    (tmp_path / "go.mod").write_text("module example\n")
    with patch.dict("os.environ", {}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert "/workspace/.venv" not in args


_TOML_WITH_BUILD_COMMAND = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "typescript"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["node-24"]

[container]
env-prefixes = []
build-command = "npm install -g is-number"
"""


def test_build_container_args_sets_node_path_when_build_command_declared(
    tmp_path: Path,
) -> None:
    # A [container].build-command global install bakes a library outside
    # /workspace but off Node's default require path; NODE_PATH points at the
    # npm global root so it resolves (#2781).
    (tmp_path / "vergil.toml").write_text(_TOML_WITH_BUILD_COMMAND)
    with patch.dict("os.environ", {}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert _env_value(args, "NODE_PATH") == _NPM_GLOBAL_ROOT
    assert _NPM_GLOBAL_ROOT == "/usr/lib/node_modules"


def test_build_container_args_no_node_path_without_build_command(tmp_path: Path) -> None:
    # No build-command ⇒ behaviour is byte-identical to before: no NODE_PATH is
    # injected for any repo that declares none (#2781).
    baseline = None
    with patch.dict("os.environ", {}, clear=True):
        baseline = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert _env_value(baseline, "NODE_PATH") is None
    assert "NODE_PATH" not in " ".join(baseline)


def test_build_container_args_no_node_path_when_config_has_no_build_command(
    tmp_path: Path,
) -> None:
    # A vergil.toml present, with a [container] table but no build-command, is
    # still unchanged — the gate is the build-command, not the table.
    (tmp_path / "vergil.toml").write_text(
        _TOML_WITH_BUILD_COMMAND.replace('build-command = "npm install -g is-number"\n', "")
    )
    with patch.dict("os.environ", {}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert _env_value(args, "NODE_PATH") is None


def test_build_container_args_host_node_path_overrides_default(tmp_path: Path) -> None:
    # An explicit host NODE_PATH wins over the npm-global-root default, so a
    # consumer can point resolution elsewhere (#2781).
    (tmp_path / "vergil.toml").write_text(_TOML_WITH_BUILD_COMMAND)
    with patch.dict("os.environ", {"NODE_PATH": "/custom/modules"}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert _env_value(args, "NODE_PATH") == "/custom/modules"


def test_build_container_args_host_node_path_ignored_without_build_command(
    tmp_path: Path,
) -> None:
    # The gate is the declared build-command, not the host env: a host NODE_PATH
    # alone does not inject the flag for a repo that declares no build-command.
    with patch.dict("os.environ", {"NODE_PATH": "/custom/modules"}, clear=True):
        args = build_container_args(tmp_path, "img:1", ["cmd"], runtime="docker")
    assert _env_value(args, "NODE_PATH") is None


def test_build_docker_args_basic(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["echo", "hello"])
    assert args[0:3] == ["docker", "run", "--rm"]
    assert any(a.startswith("--platform=linux/") for a in args)
    assert "--pull=always" in args
    assert f"{tmp_path}:/workspace" in args
    assert "img:1" in args
    assert args[-2:] == ["echo", "hello"]


def test_build_docker_args_network(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"DOCKER_NETWORK": "mynet"}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "--network" in args
    assert "mynet" in args


def test_build_docker_args_extra_volumes(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"DOCKER_EXTRA_VOLUMES": "/a:/b;/c:/d"}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "/a:/b" in args
    assert "/c:/d" in args


def test_build_docker_args_empty_extra_volumes(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {"DOCKER_EXTRA_VOLUMES": ";"}, clear=True),
        patch("vergil_tooling.lib.container.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    v_indices = [i for i, a in enumerate(args) if a == "-v"]
    assert len(v_indices) == 1


def test_build_docker_args_env_passthrough(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "GH_TOKEN": "tok", "OTHER": "x"}
    with patch.dict("os.environ", env, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"], env_prefixes=("MQ_",))
    assert "MQ_HOST" in args
    assert "GH_TOKEN" not in args
    assert "OTHER" not in args


def test_build_docker_args_no_prefixes_no_passthrough(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "GH_TOKEN": "tok", "GITHUB_SHA": "abc"}
    with patch.dict("os.environ", env, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "MQ_HOST" not in args
    assert "GH_TOKEN" not in args
    assert "GITHUB_SHA" not in args


def test_build_docker_args_no_unrelated_env(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"HOME": "/home/user"}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"], env_prefixes=("MQ_",))
    assert "HOME" not in args


def test_build_docker_args_mounts_gitconfig(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    gitconfig = fake_home / ".gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert f"{gitconfig}:/root/.gitconfig:ro" in args


def test_build_docker_args_mounts_ssh_dir(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("key\n")
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert f"{ssh_dir}:/root/.ssh:ro" in args


def test_build_docker_args_no_ssh_dir(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert all("/root/.ssh" not in a for a in args)


def test_build_docker_args_no_gitconfig(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert all("/root/.gitconfig" not in a for a in args)


# -- assert_docker_available --------------------------------------------------


def test_assert_docker_available_success() -> None:
    mock_result = MagicMock(returncode=0)
    with patch("vergil_tooling.lib.container.subprocess.run", return_value=mock_result):
        assert_docker_available()  # should not raise


def test_assert_docker_available_failure() -> None:
    mock_result = MagicMock(returncode=1)
    with (
        patch("vergil_tooling.lib.container.subprocess.run", return_value=mock_result),
        pytest.raises(SystemExit),
    ):
        assert_docker_available()


def test_assert_docker_available_not_installed() -> None:
    with (
        patch("vergil_tooling.lib.container.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(SystemExit),
    ):
        assert_docker_available()


def test_assert_docker_available_timeout() -> None:
    with (
        patch(
            "vergil_tooling.lib.container.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker version", timeout=15),
        ),
        pytest.raises(SystemExit),
    ):
        assert_docker_available()


# -- worktree_parent_gitdir ---------------------------------------------------


def test_worktree_parent_gitdir_main_worktree(tmp_path: Path) -> None:
    """Main worktree has `.git` as a directory; returns None."""
    (tmp_path / ".git").mkdir()
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_no_git_at_all(tmp_path: Path) -> None:
    """No `.git` present; returns None (defensive)."""
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_secondary_worktree(tmp_path: Path) -> None:
    """`.git` file points at <parent>/.git/worktrees/<name>; returns parent .git."""
    parent_git = tmp_path / "main-repo" / ".git"
    parent_git.mkdir(parents=True)
    worktree_metadata = parent_git / "worktrees" / "issue-1-x"
    worktree_metadata.mkdir(parents=True)
    worktree = tmp_path / "main-repo" / ".worktrees" / "issue-1-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {worktree_metadata}\n", encoding="utf-8")

    assert worktree_parent_gitdir(worktree) == parent_git


def test_worktree_parent_gitdir_malformed_no_gitdir_prefix(tmp_path: Path) -> None:
    """Unexpected file content; returns None (don't crash)."""
    (tmp_path / ".git").write_text("not a real gitdir pointer\n", encoding="utf-8")
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_unrecognized_layout(tmp_path: Path) -> None:
    """`.git` points somewhere that isn't `<parent>/worktrees/<name>`; returns None."""
    target = tmp_path / "elsewhere" / "custom-path"
    target.mkdir(parents=True)
    (tmp_path / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    assert worktree_parent_gitdir(tmp_path) is None


def test_worktree_parent_gitdir_oserror_on_read(tmp_path: Path) -> None:
    """Unreadable `.git` file (permissions, race, etc.) returns None safely."""
    (tmp_path / ".git").write_text("gitdir: /irrelevant\n", encoding="utf-8")
    with patch(
        "vergil_tooling.lib.container.Path.read_text",
        side_effect=OSError("permission denied"),
    ):
        assert worktree_parent_gitdir(tmp_path) is None


def test_build_docker_args_mounts_parent_git_when_worktree(tmp_path: Path) -> None:
    """Issue #293: secondary worktree triggers an extra parent-.git mount
    so the worktree's `.git` gitdir pointer resolves inside the container.
    """
    parent_git = tmp_path / "main-repo" / ".git"
    parent_git.mkdir(parents=True)
    worktree_metadata = parent_git / "worktrees" / "issue-1-x"
    worktree_metadata.mkdir(parents=True)
    worktree = tmp_path / "main-repo" / ".worktrees" / "issue-1-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {worktree_metadata}\n", encoding="utf-8")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(worktree, "img:1", ["cmd"])

    assert f"{parent_git}:{parent_git}" in args


def test_build_docker_args_no_extra_mount_for_main_worktree(tmp_path: Path) -> None:
    """Main worktree (`.git` is a directory) gets no extra mount."""
    (tmp_path / ".git").mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch("vergil_tooling.lib.container.Path.home", return_value=fake_home),
    ):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])

    # Only the workspace mount; no parent-.git mount.
    v_indices = [i for i, a in enumerate(args) if a == "-v"]
    assert len(v_indices) == 1
    assert args[v_indices[0] + 1] == f"{tmp_path}:/workspace"


# -- pull policy --------------------------------------------------------------


def test_build_docker_args_pull_always_by_default(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "--pull=always" in args


def test_build_docker_args_pull_never_omits_pull_flag(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"], pull_policy="never")
    assert "--pull=always" not in args
    assert not any(a.startswith("--pull=") for a in args)
