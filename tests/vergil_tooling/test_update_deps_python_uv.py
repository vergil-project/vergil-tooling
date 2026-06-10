from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.update_deps.context import UpdateDepsContext
from vergil_tooling.lib.update_deps.updaters.python_uv import PythonUvUpdater

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.update_deps.updaters.python_uv"


def _ctx(root: Path) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=root)


def test_applies_true_when_pyproject_and_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("")
    assert PythonUvUpdater().applies(_ctx(tmp_path)) is True


def test_applies_false_without_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert PythonUvUpdater().applies(_ctx(tmp_path)) is False


def test_apply_reports_changed_when_lock_dirty(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: " M uv.lock")
    result = PythonUvUpdater().apply(_ctx(tmp_path))
    assert calls == [["vrg-container-run", "--", "uv", "lock", "--upgrade"]]
    assert result.changed is True
    assert result.commit_message == "chore(deps): uv lock --upgrade"


def test_apply_reports_unchanged_when_lock_clean(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: "")
    result = PythonUvUpdater().apply(_ctx(tmp_path))
    assert result.changed is False
