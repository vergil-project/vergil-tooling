"""Tests for the ``.vergil/`` guard snapshot in ``conftest.py`` (#5397).

The autouse guard snapshots the repository's real ``.vergil/`` around every
test. Each test here patches ``_REPO_VERGIL`` only inside :func:`_snapshot`, so
the patch is undone before the guard's teardown takes its second snapshot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tests.vergil_tooling.conftest as conftest

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _snapshot(monkeypatch: pytest.MonkeyPatch, directory: Path) -> dict[str, tuple[int, int]]:
    """Snapshot *directory* as if it were the repository's ``.vergil/``."""
    with monkeypatch.context() as patch:
        patch.setattr(conftest, "_REPO_VERGIL", directory)
        return conftest._vergil_snapshot()


def test_absent_directory_snapshots_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _snapshot(monkeypatch, tmp_path / "absent") == {}


def test_created_file_appears_in_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vergil = tmp_path / ".vergil"
    vergil.mkdir()
    before = _snapshot(monkeypatch, vergil)
    (vergil / "release-1.log").write_text("run\n")
    after = _snapshot(monkeypatch, vergil)
    assert before == {}
    assert set(after) == {"release-1.log"}


def test_runner_file_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vergil = tmp_path / ".vergil"
    vergil.mkdir()
    (vergil / "vrg-validate-x.log").write_text("runner\n")
    assert _snapshot(monkeypatch, vergil) == {}


def test_changed_file_changes_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vergil = tmp_path / ".vergil"
    vergil.mkdir()
    log = vergil / "submit-1.log"
    log.write_text("abc\n")
    before = _snapshot(monkeypatch, vergil)
    log.write_text("abcdef\n")
    after = _snapshot(monkeypatch, vergil)
    assert before != after
    assert after["submit-1.log"][1] == len("abcdef\n")
