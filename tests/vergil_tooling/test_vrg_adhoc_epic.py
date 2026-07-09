"""Tests for vergil_tooling.bin.vrg_adhoc_epic."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_adhoc_epic import main, parse_args
from vergil_tooling.lib import epics

_MOD = "vergil_tooling.bin.vrg_adhoc_epic"


def test_parse_args_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_ensure_current_repo(capsys: pytest.CaptureFixture[str]) -> None:
    # The ad-hoc epic for org/repo lives in org/.github (title-disambiguated).
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(
            f"{_MOD}.epics.ensure_adhoc_epic",
            return_value=epics.IssueRef("org", ".github", 5),
        ) as mock_ensure,
    ):
        rc = main(["ensure"])
    assert rc == 0
    mock_ensure.assert_called_once_with("org/repo")
    out = capsys.readouterr().out
    assert "epic home: org/.github [PUBLIC]" in out
    assert "Ad-hoc epic:" in out
    assert "org/.github#5" in out


def test_ensure_repo_override(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo") as mock_cur,
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(
            f"{_MOD}.epics.ensure_adhoc_epic",
            return_value=epics.IssueRef("org", ".github", 9),
        ) as mock_ensure,
    ):
        rc = main(["ensure", "--repo", "org/actions"])
    assert rc == 0
    mock_ensure.assert_called_once_with("org/actions")
    mock_cur.assert_not_called()
    assert "org/.github#9" in capsys.readouterr().out


def test_ensure_malformed_repo_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.epics.ensure_adhoc_epic") as mock_ensure:
        rc = main(["ensure", "--repo", "noslash"])
    assert rc == 1
    assert "owner/repo" in capsys.readouterr().err
    mock_ensure.assert_not_called()


def test_ensure_private_repo_echoes_self_home(capsys: pytest.CaptureFixture[str]) -> None:
    # A private target's ad-hoc epic homes in the repo itself; the echo shows it.
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PRIVATE"),
        patch(
            f"{_MOD}.epics.ensure_adhoc_epic",
            return_value=epics.IssueRef("org", "lab", 3),
        ),
    ):
        rc = main(["ensure", "--repo", "org/lab"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "epic home: org/lab [PRIVATE]" in out
    assert "org/lab#3" in out
