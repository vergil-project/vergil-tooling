"""Tests for vergil_tooling.bin.vrg_epic_audit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_epic_audit import main
from vergil_tooling.lib.epics import ChildState, IssueRef

_MOD = "vergil_tooling.bin.vrg_epic_audit"


def test_main_repo_scopes_to_resolved_home() -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab") as home,
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]) as op,
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]) as ed,
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", return_value=[]),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]) as stray,
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]) as ceoc,
    ):
        rc = main(["--repo", "org/lab"])
    assert rc == 0
    home.assert_called_once_with("org", "lab")
    assert ed.call_args.kwargs["home"] == "org/lab"
    assert op.call_args.kwargs["home"] == "org/lab"
    assert stray.call_args.kwargs["home"] == "org/lab"
    assert ceoc.call_args.kwargs["home"] == "org/lab"


def test_main_repo_malformed_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--repo", "noslash"])
    assert rc == 1
    assert "owner/repo" in capsys.readouterr().err


def test_main_epic_enumerates_children_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    # --epic emits the epic's children (mixed open/closed) as machine-readable
    # JSON, bypassing the drift audit (issue #2538).
    children = [
        ChildState(IssueRef("vergil-project", "vergil-tooling", 2538), "OPEN", "Open child"),
        ChildState(IssueRef("vergil-project", "vergil-tooling", 2500), "CLOSED", "Closed child"),
    ]
    with (
        patch(f"{_MOD}.github.current_repo", return_value="vergil-project/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
        patch(f"{_MOD}.epics.child_states", return_value=children) as cs,
    ):
        rc = main(["--epic", "vergil-project/.github#201"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["epic"] == "vergil-project/.github#201"
    states = {c["number"]: c["state"] for c in payload["children"]}
    assert states == {2500: "CLOSED", 2538: "OPEN"}
    # child_states is asked about the resolved epic ref.
    assert cs.call_args.args[0] == IssueRef("vergil-project", ".github", 201)


def test_main_epic_rejects_non_epic_ref(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="vergil-project/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=False),
        patch(f"{_MOD}.epics.child_states") as cs,
    ):
        rc = main(["--epic", "vergil-project/vergil-tooling#2538"])
    assert rc == 1
    assert "is not an epic" in capsys.readouterr().err
    cs.assert_not_called()


def test_main_epic_rejects_malformed_ref(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.github.current_repo", return_value="vergil-project/.github"):
        rc = main(["--epic", "not-a-ref"])
    assert rc == 1
    assert "not an issue ref" in capsys.readouterr().err


def test_main_prints_audit(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", return_value=[]),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "drift audit" in out
    # The read-only banner names the auto-detected org and states nothing changed.
    assert "Read-only audit" in out
    assert "**vergil-project**" in out


def test_main_reports_invariant_violations(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(
            f"{_MOD}.epic_audit.epic_outside_dotgithub",
            return_value=["vergil-project/vergil-tooling#99"],
        ),
        patch(
            f"{_MOD}.epic_audit.stray_dotgithub_issue",
            return_value=["vergil-project/.github#7"],
        ),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Invariant violations" in out
    assert "vergil-project/vergil-tooling#99" in out
    assert "vergil-project/.github#7" in out


def test_main_errors_when_org_undetectable(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.github.detect_org", return_value=None):
        rc = main([])
    assert rc == 1
    assert "could not determine the GitHub org" in capsys.readouterr().err


def test_window_days_controls_since() -> None:
    task_drift = MagicMock(return_value=[])
    with (
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", task_drift),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", return_value=[]),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
    ):
        rc = main(["--window-days", "7"])
    assert rc == 0
    expected_since = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    assert task_drift.call_args.args[0] == expected_since
    assert task_drift.call_args.kwargs["org"] == "vergil-project"


def test_invalid_window_days_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--window-days", "0"])
    assert exc.value.code == 2


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "Read-only" in capsys.readouterr().out


def test_close_refused_for_agent(capsys: pytest.CaptureFixture[str]) -> None:
    # Refusal happens before any network work, so nothing else needs mocking.
    # Pin the sweep signal off so the refusal is deterministic regardless of env.
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch.dict("os.environ", {"VRG_EPIC_SWEEP": ""}),
    ):
        rc = main(["--close"])
    assert rc == 1
    assert "human action" in capsys.readouterr().err


def test_close_allowed_for_scheduled_sweep(capsys: pytest.CaptureFixture[str]) -> None:
    """The scheduled sweep (VRG_EPIC_SWEEP=1) may --close even though it is not human."""
    close_drift = MagicMock(return_value=["o/r#1"])
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch.dict("os.environ", {"VRG_EPIC_SWEEP": "1"}),
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=["T"]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=["E"]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.reopen_epics_with_open_children", return_value=[]),
        patch(f"{_MOD}.epic_audit.close_drift", close_drift),
    ):
        rc = main(["--close"])
    assert rc == 0
    close_drift.assert_called_once_with(
        ["T"], ["E"], org="vergil-project", home="vergil-project/.github"
    )
    assert "o/r#1" in capsys.readouterr().out


def test_close_as_human_closes_and_summarizes(capsys: pytest.CaptureFixture[str]) -> None:
    close_drift = MagicMock(return_value=["o/r#1"])
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=["T"]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=["E"]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.reopen_epics_with_open_children", return_value=[]),
        patch(f"{_MOD}.epic_audit.close_drift", close_drift),
    ):
        rc = main(["--close"])
    assert rc == 0
    close_drift.assert_called_once_with(
        ["T"], ["E"], org="vergil-project", home="vergil-project/.github"
    )
    out = capsys.readouterr().out
    assert "closed" in out.lower()
    assert "o/r#1" in out


def test_close_reopens_closed_epic_with_open_child(capsys: pytest.CaptureFixture[str]) -> None:
    # The --close path remediates the closed-epic-with-open-child invariant by
    # reopening each violating epic (issue #2259, Fix C).
    violations = [("EPIC_VIOLATION",)]
    reopen = MagicMock(return_value=["vergil-project/.github#19"])
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=violations),
        patch(f"{_MOD}.epic_audit.close_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.reopen_epics_with_open_children", reopen),
    ):
        rc = main(["--close"])
    assert rc == 0
    reopen.assert_called_once_with(violations)
    out = capsys.readouterr().out
    assert "Reopened" in out
    assert "vergil-project/.github#19" in out
