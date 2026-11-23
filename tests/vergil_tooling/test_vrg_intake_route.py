"""Tests for vergil_tooling.bin.vrg_intake_route (the thin CLI over intake_route)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_intake_route import main, parse_args
from vergil_tooling.lib import epics, github, trust, trusted_authors
from vergil_tooling.lib.epic_size import Sizing
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.intake_route import (
    Defer,
    Drop,
    OneOff,
    RouteHalfAppliedError,
    RouteRefusedError,
    RouteResult,
    SizeItem,
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


@pytest.mark.parametrize(
    "exc",
    [
        trust.TrustReadError("read_issue org/.github#352", "the comments is missing"),
        trusted_authors.TrustedAuthorsNotConfiguredError("trusted authors are not configured"),
        trust.UntrustedContentError(
            trust.Skipped(
                "org/.github", 352, "issue", "drive-by", "2026-11-01T09:30:00Z", "body", "untrusted"
            ),
            "the issue body is untrusted",
        ),
    ],
)
def test_main_trust_failure_exits_1(capsys: pytest.CaptureFixture[str], exc: Exception) -> None:
    with patch(f"{_MOD}.intake_route.route", side_effect=exc):
        rc = main(["org/.github#352", "drop", "--reason", "x"])
    assert rc == 1
    assert capsys.readouterr().err == f"vrg-intake-route: {exc}\n"


def test_main_half_applied_route_exits_1_without_routed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A move whose final parent is not the target epic never prints Routed (#4004).
    message = (
        "moved org/.github#352 to org/tool#900, but it is under org/.github#7, not under "
        "org/.github#11; the route is half-applied. Fix it with: "
        "vrg-epic-move --task 'org/tool#900' --epic 'org/.github#11'"
    )
    with patch(f"{_MOD}.intake_route.route", side_effect=RouteHalfAppliedError(message)):
        rc = main(["org/.github#352", "one-off", "--repo", "org/tool"])
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"vrg-intake-route: {message}\n"


def test_main_unconfirmed_queue_epic_exits_1_naming_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A queue epic created but not confirmed is a clean refusal naming the new epic.
    created = IssueRef("org", ".github", 10)
    message = "created deferred epic org/.github#10 but org/.github also has org/.github#9"
    exc = epics.StandingEpicDuplicateError(message, created, [IssueRef("org", ".github", 9)])
    with patch(f"{_MOD}.intake_route.route", side_effect=exc):
        rc = main(["org/.github#352", "defer", "--reason", "later", "--revisit-when", "X ships"])
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"vrg-intake-route: {message}\n"


def test_main_no_installation_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    exc = github.NoInstallationError("org", ["other"])
    with (
        patch(f"{_MOD}.intake_route.route", side_effect=exc),
        patch(f"{_MOD}.github.no_installation_message", return_value="no app for org"),
    ):
        rc = main(["org/.github#352", "planning"])
    assert rc == 1
    assert capsys.readouterr().err == "vrg-intake-route: no app for org\n"


# -- size and sizing on the planning route (epic vergil-project/.github#1191) --


def test_size_parses(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#352", "size", "--tasks", "14", "--reason", "x"])
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, SizeItem(Sizing(14, "med", "x")))
    # The size route moves nothing, so it prints no Routed line.
    assert capsys.readouterr().out == "Sized org/.github#352: epic-size M, about 14 tasks.\n"


def test_size_with_confidence() -> None:
    rc, mock_route = _run(
        ["org/.github#352", "size", "--tasks", "3", "--confidence", "high", "--reason", "x"]
    )
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, SizeItem(Sizing(3, "high", "x")))


@pytest.mark.parametrize(
    "argv",
    [
        ["size", "--reason", "x"],
        ["size", "--tasks", "14"],
        ["size"],
    ],
)
def test_size_requires_tasks_and_reason(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(["org/.github#352", *argv])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("bad", ["fourteen", "1.5", ""])
def test_size_rejects_non_integer_tasks(bad: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(["org/.github#352", "size", "--tasks", bad, "--reason", "x"])
    assert excinfo.value.code == 2


def test_size_rejects_unknown_confidence() -> None:
    with pytest.raises(SystemExit) as excinfo:
        parse_args(
            ["org/.github#352", "size", "--tasks", "3", "--confidence", "medium", "--reason", "x"]
        )
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("argv", "match"),
    [
        (["--tasks", "0", "--reason", "x"], "positive integer"),
        (["--tasks", "-3", "--reason", "x"], "positive integer"),
        (["--tasks", "3", "--reason", " "], "must not be empty"),
        (["--tasks", "3", "--reason", "one\ntwo"], "one line"),
    ],
)
def test_size_bad_sizing_exits_1_without_routing(
    argv: list[str], match: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, mock_route = _run(["org/.github#352", "size", *argv])
    assert rc == 1
    mock_route.assert_not_called()
    assert match in capsys.readouterr().err


def test_size_success_line(capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _run(["org/.github#352", "size", "--tasks", "45", "--reason", "big"])
    assert rc == 0
    assert capsys.readouterr().out == "Sized org/.github#352: epic-size XL, about 45 tasks.\n"


def test_size_half_applied_exits_1_without_sized(capsys: pytest.CaptureFixture[str]) -> None:
    message = "org/.github#352: sizing comment posted, label not set"
    with patch(f"{_MOD}.intake_route.route", side_effect=RouteHalfAppliedError(message)):
        rc = main(["org/.github#352", "size", "--tasks", "14", "--reason", "x"])
    assert rc == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"vrg-intake-route: {message}\n"


def test_planning_with_sizing(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(
        ["org/.github#352", "planning", "--tasks", "14", "--confidence", "low", "--reason", "x"]
    )
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, ToPlanning(sizing=Sizing(14, "low", "x")))
    assert capsys.readouterr().out == (
        "Routed org/.github#352: planning.\nSized org/.github#352: epic-size M, about 14 tasks.\n"
    )


def test_planning_without_sizing_prints_only_routed(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#352", "planning"])
    assert rc == 0
    mock_route.assert_called_once_with(ITEM, ToPlanning(sizing=None))
    assert capsys.readouterr().out == "Routed org/.github#352: planning.\n"


def test_planning_tasks_without_reason_refused(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#352", "planning", "--tasks", "14"])
    assert rc == 1
    mock_route.assert_not_called()
    assert "--tasks and --reason are given together" in capsys.readouterr().err


def test_planning_reason_without_tasks_refused(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#352", "planning", "--reason", "x"])
    assert rc == 1
    mock_route.assert_not_called()
    assert "--tasks and --reason are given together" in capsys.readouterr().err


def test_planning_confidence_without_tasks_refused(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#352", "planning", "--confidence", "high"])
    assert rc == 1
    mock_route.assert_not_called()
    assert "--confidence needs --tasks" in capsys.readouterr().err


def test_planning_bad_sizing_refused_before_routing(capsys: pytest.CaptureFixture[str]) -> None:
    rc, mock_route = _run(["org/.github#352", "planning", "--tasks", "0", "--reason", "x"])
    assert rc == 1
    mock_route.assert_not_called()
    assert "positive integer" in capsys.readouterr().err
