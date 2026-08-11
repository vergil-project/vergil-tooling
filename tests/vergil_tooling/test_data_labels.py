"""Tests for the packaged label definitions (data/labels.json)."""

from __future__ import annotations

import json
from importlib import resources


def _labels() -> list[dict[str, str]]:
    data = json.loads(
        resources.files("vergil_tooling.data").joinpath("labels.json").read_text()
    )
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
