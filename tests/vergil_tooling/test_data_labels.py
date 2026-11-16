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
    for kind in {"bug", "feature", "docs", "refactor", "chore"}:
        assert kind in names, f"kind label missing from labels.json: {kind}"


def test_security_labels_registered() -> None:
    # Security records (epic vergil-project/.github#517, spec 4.7).
    by_name = {entry["name"]: entry for entry in _labels()}
    assert by_name["security"]["description"] == (
        "Has a private security record in the org's .github advisories"
    )
    assert by_name["security-disclosed"]["description"] == (
        "Past issue that disclosed a security problem publicly; fixed, no private record"
    )


def test_epic_size_labels_registered() -> None:
    # The planning-queue size scale (epic vergil-project/.github#1191, spec 6.1-6.2).
    by_name = {entry["name"]: entry for entry in _labels()}
    bands = {"S": "1–9", "M": "10–19", "L": "20–39", "XL": "40+"}
    colors = set()
    for size, band in bands.items():
        entry = by_name[f"epic-size:{size}"]
        assert set(entry) == {"name", "color", "description"}
        assert band in entry["description"]
        colors.add(entry["color"])
    assert len(colors) == len(bands), "each epic-size label needs its own shade"
