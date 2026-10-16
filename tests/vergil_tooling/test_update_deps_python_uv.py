from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.update_deps import pypi
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updaters import python_uv
from vergil_tooling.lib.update_deps.updaters.python_uv import PythonUvUpdater

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.update_deps.updaters.python_uv"


def _ctx(root: Path, worktree: Path | None = None) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=root, worktree_path=worktree)


def _hb(name: str, current: str, latest: str, note: str) -> HeldBack:
    return HeldBack(source="python", name=name, current=current, latest=latest, note=note)


def _write_project(root: Path) -> None:
    (root / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["a<2"]\n')
    (root / "uv.lock").write_text('[[package]]\nname = "a"\nversion = "1.0"\n')


def test_applies_true_when_pyproject_and_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("")
    assert PythonUvUpdater().applies(_ctx(tmp_path)) is True


def test_applies_false_without_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert PythonUvUpdater().applies(_ctx(tmp_path)) is False


def test_default_latest_is_pypi() -> None:
    assert PythonUvUpdater()._latest is pypi.latest_version


def test_apply_reports_changed_when_lock_dirty(tmp_path: Path, monkeypatch) -> None:
    _write_project(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: " M uv.lock")
    result = PythonUvUpdater(latest=lambda n: "1.5").apply(_ctx(tmp_path))
    assert calls == [["vrg-container-run", "--", "uv", "lock", "--upgrade"]]
    assert result.changed is True
    assert result.commit_message == "chore(deps): uv lock --upgrade"
    assert result.held_back == []


def test_apply_reports_unchanged_when_lock_clean(tmp_path: Path, monkeypatch) -> None:
    _write_project(tmp_path)
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: "")
    result = PythonUvUpdater(latest=lambda n: "1.5").apply(_ctx(tmp_path))
    assert result.changed is False


def test_apply_attaches_held_back(tmp_path: Path, monkeypatch) -> None:
    _write_project(tmp_path)
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: "")
    result = PythonUvUpdater(latest=lambda n: "2.0").apply(_ctx(tmp_path))
    assert result.held_back == [_hb("a", "1.0", "2.0", "project.dependencies: a<2")]


def test_apply_reads_the_upgraded_worktree(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    worktree = tmp_path / "wt"
    root.mkdir()
    worktree.mkdir()
    _write_project(worktree)  # root has no manifests: the worktree must be read
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: "")
    result = PythonUvUpdater(latest=lambda n: "2.0").apply(_ctx(root, worktree))
    assert [e.name for e in result.held_back] == ["a"]


# -- held-back report (spec §7.3, §8.1) --------------------------------------

_PYPROJECT: dict[str, Any] = {
    "build-system": {"requires": ["setuptools>=68", "wheel"]},
    "project": {
        "dependencies": [
            "rich>=13.0",
            "pyyaml>=6.0,<7",
            'foo[bar]>=1,<2; python_version < "3.13"',
        ],
    },
    "dependency-groups": {"dev": ["pytest<9", {"include-group": "lint"}], "lint": ["ruff"]},
    "tool": {"uv": {"constraint-dependencies": ["msgpack>=1.2.1,<1.3"]}},
}
_LOCK: dict[str, Any] = {
    "package": [
        {"name": "rich", "version": "14.0.0"},
        {"name": "pyyaml", "version": "6.0.2"},
        {"name": "foo", "version": "1.4.0"},
        {"name": "pytest", "version": "8.4.1"},
        {"name": "ruff", "version": "0.12.0"},
        {"name": "msgpack", "version": "1.2.3"},
        {"name": "transitive-only", "version": "1.0.0"},
    ]
}
_LATEST = {
    "rich": "14.0.0",
    "pyyaml": "7.0.0",
    "foo": "2.0.0",
    "pytest": "9.0.1",
    "ruff": "0.13.0",
    "msgpack": "1.3.0",
    "setuptools": "80.0.0",
    "wheel": "0.45.0",
    "transitive-only": "9.0.0",
}


def test_direct_requirements_shapes() -> None:
    reqs = python_uv.direct_requirements(
        {
            "project": {
                "dependencies": [
                    'foo[bar]>=1; python_version < "3.13"',
                    "local @ https://example.invalid/local.whl",
                ]
            },
            "dependency-groups": {"dev": [{"include-group": "x"}, "pytest"]},
        }
    )
    # URL requirement and include-group table skipped; extras+marker parsed by name.
    assert [(r.name, where) for r, where in reqs] == [
        ("foo", "project.dependencies"),
        ("pytest", "dependency-groups.dev"),
    ]


