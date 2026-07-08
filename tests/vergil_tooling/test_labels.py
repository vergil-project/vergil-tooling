"""Tests for vergil_tooling.lib.labels."""

from __future__ import annotations

from vergil_tooling.lib.labels import load_labels

# GitHub default labels that must not collide with the registry.
_GITHUB_DEFAULTS = frozenset(
    {
        "bug",
        "documentation",
        "duplicate",
        "good first issue",
        "help wanted",
        "invalid",
        "question",
        "wontfix",
    }
)


def test_load_labels_returns_dict() -> None:
    registry = load_labels()
    assert isinstance(registry, dict)
    assert "labels" in registry
    assert "delete" in registry


def test_labels_have_required_fields() -> None:
    registry = load_labels()
    for label in registry["labels"]:
        assert "name" in label, f"Label missing 'name': {label}"
        assert "color" in label, f"Label {label['name']} missing 'color'"
        assert "description" in label, f"Label {label['name']} missing 'description'"


def test_label_colors_are_valid_hex() -> None:
    registry = load_labels()
    for label in registry["labels"]:
        color = label["color"]
        assert len(color) == 6, f"Color '{color}' for {label['name']} is not 6 chars"
        int(color, 16)  # raises ValueError if not valid hex


def test_no_collision_with_github_defaults() -> None:
    registry = load_labels()
    names = {label["name"] for label in registry["labels"]}
    collisions = names & _GITHUB_DEFAULTS
    assert not collisions, f"Labels collide with GitHub defaults: {collisions}"


def test_no_duplicate_label_names() -> None:
    registry = load_labels()
    names = [label["name"] for label in registry["labels"]]
    assert len(names) == len(set(names)), f"Duplicate label names: {names}"


def test_delete_list_is_strings() -> None:
    registry = load_labels()
    for name in registry["delete"]:
        assert isinstance(name, str)


def test_registry_includes_convention_labels() -> None:
    # epic/task convention (epic #40, #85): Role (epic, ad-hoc), Stage (triage),
    # Kind (idea, research), Exception (hotfix). "standing" is retained during the
    # ad-hoc rollout window as a deprecated alias (removed in epic #85, Task 11).
    # The remaining Kind axis reuses the existing conventional-commit labels.
    names = {label["name"] for label in load_labels()["labels"]}
    for required in {"epic", "ad-hoc", "standing", "triage", "idea", "research", "hotfix"}:
        assert required in names, f"convention label missing: {required}"


def test_registry_includes_validation_label() -> None:
    # Post-merge validation task type (epic vergil-project/.github#115): a
    # validation task is never PR-workable and never auto-closed — it closes only
    # when its checklist runs and a PASS result is recorded as a comment.
    entry = next(
        (label for label in load_labels()["labels"] if label["name"] == "validation"),
        None,
    )
    assert entry is not None, "validation label missing from the registry"
    assert entry["description"], "validation label needs a description"


def test_label_change_is_additive_only() -> None:
    # Convention labels are added additively; retiring default cruft is deferred
    # to the per-repo migration pass (epic #40, Task 9). This task must not
    # enqueue any new deletions beyond the pre-existing one.
    assert load_labels()["delete"] == ["enhancement"]
