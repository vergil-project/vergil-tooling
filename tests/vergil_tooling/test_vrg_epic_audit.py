"""Tests for vergil_tooling.bin.vrg_epic_audit."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_epic_audit import main
from vergil_tooling.lib import github
from vergil_tooling.lib.epic_audit import Orphan, RetiredLabels, StandingEpicFull
from vergil_tooling.lib.epics import ChildState, IssueRef

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

_MOD = "vergil_tooling.bin.vrg_epic_audit"


@pytest.fixture(autouse=True)
def standing_drain() -> Iterator[MagicMock]:
    # Every audit path also drains the intake/planning queue epics; stub it out
    # by default so tests that do not exercise it make no GitHub calls. Tests
    # that do exercise it patch drain_standing_org themselves (innermost wins).
    # Likewise the org-wide open-issue enumeration behind the orphan pass: an
    # empty org makes every pass a no-op with no GitHub calls.
    with (
        patch(f"{_MOD}.epics.drain_standing_org", return_value=[]) as mock_drain,
        patch(f"{_MOD}.epic_audit.open_issues", return_value=[]),
    ):
        yield mock_drain


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
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
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


@pytest.mark.parametrize(
    ("cwd_repo", "public", "expected_epic"),
    [
        # Public code repo: its epics home in <org>/.github.
        ("org/tooling", {"org/tooling", "org/.github"}, IssueRef("org", ".github", 407)),
        # Private repo with a public .github: its epics home in the repo itself.
        ("org/lab", {"org/.github"}, IssueRef("org", "lab", 407)),
    ],
)
def test_main_bare_epic_resolves_against_cwd_epic_home(
    cwd_repo: str, public: set[str], expected_epic: IssueRef
) -> None:
    """A bare '--epic #N' reads against the current repo's epic home (#3415).

    Every visibility probe runs under the current repo owner's App token.
    """
    seen_orgs: list[str | None] = []

    def is_public(nwo: str) -> bool:
        seen_orgs.append(github._target_org.get())
        return nwo in public

    with (
        patch(f"{_MOD}.github.current_repo", return_value=cwd_repo),
        patch("vergil_tooling.lib.github.is_public", side_effect=is_public),
        patch(f"{_MOD}.epics.is_epic", return_value=True) as ie,
        patch(f"{_MOD}.epics.child_states", return_value=[]) as cs,
    ):
        rc = main(["--epic", "#407"])
    assert rc == 0
    ie.assert_called_once_with(expected_epic)
    assert cs.call_args.args[0] == expected_epic
    assert seen_orgs
    assert set(seen_orgs) == {"org"}


def test_main_qualified_epic_ref_skips_visibility_lookup() -> None:
    """A full 'owner/repo#N' is used as written, with no visibility probe."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch("vergil_tooling.lib.github.is_public") as probe,
        patch(f"{_MOD}.epics.is_epic", return_value=True) as ie,
        patch(f"{_MOD}.epics.child_states", return_value=[]),
    ):
        rc = main(["--epic", "other/repo#12"])
    assert rc == 0
    ie.assert_called_once_with(IssueRef("other", "repo", 12))
    probe.assert_not_called()


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
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
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
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
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
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
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
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
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
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
    ):
        rc = main(["--close"])
    assert rc == 0
    close_drift.assert_called_once_with(
        ["T"], ["E"], org="vergil-project", home="vergil-project/.github"
    )
    out = capsys.readouterr().out
    assert "closed" in out.lower()
    assert "o/r#1" in out


def test_close_sweep_also_drains_adhoc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_EPIC_SWEEP", "1")
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.close_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.reopen_epics_with_open_children", return_value=[]),
        patch(f"{_MOD}.epic_audit.render_closed", return_value=""),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]) as mock_drain,
    ):
        rc = main(["--close"])
    assert rc == 0
    assert mock_drain.call_args.args[0] == "org"
    assert mock_drain.call_args.kwargs["apply"] is True


def test_close_sweep_drain_scoped_to_org(capsys: pytest.CaptureFixture[str]) -> None:
    # The write-path drain mutates issues in the org's home, so its token must be
    # scoped to the org (spec §7) — main() has no other org scope, so the drain
    # must be wrapped explicitly.
    order: list[str] = []
    scope = MagicMock()
    scope.return_value.__enter__ = lambda *_: order.append("enter")
    scope.return_value.__exit__ = lambda *_: order.append("exit") or False

    def _drain(_org: str, *, apply: bool, now: datetime) -> list[object]:
        order.append("drain")
        return []

    plan = MagicMock()
    plan.moves = [object()]
    plan.close = []
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.target_org", scope),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.close_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.reopen_epics_with_open_children", return_value=[]),
        patch(f"{_MOD}.epic_audit.render_closed", return_value=""),
        patch(f"{_MOD}.epics.drain_adhoc_org", side_effect=_drain),
    ):
        rc = main(["--close"])
    assert rc == 0
    assert scope.call_args.args[0] == "org"
    # The second scoped block is the orphan pass (no-op on an empty org).
    assert order == ["enter", "drain", "exit", "enter", "exit"]


