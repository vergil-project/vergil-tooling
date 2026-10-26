"""Tests for vergil_tooling.bin.vrg_intake_route (the thin CLI over intake_route)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_intake_route import main, parse_args
from vergil_tooling.lib import github
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.intake_route import (
    Defer,
    Drop,
    OneOff,
    RouteRefusedError,
    RouteResult,
    Split,
    ToIntake,
    ToPlanning,
)

_MOD = "vergil_tooling.bin.vrg_intake_route"
ITEM = IssueRef("org", ".github", 352)


def _run(argv: list[str], result: RouteResult | None = None) -> tuple[int, MagicMock]:
    with patch(
        f"{_MOD}.intake_route.route", return_value=result or RouteResult(ITEM, "none")
    ) as mock_route:
        rc = main(argv)
    return rc, mock_route


def test_parse_args_requires_a_route() -> None:
    with pytest.raises(SystemExit):
        parse_args(["org/.github#352"])


def test_parse_args_drop_requires_reason() -> None:
    with pytest.raises(SystemExit):
        parse_args(["org/.github#352", "drop"])


def test_parse_args_split_requires_into() -> None:
    with pytest.raises(SystemExit):
        parse_args(["org/.github#352", "split", "--reason", "x"])


def test_main_drop(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#352", "drop", "--reason", "not feasible"])
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, Drop(reason="not feasible"))
    assert capsys.readouterr().out == "Routed org/.github#352: drop.\n"


def test_main_defer() -> None:
    rc, mock_route = _run(
        ["org/.github#352", "defer", "--reason", "later", "--revisit-when", "X ships"]
    )
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, Defer(reason="later", revisit_when="X ships"))


def test_main_defer_with_date() -> None:
    rc, mock_route = _run(
        [
            "org/.github#352",
            "defer",
            "--reason",
            "later",
            "--revisit-when",
            "X ships",
            "--date",
            "2026-03-04",
        ]
    )
    assert rc == 0
    mock_route.assert_called_once_with(
        ITEM, Defer(reason="later", revisit_when="X ships", day=date(2026, 3, 4))
    )


def test_parse_args_defer_requires_revisit_when() -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(["org/.github#352", "defer", "--reason", "later"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("bad", ["2026-13-01", "26-10-01", "2026/10/01", "soon"])
def test_parse_args_defer_rejects_bad_date(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(
            ["org/.github#352", "defer", "--reason", "r", "--revisit-when", "w", "--date", bad]
        )
    assert excinfo.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_main_intake() -> None:
    rc, mock_route = _run(["org/.github#352", "intake"])
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, ToIntake())


def test_main_planning() -> None:
    rc, mock_route = _run(["org/.github#352", "planning"])
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, ToPlanning())


def test_main_one_off_defaults_to_adhoc(capsys: pytest.CaptureFixture[str]) -> None:
    moved = RouteResult(IssueRef("org", "tool", 900), "transfer")
    rc, mock_route = _run(["org/.github#352", "one-off", "--repo", "org/tool"], moved)
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, OneOff(repo="org/tool", epic="adhoc"))
    assert capsys.readouterr().out == (
        "Routed org/.github#352: one-off, now org/tool#900 (by transfer).\n"
    )


def test_main_one_off_with_epic() -> None:
    rc, mock_route = _run(["org/.github#352", "one-off", "--repo", "org/tool", "--epic", "#50"])
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, OneOff(repo="org/tool", epic="#50"))


def test_main_split_reads_bare_refs_against_item_repo() -> None:
    rc, mock_route = _run(
        [
            "org/.github#352",
            "split",
            "--into",
            "#10",
            "--into",
            "org/tool#11",
            "--reason",
            "two fixes",
        ]
    )
    assert rc == 0
    mock_route.assert_called_once_with(
        ITEM,
        Split(
            into=(IssueRef("org", ".github", 10), IssueRef("org", "tool", 11)),
            reason="two fixes",
        ),
    )


def test_main_bare_item_ref_uses_current_repo() -> None:
    with patch(f"{_MOD}.github.current_repo", return_value="org/.github") as mock_repo:
        rc, mock_route = _run(["#352", "planning"])
    assert rc == 0
    mock_repo.assert_called_once_with()
    mock_route.assert_called_once_with(ITEM, ToPlanning())


def test_main_qualified_item_ref_does_not_read_cwd() -> None:
    with patch(f"{_MOD}.github.current_repo") as mock_repo:
        _run(["org/.github#352", "planning"])
    mock_repo.assert_not_called()


def test_main_malformed_ref_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#x", "planning"])
    assert rc == 1
    mock_route.assert_not_called()
    assert "vrg-intake-route: not an issue ref" in capsys.readouterr().err


def test_main_refusal_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        f"{_MOD}.intake_route.route",
        side_effect=RouteRefusedError("org/.github#352 has no parent"),
    ):
        rc = main(["org/.github#352", "drop", "--reason", "x"])
    assert rc == 1
    assert capsys.readouterr().err == "vrg-intake-route: org/.github#352 has no parent\n"


def test_main_no_installation_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    exc = github.NoInstallationError("org", ["other"])
    with (
        patch(f"{_MOD}.intake_route.route", side_effect=exc),
        patch(f"{_MOD}.github.no_installation_message", return_value="no app for org"),
    ):
        rc = main(["org/.github#352", "planning"])
    assert rc == 1
    assert capsys.readouterr().err == "vrg-intake-route: no app for org\n"