def test_direct_requirements_empty_pyproject() -> None:
    assert python_uv.direct_requirements({}) == []


def test_direct_requirements_covers_every_table() -> None:
    reqs = python_uv.direct_requirements(_PYPROJECT)
    assert [where for _, where in reqs] == [
        "project.dependencies",
        "project.dependencies",
        "project.dependencies",
        "dependency-groups.dev",
        "dependency-groups.lint",
        "build-system.requires",
        "build-system.requires",
        "tool.uv.constraint-dependencies",
    ]


def test_direct_requirements_invalid_fails_loud() -> None:
    with pytest.raises(UpdateDepsError, match=r"dependency-groups\.dev.*not a valid"):
        python_uv.direct_requirements({"dependency-groups": {"dev": ["!!nope"]}})


def test_locked_versions_canonicalizes() -> None:
    assert python_uv.locked_versions({"package": [{"name": "PyYAML", "version": "6.0.2"}]}) == {
        "pyyaml": "6.0.2"
    }


def test_locked_versions_skips_versionless_packages() -> None:
    # The editable root of a dynamic-version project has no version in uv.lock.
    lock = {
        "package": [
            {"name": "root", "source": {"editable": "."}},
            {"name": "a", "version": "1"},
        ]
    }
    assert python_uv.locked_versions(lock) == {"a": "1"}


def test_locked_versions_empty_lock() -> None:
    assert python_uv.locked_versions({}) == {}


def test_held_back_reports_only_direct_deps_outside_bounds() -> None:
    entries = python_uv.held_back(_PYPROJECT, _LOCK, latest=lambda n: _LATEST[n])
    assert entries == [
        _hb("pyyaml", "6.0.2", "7.0.0", "project.dependencies: pyyaml<7,>=6.0"),
        _hb(
            "foo",
            "1.4.0",
            "2.0.0",
            'project.dependencies: foo[bar]<2,>=1; python_version < "3.13"',
        ),
        _hb("pytest", "8.4.1", "9.0.1", "dependency-groups.dev: pytest<9"),
        _hb(
            "msgpack",
            "1.2.3",
            "1.3.0",
            "tool.uv.constraint-dependencies: msgpack<1.3,>=1.2.1",
        ),
    ]
    assert all(e.name != "transitive-only" for e in entries)


def test_held_back_skips_unbounded_requirements_without_lookup() -> None:
    looked_up: list[str] = []

    def latest(name: str) -> str:
        looked_up.append(name)
        return "99"

    assert python_uv.held_back({"project": {"dependencies": ["ruff"]}}, {}, latest=latest) == []
    assert looked_up == []


def test_held_back_canonicalizes_names() -> None:
    entries = python_uv.held_back(
        {"project": {"dependencies": ["Py_YAML<7"]}},
        {"package": [{"name": "py-yaml", "version": "6.0"}]},
        latest=lambda n: {"py-yaml": "7.0"}[n],
    )
    assert entries == [_hb("py-yaml", "6.0", "7.0", "project.dependencies: Py_YAML<7")]


def test_held_back_unlocked_build_requirement() -> None:
    entries = python_uv.held_back(
        {"build-system": {"requires": ["setuptools<70"]}},
        {"package": []},
        latest=lambda n: "80.0.0",
    )
    assert entries == [
        _hb("setuptools", "(not locked)", "80.0.0", "build-system.requires: setuptools<70"),
    ]


def test_held_back_dedupes_same_requirement() -> None:
    pp = {"project": {"dependencies": ["a<2"]}, "dependency-groups": {"dev": ["a<2"]}}
    lock = {"package": [{"name": "a", "version": "1"}]}
    entries = python_uv.held_back(pp, lock, latest=lambda n: "2")
    assert len(entries) == 1


def test_held_back_prerelease_latest_inside_bounds_not_reported() -> None:
    pp = {"project": {"dependencies": ["a>=1"]}}
    assert python_uv.held_back(pp, {}, latest=lambda n: "2.0rc1") == []


def test_held_back_invalid_latest_version_fails_loud() -> None:
    pp = {"project": {"dependencies": ["a<2"]}}
    with pytest.raises(UpdateDepsError, match="'a'.*not-a-version"):
        python_uv.held_back(pp, {}, latest=lambda n: "not-a-version")


def test_held_back_lookup_failure_propagates() -> None:
    def latest(name: str) -> str:
        raise UpdateDepsError(phase="update", command="GET x", message="PyPI lookup failed for a")

    with pytest.raises(UpdateDepsError, match="PyPI lookup failed"):
        python_uv.held_back({"project": {"dependencies": ["a<2"]}}, {}, latest=latest)