def test_close_sweep_drain_summary(capsys: pytest.CaptureFixture[str]) -> None:
    plan = MagicMock()
    plan.moves = [object(), object()]
    plan.close = [object()]
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.close_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.reopen_epics_with_open_children", return_value=[]),
        patch(f"{_MOD}.epic_audit.render_closed", return_value=""),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[plan]),
    ):
        rc = main(["--close"])
    assert rc == 0
    assert (
        "Ad-hoc drain: 2 child(ren) archived, 1 past archive(s) closed." in capsys.readouterr().out
    )


def test_report_path_previews_adhoc_drain(capsys: pytest.CaptureFixture[str]) -> None:
    plan = MagicMock()
    plan.moves = [object(), object()]
    with (
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", return_value=[]),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[plan]) as mock_drain,
    ):
        rc = main([])
    assert rc == 0
    assert mock_drain.call_args.kwargs["apply"] is False
    assert "Ad-hoc drain (preview): 2 child(ren) would be archived." in capsys.readouterr().out


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
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
    ):
        rc = main(["--close"])
    assert rc == 0
    reopen.assert_called_once_with(violations)
    out = capsys.readouterr().out
    assert "Reopened" in out
    assert "vergil-project/.github#19" in out


# -- intake/planning queue drain (epic vergil-project/.github#176, Task 2) -----


def test_epic_audit_close_runs_standing_drain(
    standing_drain: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    # --close drains the intake/planning queue epics beside the ad-hoc drain,
    # inside the same org-scoped token block, and summarizes what moved.
    order: list[str] = []
    scope = MagicMock()
    scope.return_value.__enter__ = lambda *_: order.append("enter")
    scope.return_value.__exit__ = lambda *_: order.append("exit") or False

    plan = MagicMock()
    plan.moves = [object(), object(), object()]
    plan.close = [object()]

    def _standing(_org: str, *, apply: bool, now: datetime) -> list[object]:
        order.append("standing")
        return [plan]

    standing_drain.side_effect = _standing
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.target_org", scope),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.close_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.reopen_epics_with_open_children", return_value=[]),
        patch(f"{_MOD}.epic_audit.render_closed", return_value=""),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
    ):
        rc = main(["--close"])
    assert rc == 0
    assert order == ["enter", "standing", "exit", "enter", "exit"]
    assert standing_drain.call_args.args[0] == "org"
    assert standing_drain.call_args.kwargs["apply"] is True
    out = capsys.readouterr().out
    assert "Intake/planning drain: 3 child(ren) archived, 1 past archive(s) closed." in out


def test_report_path_previews_standing_drain(
    standing_drain: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = MagicMock()
    plan.moves = [object()]
    standing_drain.return_value = [plan]
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", return_value=[]),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
    ):
        rc = main([])
    assert rc == 0
    assert standing_drain.call_args.kwargs["apply"] is False
    out = capsys.readouterr().out
    assert "Intake/planning drain (preview): 1 child(ren) would be archived." in out


# -- orphan pass, label retirement, queue visibility (epic .github#176, Task 5) --

_ORPHAN = Orphan(IssueRef("org", "tool", 5), home="org/.github", name="org")
_RETIRED = RetiredLabels(IssueRef("org", "tool", 6), ("idea",))
_BODY_ONLY = (IssueRef("org", "tool", 7), IssueRef("org", ".github", 40))
_PUBLIC_QUEUE = IssueRef("org", "lab", 2)


def _derivations() -> dict[str, MagicMock]:
    return {
        "orphan_drift": MagicMock(return_value=[_ORPHAN]),
        "retired_label_drift": MagicMock(return_value=[_RETIRED]),
        "body_only_parents": MagicMock(return_value=[_BODY_ONLY]),
        "public_self_homed_queues": MagicMock(return_value=[_PUBLIC_QUEUE]),
        "private_under_public_epic": MagicMock(return_value=["org/secret#3"]),
    }


@contextlib.contextmanager
def _patched(patches: list[Any]) -> Generator[None]:
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _close_patches(**extra: MagicMock) -> list[Any]:
    base = {
        "task_drift": MagicMock(return_value=[]),
        "epic_drift": MagicMock(return_value=[]),
        "closed_epic_open_child": MagicMock(return_value=[]),
        "close_drift": MagicMock(return_value=[]),
        "reopen_epics_with_open_children": MagicMock(return_value=[]),
        **_derivations(),
        **extra,
    }
    return [patch(f"{_MOD}.epic_audit.{name}", mock) for name, mock in base.items()]


