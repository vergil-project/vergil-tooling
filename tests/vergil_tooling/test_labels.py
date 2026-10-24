"""Tests for vergil_tooling.lib.labels."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.labels import (
    RETIRED_LABELS,
    UnknownLabelError,
    ensure_labels,
    ensure_labels_or_report,
    labels_to_delete,
    load_labels,
    sync_labels,
)

# GitHub default labels that must not collide with the registry.
#
# ``bug`` is intentionally omitted: the registry now owns ``bug`` canonically as
# part of the full kind axis (issue #1971) rather than leaning on GitHub's
# default, so ``--sync`` provisions a complete, consistent set regardless of
# which defaults a repo happens to have. ``documentation`` stays on the list
# because the registry's documentation kind is ``docs``, not ``documentation``.
_GITHUB_DEFAULTS = frozenset(
    {
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
    # epic/task convention (epic #40, #85): Role (epic, ad-hoc), Exception
    # (hotfix). The Kind axis reuses the existing conventional-commit labels.
    names = {label["name"] for label in load_labels()["labels"]}
    for required in {"epic", "ad-hoc", "hotfix"}:
        assert required in names, f"convention label missing: {required}"


def test_registry_excludes_retired_intake_labels() -> None:
    # The intake kind labels were retired by epic .github#176 (the parent epic
    # carries the state now), so the registry must not provision them; the
    # sync deletes them instead (issue #3546).
    names = {label["name"] for label in load_labels()["labels"]}
    assert not names & RETIRED_LABELS


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


def test_registry_includes_deployment_label() -> None:
    # Deployment task type (epic vergil-project/.github#124): an operational task
    # that installs/syncs merged changes so they are usable; never PR-workable,
    # never auto-closed, closed only on a recorded SUCCESS comment.
    entry = next(
        (label for label in load_labels()["labels"] if label["name"] == "deployment"),
        None,
    )
    assert entry is not None, "deployment label missing from the registry"
    assert entry["description"], "deployment label needs a description"


def test_registry_includes_retrospective_label() -> None:
    # Epic retrospective bookend (vergil-project/.github#201): a labelled kind
    # that — unlike validation/deployment — IS PR-workable; its docs PR publishes
    # the epic's retrospective.md. The label makes the bookend mechanical for the
    # epic-retrospective preflight and vrg-epic-audit.
    entry = next(
        (label for label in load_labels()["labels"] if label["name"] == "retrospective"),
        None,
    )
    assert entry is not None, "retrospective label missing from the registry"
    assert entry["description"], "retrospective label needs a description"


def test_label_descriptions_within_github_limit() -> None:
    # GitHub caps a label's description at 100 chars; a longer one fails
    # provisioning with HTTP 422 so vrg-ensure-label --sync cannot deploy it.
    # (Found by the post-merge-validation dogfood, epic vergil-project/.github#115.)
    for label in load_labels()["labels"]:
        length = len(label["description"])
        assert length <= 100, f"{label['name']} description is {length} chars (max 100)"


def test_delete_list_retires_deprecated_labels() -> None:
    # The delete list retires default cruft (``enhancement``) and the deprecated
    # ``standing`` alias, removed once the ad-hoc migration completed (epic #85,
    # retire-standing task): no live ``standing`` epics remain, so a sync must
    # de-provision the label.
    assert load_labels()["delete"] == ["enhancement", "standing"]


# --- ensure_labels: the shared point-of-use primitive (#3373) ---------------

_RUN = "vergil_tooling.lib.labels.github.run"


def _spec(name: str) -> dict[str, str]:
    return next(entry for entry in load_labels()["labels"] if entry["name"] == name)


def _fake_gh(repo_labels: dict[str, tuple[str, str]]):
    """A stand-in for ``gh label create --force`` over an in-memory label set."""

    def run(*args: str) -> None:
        assert args[:2] == ("label", "create")
        assert "--force" in args
        name = args[2]
        color = args[args.index("--color") + 1]
        description = args[args.index("--description") + 1]
        repo_labels[name] = (color, description)

    return run


def test_ensure_labels_creates_missing_label_from_registry() -> None:
    # A repo that was never label-synced lacks 'intake'; ensuring it creates it
    # with the registry's colour and description.
    repo_labels: dict[str, tuple[str, str]] = {}
    with patch(_RUN, side_effect=_fake_gh(repo_labels)) as mock_run:
        ensure_labels("org/repo", ["intake"])
    spec = _spec("intake")
    assert repo_labels == {"intake": (spec["color"], spec["description"])}
    assert list(mock_run.call_args.args[:5]) == ["label", "create", "intake", "--repo", "org/repo"]


def test_ensure_labels_leaves_present_label_canonical() -> None:
    # A label already present is not an error: the idempotent --force create
    # succeeds and leaves it at its canonical definition.
    spec = _spec("epic")
    repo_labels = {"epic": (spec["color"], spec["description"]), "bug": ("ffffff", "x")}
    with patch(_RUN, side_effect=_fake_gh(repo_labels)):
        ensure_labels("org/repo", ["epic"])
    assert repo_labels["epic"] == (spec["color"], spec["description"])
    assert repo_labels["bug"] == ("ffffff", "x")  # untouched: not asked for


def test_ensure_labels_creates_each_name_once_in_order() -> None:
    with patch(_RUN) as mock_run:
        ensure_labels("org/.github", ("archive", "ad-hoc", "archive"))
    assert [c.args[2] for c in mock_run.call_args_list] == ["archive", "ad-hoc"]


def test_ensure_labels_empty_is_a_noop() -> None:
    with patch(_RUN) as mock_run:
        ensure_labels("org/repo", [])
    mock_run.assert_not_called()


def test_ensure_labels_refuses_unknown_label_before_any_gh_call() -> None:
    # An off-registry label is an actionable error, and nothing is created —
    # not even the known labels named alongside it.
    with patch(_RUN) as mock_run, pytest.raises(UnknownLabelError) as excinfo:
        ensure_labels("org/repo", ["intake", "not-a-label", "nope"])
    mock_run.assert_not_called()
    assert excinfo.value.names == ("not-a-label", "nope")
    message = str(excinfo.value)
    assert "unknown labels 'not-a-label', 'nope'" in message
    assert "data/labels.json" in message


def test_unknown_label_error_singular_wording() -> None:
    assert str(UnknownLabelError(["x"])).startswith("unknown label 'x':")


def test_ensure_labels_gh_failure_propagates() -> None:
    # A failing `gh label create` is never swallowed: it surfaces to the caller,
    # which must not go on to create the issue.
    error = github.GitHubAPIError(1, ["gh"], "", "HTTP 403: Resource not accessible")
    with patch(_RUN, side_effect=error) as mock_run, pytest.raises(subprocess.CalledProcessError):
        ensure_labels("org/repo", ["intake", "epic"])
    assert mock_run.call_count == 1


def test_ensure_labels_or_report_success() -> None:
    with patch(_RUN) as mock_run:
        assert ensure_labels_or_report("vrg-x", "org/repo", ["intake"]) is True
    mock_run.assert_called_once()


def test_ensure_labels_or_report_unknown_label(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_RUN) as mock_run:
        assert ensure_labels_or_report("vrg-x", "org/repo", ["bogus"]) is False
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert err.startswith("vrg-x: unknown label 'bogus':")
    assert "data/labels.json" in err


def test_ensure_labels_or_report_gh_failure(capsys: pytest.CaptureFixture[str]) -> None:
    error = github.GitHubAPIError(1, ["gh"], "", "HTTP 403: Resource not accessible")
    with patch(_RUN, side_effect=error):
        assert ensure_labels_or_report("vrg-x", "org/repo", ["intake", "bug"]) is False
    err = capsys.readouterr().err
    assert "vrg-x: could not ensure label(s) intake, bug exist in org/repo" in err
    assert "no issue was created" in err
    assert "HTTP 403: Resource not accessible" in err


def test_ensure_labels_or_report_lets_missing_token_reach_cli_boundary() -> None:
    # The missing-token message is owned by cli_main, so it is re-raised as-is.
    error = github.MissingGitHubTokenError(1, ["gh"], "", "set the GH_TOKEN environment variable")
    with patch(_RUN, side_effect=error), pytest.raises(github.MissingGitHubTokenError):
        ensure_labels_or_report("vrg-x", "org/repo", ["intake"])


# ---------------------------------------------------------------------------
# RETIRED_LABELS / labels_to_delete / sync_labels (#3558)
# ---------------------------------------------------------------------------


def test_retired_labels_are_the_old_intake_kinds() -> None:
    assert frozenset({"triage", "idea", "research"}) == RETIRED_LABELS


def test_labels_to_delete_merges_registry_and_retired_labels() -> None:
    registry = {"labels": [], "delete": ["enhancement", "triage"]}
    assert labels_to_delete(registry) == ["enhancement", "triage", "idea", "research"]
    assert labels_to_delete({"labels": []}) == sorted(RETIRED_LABELS)


def test_sync_labels_creates_registry_and_deletes_present_retired_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = {
        "labels": [{"name": "bug", "color": "d73a4a", "description": "Something is broken"}],
        "delete": ["enhancement"],
    }
    with (
        patch("vergil_tooling.lib.labels.load_labels", return_value=registry),
        patch(
            "vergil_tooling.lib.labels.github.read_json",
            return_value=[{"name": "bug"}, {"name": "enhancement"}, {"name": "idea"}],
        ),
        patch("vergil_tooling.lib.labels.github.run") as mock_run,
    ):
        sync_labels("o/r")
    assert [c.args for c in mock_run.call_args_list] == [
        (
            "label",
            "create",
            "bug",
            "--repo",
            "o/r",
            "--force",
            "--color",
            "d73a4a",
            "--description",
            "Something is broken",
        ),
        ("label", "delete", "enhancement", "--repo", "o/r", "--yes"),
        ("label", "delete", "idea", "--repo", "o/r", "--yes"),
    ]
    assert capsys.readouterr().out == (
        "Syncing labels for o/r:\n  bug\n  deleted enhancement\n  deleted idea\n"
    )


def test_sync_labels_rejects_non_list_label_listing() -> None:
    with (
        patch("vergil_tooling.lib.labels.github.read_json", return_value={"message": "nope"}),
        patch("vergil_tooling.lib.labels.github.run"),
        pytest.raises(TypeError, match="unexpected `gh label list` output for o/r"),
    ):
        sync_labels("o/r")
