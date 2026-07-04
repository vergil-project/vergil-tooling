"""Tests for vergil_tooling.bin.vrg_triage_create."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_triage_create import main, parse_args

_MOD = "vergil_tooling.bin.vrg_triage_create"
_URL = "https://github.com/org/.github/issues/321"


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_main_defaults_to_org_dotgithub() -> None:
    # Intake defaults to <org>/.github (supersedes the old current-repo default,
    # #2075) so the whole org-wide intake queue lives in one place.
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--body", "B"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["labels"] == ["triage"]


def test_main_kind_research() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--kind", "research"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["labels"] == ["research"]


def test_main_kind_idea() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--kind", "idea"])
    assert mock_create.call_args.kwargs["labels"] == ["idea"]


def test_main_repo_override_skips_detect_org() -> None:
    with (
        patch(f"{_MOD}.github.detect_org") as mock_detect,
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--repo", "org/tooling"])
    assert mock_create.call_args.kwargs["repo"] == "org/tooling"
    mock_detect.assert_not_called()


def test_main_adds_extra_labels() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--label", "bug"])
    assert mock_create.call_args.kwargs["labels"] == ["triage", "bug"]


def test_main_kind_dedupes_with_extra_label() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--kind", "research", "--label", "research"])
    assert mock_create.call_args.kwargs["labels"] == ["research"]


def test_main_undetectable_org_errors() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value=None),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()