def test_vrg_epic_audit_preview_lists_orphans_without_mutating(
    capsys: pytest.CaptureFixture[str],
) -> None:
    issues = [object()]
    derived = _derivations()
    writers = {
        name: MagicMock()
        for name in (
            "attach_orphans",
            "strip_retired_labels",
            "nativize_parents",
            "move_public_queues",
        )
    }
    patches = [patch(f"{_MOD}.epic_audit.{n}", m) for n, m in {**derived, **writers}.items()]
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epic_audit.open_issues", return_value=issues) as enum,
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", return_value=[]),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        _patched(patches),
    ):
        rc = main([])
    assert rc == 0
    enum.assert_called_once_with("org")  # one shared enumeration
    for mock in derived.values():
        assert mock.call_args.kwargs["issues"] is issues
    for mock in writers.values():
        mock.assert_not_called()
    out = capsys.readouterr().out
    assert "org/tool#5 → Epic (intake): org in org/.github" in out
    assert "org/tool#6 — idea" in out
    assert "org/tool#7 → org/.github#40" in out
    assert "org/lab#2" in out
    assert "org/secret#3" in out


def test_vrg_epic_audit_close_applies_in_order(capsys: pytest.CaptureFixture[str]) -> None:
    order: list[str] = []
    scope = MagicMock()
    scope.return_value.__enter__ = lambda *_: order.append("enter")
    scope.return_value.__exit__ = lambda *_: order.append("exit") or False

    def _writer(name: str, result: int) -> MagicMock:
        return MagicMock(side_effect=lambda *_a, **_k: order.append(name) or result)

    writers = {
        "nativize_parents": _writer("nativize", 1),
        "strip_retired_labels": _writer("strip", 2),
        "move_public_queues": _writer("move", 3),
        "attach_orphans": _writer("attach", 4),
    }
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.github.target_org", scope),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        _patched(_close_patches(**writers)),
    ):
        rc = main(["--close"])
    assert rc == 0
    # The existing passes run in the first org-scoped block; the orphan pass
    # applies in its own, in the order nativize -> strip -> move -> attach.
    assert order == ["enter", "exit", "enter", "nativize", "strip", "move", "attach", "exit"]
    writers["nativize_parents"].assert_called_once_with([_BODY_ONLY])
    writers["strip_retired_labels"].assert_called_once_with([_RETIRED])
    writers["move_public_queues"].assert_called_once_with([_PUBLIC_QUEUE], org="org")
    writers["attach_orphans"].assert_called_once_with([_ORPHAN])
    out = capsys.readouterr().out
    assert "Body-only Parent: links nativized: 1." in out
    assert "Retired kind labels stripped: 2 issue(s)." in out
    assert "Public-repo queue items moved to org/.github: 3." in out
    assert "Orphans attached to intake: 4." in out
    assert "org/secret#3" in out  # the report-only check still surfaces


def test_vrg_epic_audit_close_reports_full_queue_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    full = StandingEpicFull(
        IssueRef("org", ".github", 100),
        95,
        [IssueRef("org", "tool", n) for n in (5, 6, 7)],
    )
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        _patched(
            _close_patches(
                nativize_parents=MagicMock(return_value=0),
                strip_retired_labels=MagicMock(return_value=0),
                move_public_queues=MagicMock(return_value=0),
                attach_orphans=MagicMock(side_effect=full),
            )
        ),
    ):
        rc = main(["--close"])
    assert rc == 1
    captured = capsys.readouterr()
    assert (
        "intake epic org/.github#100 is full (95 open); 3 orphan(s) not attached: "
        "org/tool#5, org/tool#6, org/tool#7. Run intake-review." in captured.err
    )
    assert "Orphans attached" not in captured.out


def test_vrg_epic_audit_close_stops_when_queue_move_overflows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    full = StandingEpicFull(
        IssueRef("org", ".github", 101),
        95,
        [IssueRef("org", "lab", 10)],
        kind="planning",
        noun="queue item(s) not moved",
    )
    attach = MagicMock()
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        _patched(
            _close_patches(
                nativize_parents=MagicMock(return_value=0),
                strip_retired_labels=MagicMock(return_value=0),
                move_public_queues=MagicMock(side_effect=full),
                attach_orphans=attach,
            )
        ),
    ):
        rc = main(["--close"])
    assert rc == 1
    attach.assert_not_called()  # a full queue stops the pass; nothing is skipped past
    assert "planning epic org/.github#101 is full" in capsys.readouterr().err
