"""Tests for vergil_tooling.bin.vrg_epic_create."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_epic_create import main, parse_args

_MOD = "vergil_tooling.bin.vrg_epic_create"
_URL = "https://github.com/org/.github/issues/50"


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_main_creates_epic_in_org_dot_github() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "Epic: X", "--body", "B"])
    assert rc == 0
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["title"] == "Epic: X"
    assert mock_create.call_args.kwargs["labels"] == ["epic"]


def test_main_adds_extra_labels() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        rc = main(["--title", "T", "--label", "standing"])
    assert rc == 0
    assert mock_create.call_args.kwargs["labels"] == ["epic", "standing"]


def test_main_dedups_epic_label() -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
    ):
        main(["--title", "T", "--label", "epic"])
    assert mock_create.call_args.kwargs["labels"] == ["epic"]


def test_main_errors_when_org_undetectable(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value=None),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--title", "T"])
    assert rc == 1
    assert "could not determine the GitHub org" in capsys.readouterr().err
    mock_create.assert_not_called()
