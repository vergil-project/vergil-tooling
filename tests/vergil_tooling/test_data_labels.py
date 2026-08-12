"""Tests for the packaged label definitions (data/labels.json)."""

from __future__ import annotations

import json
from importlib import resources


def _labels() -> list[dict[str, str]]:
    data = json.loads(resources.files("vergil_tooling.data").joinpath("labels.json").read_text())
    return data["labels"]


def test_archive_label_present_and_wellformed() -> None:
    by_name = {entry["name"]: entry for entry in _labels()}
    assert "archive" in by_name, "labels.json must define the 'archive' label"
    archive = by_name["archive"]
    assert set(archive) == {"name", "color", "description"}
    assert archive["color"]  # non-empty hex
    assert "archive" in archive["description"].lower()


def test_label_names_are_unique() -> None:
    names = [entry["name"] for entry in _labels()]
    assert len(names) == len(set(names)), "duplicate label names in labels.json"


def test_registry_includes_full_kind_axis() -> None:
    # The canonical registry must own the *complete* kind axis so a repo's first
    # ``vrg-ensure-label --sync`` seeds a consistent set without relying on GitHub
    # default labels (which may be renamed or deleted). ``bug`` and ``docs`` were
    # historically absent — ``bug`` leaned on GitHub's default and ``docs`` had to
    # be created ad hoc mid-migration (issue #1971).
    names = {entry["name"] for entry in _labels()}
    for kind in {"bug", "feature", "docs", "refactor", "chore", "research"}:
        assert kind in names, f"kind label missing from labels.json: {kind}"
