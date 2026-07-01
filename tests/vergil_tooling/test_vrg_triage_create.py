"""Tests for vergil_tooling.bin.vrg_triage_create."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_triage_create import main, parse_args

_MOD = "vergil_tooling.bin.vrg_triage_create"
_URL = "https://github.com/org/repo/issues/321"


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_main_creates_unlinked_triage_in_current_repo() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--body", "B"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/repo"
    assert mock_create.call_args.kwargs["labels"] == ["triage"]


def test_main_repo_override_skips_current_repo() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo") as mock_cur,
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--repo", "org/.github"])
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    mock_cur.assert_not_called()


def test_main_adds_extra_labels() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--label", "bug"])
    assert mock_create.call_args.kwargs["labels"] == ["triage", "bug"]
