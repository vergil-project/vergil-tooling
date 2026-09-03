"""Tests for vergil_tooling.lib.lang_scaffold (epic vergil-project/.github#342, T2).

The born-green language-skeleton phase: render per-language skeleton templates,
stamp them idempotently, and drive the containerized lock-resolve + verify. cpp
is the first (and only) language implemented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import lang_scaffold
from vergil_tooling.lib.lang_scaffold import ScaffoldError
from vergil_tooling.lib.repo_init import RepoInitContext

if TYPE_CHECKING:
    from pathlib import Path


def _fake_ctx(tmp_path: Path, *, language: str, repo_name: str = "demo") -> RepoInitContext:
    """Build a minimal RepoInitContext rooted at *tmp_path* for scaffold tests."""
    ctx = RepoInitContext(org="acme", name=repo_name)
    ctx.primary_language = language
    ctx.work_dir = tmp_path
    return ctx


# -- 2b: name sanitizer -------------------------------------------------------


def test_sanitize_project_name() -> None:
    assert lang_scaffold.sanitize_project_name("mq-protocol-gateway") == "mq_protocol_gateway"
    assert lang_scaffold.sanitize_project_name("Foo.Bar") == "foo_bar"


# -- 2c: skeleton templates + render ------------------------------------------


def test_render_skeleton_cpp() -> None:
    files = lang_scaffold.render_skeleton("cpp", "mq_protocol_gateway")
    assert set(files) == {
        "conanfile.txt",
        "CMakeLists.txt",
        "src/mq_protocol_gateway.hpp",
        "src/mq_protocol_gateway.cpp",
        "tests/mq_protocol_gateway_test.cpp",
    }
    assert "project(mq_protocol_gateway CXX)" in files["CMakeLists.txt"]
    assert "CMAKE_PREFIX_PATH" not in files["CMakeLists.txt"]  # clean, #2912-based


def test_render_skeleton_unknown_language_empty() -> None:
    assert lang_scaffold.render_skeleton("go", "x") == {}


def test_render_skeleton_substitutes_name_in_sources() -> None:
    files = lang_scaffold.render_skeleton("cpp", "mq_protocol_gateway")
    # The source unit and test reference the sanitized name/namespace.
    assert "namespace mq_protocol_gateway" in files["src/mq_protocol_gateway.hpp"]
    assert '#include "mq_protocol_gateway.hpp"' in files["src/mq_protocol_gateway.cpp"]
    assert "mq_protocol_gateway::toolchain_ready()" in files["tests/mq_protocol_gateway_test.cpp"]
    # conanfile.txt is fixed (no name substitution) — gtest pinned, gmock off.
    assert "gtest/1.15.0" in files["conanfile.txt"]
    assert "gtest/*:build_gmock=False" in files["conanfile.txt"]


# -- 2d: write skeleton (idempotent, greenfield) ------------------------------


def test_write_stamps_missing_only(tmp_path: Path) -> None:
    (tmp_path / "conanfile.txt").write_text("CUSTOM")
    lang_scaffold._write_skeleton(tmp_path, {"conanfile.txt": "TEMPLATE", "CMakeLists.txt": "X"})
    assert (tmp_path / "conanfile.txt").read_text() == "CUSTOM"  # never clobbered
    assert (tmp_path / "CMakeLists.txt").read_text() == "X"  # missing → stamped


def test_write_force_restamps(tmp_path: Path) -> None:
    (tmp_path / "conanfile.txt").write_text("CUSTOM")
    lang_scaffold._write_skeleton(tmp_path, {"conanfile.txt": "TEMPLATE"}, force=True)
    assert (tmp_path / "conanfile.txt").read_text() == "TEMPLATE"


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    lang_scaffold._write_skeleton(tmp_path, {"src/deep/unit.cpp": "BODY"})
    assert (tmp_path / "src" / "deep" / "unit.cpp").read_text() == "BODY"


# -- 2e: container precondition + orchestration -------------------------------


def test_scaffold_refuses_without_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _fake_ctx(tmp_path, language="cpp")
    monkeypatch.setattr(lang_scaffold, "container_runtime_available", lambda: False)
    with pytest.raises(ScaffoldError, match="requires a container runtime"):
        lang_scaffold.scaffold_language(ctx)
    assert not (tmp_path / "CMakeLists.txt").exists()  # nothing written


def test_scaffold_cpp_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _fake_ctx(tmp_path, language="cpp", repo_name="mq-protocol-gateway")
    monkeypatch.setattr(lang_scaffold, "container_runtime_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        lang_scaffold, "_run_in_container", lambda root, cmd: calls.append(cmd) or 0
    )
    lang_scaffold.scaffold_language(ctx)
    assert (tmp_path / "CMakeLists.txt").exists()
    assert (tmp_path / "src" / "mq_protocol_gateway.cpp").exists()
    # resolve then verify, in that order.
    assert ["conan", "lock", "create", ".", "-s", "build_type=Debug"] in calls
    assert ["vrg-validate"] in calls
    assert calls.index(["conan", "lock", "create", ".", "-s", "build_type=Debug"]) < calls.index(
        ["vrg-validate"]
    )


def test_scaffold_noop_for_lockless_skeletonless_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _fake_ctx(tmp_path, language="go")
    # A language with no lock command and no skeleton does nothing — not even a
    # container-runtime check.
    monkeypatch.setattr(
        lang_scaffold,
        "container_runtime_available",
        lambda: pytest.fail("must not probe the runtime for a no-op language"),
    )
    called: list[object] = []
    monkeypatch.setattr(lang_scaffold, "_run_in_container", lambda root, cmd: called.append(cmd))
    lang_scaffold.scaffold_language(ctx)
    assert called == []


def test_scaffold_no_language_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _fake_ctx(tmp_path, language="")
    monkeypatch.setattr(lang_scaffold, "_run_in_container", lambda root, cmd: pytest.fail("no-op"))
    lang_scaffold.scaffold_language(ctx)


def test_scaffold_writes_skeleton_without_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hypothetical skeleton-only, lockless language: the skeleton is stamped
    # but no container resolve/verify runs (the runtime is never even probed).
    ctx = _fake_ctx(tmp_path, language="go")
    monkeypatch.setattr(
        lang_scaffold, "render_skeleton", lambda lang, project: {"unit.txt": "body"}
    )
    monkeypatch.setattr(
        lang_scaffold,
        "container_runtime_available",
        lambda: pytest.fail("a lockless skeleton must not probe the runtime"),
    )
    monkeypatch.setattr(
        lang_scaffold,
        "_run_in_container",
        lambda root, cmd: pytest.fail("a lockless skeleton must not touch the container"),
    )
    lang_scaffold.scaffold_language(ctx)
    assert (tmp_path / "unit.txt").read_text() == "body"


def test_scaffold_raises_when_resolve_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _fake_ctx(tmp_path, language="cpp", repo_name="demo")
    monkeypatch.setattr(lang_scaffold, "container_runtime_available", lambda: True)
    monkeypatch.setattr(lang_scaffold, "_run_in_container", lambda root, cmd: 1)
    with pytest.raises(ScaffoldError, match="conan lock create"):
        lang_scaffold.scaffold_language(ctx)


def test_scaffold_raises_when_verify_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _fake_ctx(tmp_path, language="cpp", repo_name="demo")
    monkeypatch.setattr(lang_scaffold, "container_runtime_available", lambda: True)

    def _run(root: Path, cmd: list[str]) -> int:
        return 0 if cmd[0] == "conan" else 1  # resolve ok, verify fails

    monkeypatch.setattr(lang_scaffold, "_run_in_container", _run)
    with pytest.raises(ScaffoldError, match="vrg-validate"):
        lang_scaffold.scaffold_language(ctx)


# -- helpers: container_runtime_available / _run_in_container ------------------


def test_container_runtime_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lang_scaffold.container, "detect_runtime", lambda: "docker")
    assert lang_scaffold.container_runtime_available() is True


def test_container_runtime_available_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise SystemExit(1)

    monkeypatch.setattr(lang_scaffold.container, "detect_runtime", _boom)
    assert lang_scaffold.container_runtime_available() is False


def test_run_in_container_shells_to_vrg_container_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(cmd, cwd, check):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["check"] = check

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(lang_scaffold.subprocess, "run", _fake_run)
    rc = lang_scaffold._run_in_container(tmp_path, ["conan", "lock", "create", "."])
    assert rc == 0
    assert captured["cmd"] == [
        "vrg-container-run",
        "--",
        "conan",
        "lock",
        "create",
        ".",
    ]
    assert captured["cwd"] == tmp_path
    assert captured["check"] is False
