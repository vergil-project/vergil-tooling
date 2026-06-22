"""Tests for the per-instance Lima metadata sidecar (vergil_tooling.lib.lima)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import lima

if TYPE_CHECKING:
    from pathlib import Path


def test_write_then_read_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lima.Path, "home", classmethod(lambda cls: tmp_path))
    lima.write_instance_meta("u.org.repo", "vergil-user", "org", "repo")
    assert lima.read_instance_meta("u.org.repo") == {
        "schema": 1,
        "identity": "vergil-user",
        "org": "org",
        "repo": "repo",
    }


def test_read_returns_none_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lima.Path, "home", classmethod(lambda cls: tmp_path))
    assert lima.read_instance_meta("missing") is None


def test_read_raises_on_corrupt_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lima.Path, "home", classmethod(lambda cls: tmp_path))
    meta_dir = tmp_path / ".lima" / "u.org.repo"
    meta_dir.mkdir(parents=True)
    (meta_dir / "vergil-meta.json").write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        lima.read_instance_meta("u.org.repo")
