"""Tests for vergil_tooling.bin.vrg_epic_audit."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.bin.vrg_epic_audit import main
from vergil_tooling.lib import epic_audit, github, proxies, roadmap, trust
from vergil_tooling.lib.epic_audit import (
    BodyParents,
    EpicDocsFinding,
    MisplacedArchive,
    Orphan,
    RetiredLabels,
    StandingEpicFull,
)
from vergil_tooling.lib.epics import ChildState, IssueRef
from vergil_tooling.lib.security_records import Advisory, SecurityIssue
from vergil_tooling.lib.trusted_authors import (
    OrgConfig,
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

_MOD = "vergil_tooling.bin.vrg_epic_audit"
_SEC = f"{_MOD}.security_records"
_PX = "vergil_tooling.lib.proxies"
# The real check, taken before the autouse fixture stubs it out.
_PROXY_LINK_DRIFT = epic_audit.proxy_link_drift
_PROXY_CLOSURES = epic_audit.proxy_closures
# The orgs the tests audit, each a Vergil org with a stanza of its own.
_ORGS = ("org", "owner", "lab-org", "vergil-project", "logical-minds-foundry")
AUTHORS = TrustedAuthors(
    human=("human",),
    agents=("vergil-user",),
    orgs={org: OrgConfig(f"{org}-automation") for org in _ORGS},
)


@pytest.fixture(autouse=True)
def standing_drain() -> Iterator[MagicMock]:
    # Every audit path also drains the intake/planning queue epics; stub it out
    # by default so tests that do not exercise it make no GitHub calls. Tests
    # that do exercise it patch drain_standing_org themselves (innermost wins).
    # Likewise the org-wide open-issue enumeration behind the orphan pass: an
    # empty org makes every pass a no-op with no GitHub calls. The org-wide
    # path's <org>/.github existence probe answers "exists" by default. The
    # security-records pass reads an org with no security issues or advisories.
    # The home's epic listing is empty and the Epic documents check clean.
    with (
        patch(f"{_MOD}.load_trusted_authors", return_value=AUTHORS),
        patch(f"{_MOD}.epics.drain_standing_org", return_value=[]) as mock_drain,
        patch(f"{_MOD}.epic_audit.open_issues", return_value=[]),
        patch(f"{_MOD}.epic_audit.list_home_epics", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_documents", return_value=[]),
        patch(f"{_MOD}.epics.github.repo_exists", return_value=True),
        patch(f"{_SEC}.security_issues", return_value=[]),
        patch(f"{_SEC}.advisory_home", side_effect=lambda org: f"{org}/.github"),
        patch(f"{_SEC}.list_advisories", return_value=[]),
        # No epic names a proxy and none is one; the org has no proxy link drift.
        patch(f"{_PX}.proxies_of", return_value=()),
        patch(f"{_PX}.is_proxy_ref", return_value=False),
        patch(f"{_MOD}.epic_audit.proxy_link_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.proxy_closures", return_value=([], [])),
        patch(f"{_MOD}.epic_audit.misplaced_adhoc", return_value=[]),
    ):
        yield mock_drain


def test_main_org_wide_without_dotgithub_fails_clearly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An owner with no .github has no org-wide home: one clear line and exit 1,
    # before any audit query runs (#3613).
    with (
        patch(f"{_MOD}.github.detect_org", return_value="owner"),
        patch(f"{_MOD}.epics.github.repo_exists", return_value=False),
        patch(f"{_MOD}.epic_audit.task_drift") as drift,
        pytest.raises(SystemExit) as excinfo,
    ):
        main([])
    assert excinfo.value.code == 1
    drift.assert_not_called()
    err = capsys.readouterr().err
    assert err == (
        "vrg-epic-audit: owner has no .github repository, so there is no org-wide "
        "queue or epic home; create the owner/.github repository to give it one\n"
    )


def test_main_org_wide_dotgithub_probe_error_fails_loud(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(
            f"{_MOD}.epics.github.repo_exists",
            side_effect=github.GitHubAPIError(1, "cmd", "", "HTTP 502: boom"),
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        main([])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-epic-audit: cannot check for org/.github")
    assert "HTTP 502: boom" in err


def _report_patches() -> list[Any]:
    return [
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", return_value=[]),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
    ]


def test_main_lists_home_epics_once_for_both_per_epic_checks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    listing = [{"number": 4}]
    finding = EpicDocsFinding(4, "OPEN", "no Documents section")
    with (
        _patched(_report_patches()),
        patch(f"{_MOD}.epic_audit.list_home_epics", return_value=listing) as list_epics,
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]) as ceoc,
        patch(f"{_MOD}.epic_audit.epic_documents", return_value=[finding]) as documents,
    ):
        rc = main([])
    list_epics.assert_called_once_with("org/.github")
    assert ceoc.call_args.kwargs["home"] == "org/.github"
    assert ceoc.call_args.kwargs["listing"] is listing
    assert documents.call_args.kwargs == {"home": "org/.github", "listing": listing}
    out = capsys.readouterr().out
    assert "## Epic documents" in out
    assert "- org/.github#4 (open) — no Documents section" in out
    assert rc == 0  # report-only: a finding does not change the exit status


def test_main_lists_home_epics_under_the_home_owner_token() -> None:
    seen: list[str | None] = []

    def fake_list(home: str) -> list[Any]:
        assert home == "lab-org/.github"
        seen.append(github._target_org.get())
        return []

    with (
        _patched(_report_patches()),
        patch(f"{_MOD}.github.detect_org", return_value="lab-org"),
        patch(f"{_MOD}.epic_audit.list_home_epics", side_effect=fake_list),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
    ):
        assert main([]) == 0
    assert seen == ["lab-org"]


def test_main_reads_the_whole_audit_under_the_audited_org_token() -> None:
    # Every read uses the audited org's token, never a cwd-scoped one.
    seen: dict[str, str | None] = {}

    def record(name: str, value: Any) -> Any:
        def fake(*_args: object, **_kwargs: object) -> Any:
            seen[name] = github._target_org.get()
            return value

        return fake

    with (
        patch(f"{_MOD}.github.detect_org", return_value="lab-org"),
        patch(f"{_MOD}.epics.require_org_home", side_effect=record("home", "lab-org/.github")),
        patch(f"{_MOD}.epic_audit.task_drift", side_effect=record("tasks", [])),
        patch(f"{_MOD}.epic_audit.epic_drift", side_effect=record("epics", [])),
        patch(f"{_MOD}.epic_audit.open_issues", side_effect=record("issues", [])),
        patch(f"{_MOD}.epic_audit.operational_pending", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_operational_without_success", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_outside_dotgithub", side_effect=record("outside", [])),
        patch(f"{_MOD}.epic_audit.stray_dotgithub_issue", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epics.drain_adhoc_org", side_effect=record("drain", [])),
    ):
        assert main([]) == 0
    assert seen == dict.fromkeys(
        ("home", "tasks", "epics", "outside", "drain", "issues"), "lab-org"
    )


def _stopping_pending(*, org: str, stops: trust.BatchStops) -> list[epic_audit.OperationalStatus]:
    """``operational_pending`` that stops one untrusted child (#4971) and reports it pending."""
    del org
    child = IssueRef("org", "repo", 9)
    item = trust.Skipped("org/repo", 9, "issue", "drive-by", "2026-11-01", "body", "untrusted")
    stops.add(item, item.cause)
    epic = IssueRef("org", ".github", 115)
    return [epic_audit.OperationalStatus(epic, (), (), {child: "validation"}, (child,))]


def _run_stopping_audit(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with (
        _patched(_report_patches()),
        patch(
            f"{_MOD}.epic_audit.operational_pending",
            side_effect=lambda org, *, stops: _stopping_pending(org=org, stops=stops),
        ),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
    ):
        rc = main([])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_main_stopped_child_is_reported_and_exits_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A batch run stops the child, not the audit (spec §5.3; #4971).
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    rc, out, err = _run_stopping_audit(capsys)
    assert rc == 1
    assert "- org/.github#115 — runnable: none; blocked: none; stopped: 1 (see stderr)" in out
    assert err == "stopped: org/repo#9 issue drive-by at 2026-11-01 on body (untrusted)\n"


def test_main_stopped_child_under_actions_is_counted_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc, out, err = _run_stopping_audit(capsys)
    assert rc == 1
    assert err == "stopped 1 item(s) (untrusted: 1)\n"
    assert "org/repo#9" not in out + err
    assert "drive-by" not in out + err


def test_close_does_not_run_the_epic_documents_check() -> None:
    documents = MagicMock(return_value=[])
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.close_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_documents", documents),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
    ):
        assert main(["--close"]) == 0
    documents.assert_not_called()


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


def test_main_bare_epic_resolves_against_cwd_epic_home() -> None:
    """A bare '--epic #N' reads against the current repo's epic home (#3415).

    The home's existence check runs under the current repo owner's App token.
    """
    seen_orgs: list[str | None] = []
    expected_epic = IssueRef("org", ".github", 407)

    def exists(owner: str, name: str) -> bool:
        assert (owner, name) == ("org", ".github")
        seen_orgs.append(github._target_org.get())
        return True

    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch("vergil_tooling.lib.github.repo_exists", side_effect=exists),
        patch(f"{_MOD}.epics.is_epic", return_value=True) as ie,
        patch(f"{_MOD}.epics.child_states", return_value=[]) as cs,
    ):
        rc = main(["--epic", "#407"])
    assert rc == 0
    ie.assert_called_once_with(expected_epic)
    assert cs.call_args.args[0] == expected_epic
    assert seen_orgs == ["org"]


def test_main_qualified_epic_ref_skips_home_lookup() -> None:
    """A full 'owner/repo#N' is used as written, with no home check."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch("vergil_tooling.lib.github.repo_exists") as probe,
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
    close_drift.assert_called_once_with(["T"], ["E"], org="vergil-project")
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
    close_drift.assert_called_once_with(["T"], ["E"], org="vergil-project")
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
    # scoped to the org (spec §7); main() scopes the whole audit to it (#3513).
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
    # One block scopes the whole audit, the drain included, to the org (#3513).
    assert order == ["enter", "drain", "exit"]


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
    # The drains run inside the one block that scopes the whole audit to the
    # org (#3513).
    assert order == ["enter", "standing", "exit"]
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


# -- orphan pass and label retirement (epic .github#176, Task 5) ---------------

_ORPHAN = Orphan(IssueRef("org", "tool", 5), home="org/.github", name="org")
_RETIRED = RetiredLabels(IssueRef("org", "tool", 6), ("idea",))
_BODY_ONLY = (IssueRef("org", "tool", 7), IssueRef("org", ".github", 40))
_MISPLACED = MisplacedArchive(IssueRef("org", ".github", 180), "missing the `archive` label")


def _derivations() -> dict[str, MagicMock]:
    return {
        "orphan_drift": MagicMock(return_value=[_ORPHAN]),
        "retired_label_drift": MagicMock(return_value=[_RETIRED]),
        "body_only_parents": MagicMock(return_value=BodyParents(pairs=(_BODY_ONLY,))),
        "misplaced_archives": MagicMock(return_value=[_MISPLACED]),
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
    assert "org/.github#180 — missing the `archive` label" in out


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
        "attach_orphans": _writer("attach", 3),
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
    # One block scopes the whole audit to the org (#3513); inside it the orphan
    # pass applies in the order nativize -> strip -> attach.
    assert order == ["enter", "nativize", "strip", "attach", "exit"]
    writers["nativize_parents"].assert_called_once_with([_BODY_ONLY])
    writers["strip_retired_labels"].assert_called_once_with([_RETIRED])
    writers["attach_orphans"].assert_called_once()
    assert writers["attach_orphans"].call_args.args == ([_ORPHAN],)
    assert writers["attach_orphans"].call_args.kwargs["authors"] is AUTHORS
    out = capsys.readouterr().out
    assert "Body-only Parent: links nativized: 1." in out
    assert "Retired kind labels stripped: 2 issue(s)." in out
    assert "Orphans attached to intake: 3." in out
    # The report-only check still surfaces.
    assert "org/.github#180 — missing the `archive` label" in out


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
                attach_orphans=MagicMock(side_effect=full),
            )
        ),
    ):
        rc = main(["--close"])
    assert rc == 1
    captured = capsys.readouterr()
    assert (
        "intake epic org/.github#100 is full (95 open); 3 orphan(s) not attached: "
        "org/tool#5, org/tool#6, org/tool#7. Run epic-review intake." in captured.err
    )
    assert "Orphans attached" not in captured.out


# -- security-records pass: preview only; --close runs none (vergil-tooling#4313) --

_GHSA = "GHSA-abcd-2345-wxyz"
_BROKEN_GHSA = "GHSA-zzzz-9999-aaaa"
_TRACKED = IssueRef("org", ".github", 10)


def _security_issue(
    ref: IssueRef = _TRACKED,
    *,
    open_: bool = False,
    ghsa_ids: tuple[str, ...] = (_GHSA,),
    reason: str | None = "completed",
) -> SecurityIssue:
    return SecurityIssue(ref, open_, ghsa_ids, None if open_ else reason)


def _advisory(
    ghsa: str = _GHSA, tracking: tuple[IssueRef, ...] = (_TRACKED,), **kw: Any
) -> Advisory:
    return Advisory(ghsa, kw.get("state", "draft"), "tool", tracking, kw.get("external", False))


def _run_close(
    *,
    issues: list[SecurityIssue],
    advisories: list[Advisory] | None,
    write: MagicMock,
    attach: MagicMock | None = None,
) -> int:
    # advisories=None leaves list_advisories to the caller's own patch.
    listed = (
        patch(f"{_SEC}.list_advisories", return_value=advisories)
        if advisories is not None
        else contextlib.nullcontext()
    )
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        patch(f"{_SEC}.security_issues", return_value=issues),
        listed,
        patch(f"{_SEC}.github.write_json", write),
        _patched(
            _close_patches(
                nativize_parents=MagicMock(return_value=0),
                strip_retired_labels=MagicMock(return_value=0),
                attach_orphans=attach or MagicMock(return_value=0),
            )
        ),
    ):
        return main(["--close"])


_PLANNING_EPIC = IssueRef("org", ".github", 501)


def _run_report(
    *,
    issues: list[SecurityIssue],
    advisories: list[Advisory] | None,
    write: MagicMock,
    parent: IssueRef | None = None,
) -> int:
    # advisories=None leaves list_advisories to the caller's own patch.
    # Every tracked issue's native parent is *parent*; _PLANNING_EPIC reads as
    # the live planning queue epic.
    def native_parent_of(_ref: IssueRef) -> IssueRef | None:
        return parent

    def labels_and_title(ref: IssueRef) -> tuple[set[str], str]:
        assert ref == _PLANNING_EPIC
        return {"epic", "planning"}, "Epic (planning): org"

    listed = (
        patch(f"{_SEC}.list_advisories", return_value=advisories)
        if advisories is not None
        else contextlib.nullcontext()
    )
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
        patch(f"{_SEC}.security_issues", return_value=issues),
        patch(f"{_SEC}.epics.native_parent_of", native_parent_of),
        patch(f"{_SEC}.epics._labels_and_title", labels_and_title),
        listed,
        patch(f"{_SEC}.github.write_json", write),
    ):
        return main([])


@pytest.mark.parametrize("orphan_pass_fails", [False, True])
def test_close_makes_no_security_advisory_call(
    capsys: pytest.CaptureFixture[str], *, orphan_pass_fails: bool
) -> None:
    # The sweep's token is scoped to Issues write, Pull requests read and
    # Contents read, so --close must not list, read or write a security
    # advisory (vergil-tooling#4313): vrg-security-record sync owns that pass.
    # A closed security issue with a draft advisory would sync if the pass ran.
    full = StandingEpicFull(IssueRef("org", ".github", 100), 95, [IssueRef("org", "tool", 5)])
    write = MagicMock()
    spies = {
        name: MagicMock(name=name)
        for name in (
            "records_pass",
            "advisory_home",
            "list_advisories",
            "read_text",
            "set_state",
            "sync_states",
        )
    }
    paginated = MagicMock(return_value=[])
    with (
        _patched([patch(f"{_SEC}.{name}", spy) for name, spy in spies.items()]),
        patch(f"{_SEC}.github.read_json_paginated", paginated),
    ):
        rc = _run_close(
            issues=[_security_issue()],
            advisories=None,
            write=write,
            attach=MagicMock(side_effect=full) if orphan_pass_fails else None,
        )
    assert rc == (1 if orphan_pass_fails else 0)
    for spy in spies.values():
        spy.assert_not_called()
    write.assert_not_called()
    assert not [c for c in paginated.call_args_list if "security-advisories" in str(c)]
    assert "Security records" not in capsys.readouterr().out


def test_close_help_names_the_sync_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--close makes no security-advisory call" in text
    assert "vrg-security-record sync" in text
    assert "set each paired draft advisory" not in text


def test_report_only_mode_previews_without_writes(capsys: pytest.CaptureFixture[str]) -> None:
    write = MagicMock()
    rc = _run_report(
        issues=[_security_issue()],
        advisories=[_advisory(), _advisory(_BROKEN_GHSA, tracking=())],
        write=write,
    )
    # The preview reports broken pairs without failing, like the rest of the
    # read-only report path.
    assert rc == 0
    write.assert_not_called()
    out = capsys.readouterr().out
    assert (
        "Security records (preview): 1 advisory state would sync, 1 broken pair, 0 held, 0 errors."
        in out
    )
    assert f"- broken: {_BROKEN_GHSA}" in out


def test_report_only_mode_previews_holds(capsys: pytest.CaptureFixture[str]) -> None:
    # A closed planning item is all that ended the advisory: the preview
    # surfaces the hold, apart from broken pairs, and does not fail.
    write = MagicMock()
    rc = _run_report(
        issues=[_security_issue()], advisories=[_advisory()], write=write, parent=_PLANNING_EPIC
    )
    assert rc == 0
    write.assert_not_called()
    out = capsys.readouterr().out
    assert (
        "Security records (preview): 0 advisory states would sync, 0 broken pairs, "
        "1 held, 0 errors." in out
    )
    assert (
        f"- held: {_GHSA} ({_TRACKED.slug}): retarget it to a fix task, "
        "or close it with vrg-security-record close"
    ) in out


def test_summary_line_pluralizes_two_broken_pairs(capsys: pytest.CaptureFixture[str]) -> None:
    second = "GHSA-cccc-cccc-cccc"
    rc = _run_report(
        issues=[],
        advisories=[_advisory(_BROKEN_GHSA, tracking=()), _advisory(second, tracking=())],
        write=MagicMock(),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "Security records (preview): 0 advisory states would sync, 2 broken pairs, "
        "0 held, 0 errors." in out
    )


def test_report_only_mode_fails_loud_when_the_read_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = github.GitHubAPIError(1, ["gh", "api"], None, "HTTP 403: Resource not accessible")
    with patch(f"{_SEC}.list_advisories", side_effect=error):
        rc = _run_report(issues=[_security_issue()], advisories=None, write=MagicMock())
    assert rc == 1
    captured = capsys.readouterr()
    assert "vrg-epic-audit: security records pass failed" in captured.err
    assert "Resource not accessible" in captured.err
    assert "Security records" not in captured.out


def _no_op_writers() -> dict[str, MagicMock]:
    """The orphan pass's writers, each a no-op, for a --close run that is not about them."""
    names = ("nativize_parents", "strip_retired_labels", "attach_orphans")
    return {name: MagicMock(return_value=0) for name in names}


# --- proxies (epic .github#1417) --------------------------------------------------

_PRIMARY = IssueRef("vergil-project", ".github", 1417)
_PROXY = IssueRef("logical-minds-foundry", ".github", 320)


def test_vrg_epic_audit_epic_lists_proxy_children(capsys: pytest.CaptureFixture[str]) -> None:
    def child_states(epic: IssueRef) -> list[ChildState]:
        if epic == _PRIMARY:
            return [ChildState(IssueRef("vergil-project", "vergil-tooling", 1), "OPEN", "own")]
        return [ChildState(IssueRef("logical-minds-foundry", "mq-rest-admin", 7), "OPEN", "px")]

    with (
        patch(f"{_MOD}.github.current_repo", return_value="vergil-project/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
        patch(f"{_MOD}.epics.child_states", side_effect=child_states),
        patch(f"{_PX}.proxies_of", side_effect=lambda ref: (_PROXY,) if ref == _PRIMARY else ()),
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
        patch(f"{_PX}.is_proxy_ref", side_effect=lambda ref: ref == _PROXY),
    ):
        rc = main(["--epic", _PRIMARY.slug])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "primary" not in payload
    assert [(c["repo"], c["number"]) for c in payload["children"]] == [
        ("logical-minds-foundry/mq-rest-admin", 7),
        ("vergil-project/vergil-tooling", 1),
    ]


def test_vrg_epic_audit_epic_link_drift_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="vergil-project/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
        patch(f"{_MOD}.epics.child_states", return_value=[]) as cs,
        patch(f"{_PX}.proxies_of", return_value=(_PROXY,)),
        patch(f"{_PX}.is_proxy_ref", side_effect=lambda ref: ref == _PROXY),
        patch(f"{_PX}.primary_of", return_value=IssueRef("vergil-project", ".github", 9)),
    ):
        rc = main(["--epic", _PRIMARY.slug])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # no JSON
    assert captured.err == (
        "vrg-epic-audit: proxy link drift: vergil-project/.github#1417 lists the proxy"
        " logical-minds-foundry/.github#320, whose 'Primary:' line names"
        " vergil-project/.github#9\n"
    )
    cs.assert_not_called()


def test_vrg_epic_audit_epic_on_proxy_reports_primary(capsys: pytest.CaptureFixture[str]) -> None:
    children = [ChildState(IssueRef("logical-minds-foundry", "mq-rest-admin", 7), "OPEN", "px")]
    with (
        patch(f"{_MOD}.github.current_repo", return_value="logical-minds-foundry/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_PX}.proxies_of", side_effect=lambda ref: (_PROXY,) if ref == _PRIMARY else ()),
        patch(f"{_PX}.is_proxy_ref", side_effect=lambda ref: ref == _PROXY),
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
    ):
        rc = main(["--epic", _PROXY.slug])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["epic"] == _PROXY.slug
    assert payload["primary"] == _PRIMARY.slug
    assert [c["number"] for c in payload["children"]] == [7]


def test_vrg_epic_audit_epic_on_proxy_without_primary_comment_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="logical-minds-foundry/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
        patch(f"{_MOD}.epics.child_states", return_value=[]),
        patch(f"{_PX}.proxies_of", return_value=()),
        patch(f"{_PX}.is_proxy_ref", side_effect=lambda ref: ref == _PROXY),
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
    ):
        rc = main(["--epic", _PROXY.slug])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "has no trusted 'Proxy: logical-minds-foundry/.github#320' comment" in captured.err


def test_org_audit_reports_proxy_link_drift(capsys: pytest.CaptureFixture[str]) -> None:
    # An open proxy whose primary lacks its Proxy: comment is reported; the
    # read-only report still exits 0.
    listing = [{"number": 320, "state": "OPEN", "labels": ["epic", "proxy"]}]
    with (
        _patched(_report_patches()),
        patch(f"{_MOD}.github.detect_org", return_value="logical-minds-foundry"),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.list_home_epics", return_value=listing),
        patch(f"{_MOD}.epic_audit.proxy_link_drift", wraps=_PROXY_LINK_DRIFT) as drift,
        patch(f"{_PX}.primary_of", return_value=_PRIMARY),
        patch(f"{_PX}.proxies_of", return_value=()),
    ):
        rc = main([])
    assert rc == 0
    drift.assert_called_once()
    assert drift.call_args.args == ("logical-minds-foundry/.github",)
    assert drift.call_args.kwargs["listing"] is listing
    out = capsys.readouterr().out
    assert "## Proxy link drift" in out
    assert (
        "- proxy logical-minds-foundry/.github#320 names vergil-project/.github#1417 as its"
        " primary, but vergil-project/.github#1417 has no trusted"
        " 'Proxy: logical-minds-foundry/.github#320' comment"
    ) in out


def test_close_runs_trusted_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The sweep reads the delivered configuration, so its trusted-comment
    # checks run under --close too (spec §5.1a of epic .github#1467), their
    # findings in the closed report's report-only section, and every trusted
    # pass shares the run's one BatchStops and the one loaded configuration.
    monkeypatch.setenv("VRG_EPIC_SWEEP", "1")
    task = IssueRef("org", "tool", 3)
    drift = MagicMock(return_value=["proxy org/.github#4 broke"])
    misplaced = MagicMock(
        return_value=[epic_audit.MisplacedTask(task, "titled `Logbook:`; it belongs elsewhere")]
    )
    passes = _close_patches(**_no_op_writers())
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        patch(f"{_MOD}.epic_audit.proxy_link_drift", drift),
        patch(f"{_MOD}.epic_audit.misplaced_adhoc", misplaced),
        patch(f"{_MOD}.epic_audit.proxy_closures", return_value=([], [])) as closures,
        _patched(passes),
    ):
        assert main(["--close"]) == 0
    drift.assert_called_once()
    misplaced.assert_called_once()
    out = capsys.readouterr().out
    assert "## Needs a human (report only)" in out
    assert "- proxy org/.github#4 broke" in out
    assert "- org/tool#3 — titled `Logbook:`; it belongs elsewhere" in out
    stops = drift.call_args.kwargs["stops"]
    assert closures.call_args.kwargs["stops"] is stops
    assert closures.call_args.kwargs["authors"] is AUTHORS
    trusted = ("task_drift", "epic_drift", "closed_epic_open_child", "body_only_parents")
    for name in trusted:
        mock = next(p.new for p in passes if p.attribute == name)
        assert mock.call_args.kwargs["stops"] is stops, name
        assert mock.call_args.kwargs["authors"] is AUTHORS, name


def test_vrg_epic_audit_epic_proxy_link_error_while_listing_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The link check passed, but the listing hit a broken link (a proxy edited
    # in between): still exit 1 with no JSON.
    with (
        patch(f"{_MOD}.github.current_repo", return_value="vergil-project/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
        patch(f"{_MOD}.epic_audit.proxy_link_problems", return_value=[]),
        patch(
            f"{_MOD}.epic_audit.epic_children",
            side_effect=proxies.ProxyLinkError("proxy has no 'Primary:' line"),
        ),
    ):
        rc = main(["--epic", _PRIMARY.slug])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "vrg-epic-audit: proxy link drift: proxy has no 'Primary:' line\n"


# --- the sweep's proxy close pass (epic .github#1417) -----------------------------

_CLOSED_PRIMARY = IssueRef("vergil-project", ".github", 1417)


def _proxy_listing(*numbers: int) -> list[dict[str, Any]]:
    return [{"number": n, "state": "OPEN", "labels": ["epic", "proxy"]} for n in numbers]


def test_proxy_unreadable_primary_reported_and_run_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # One proxy's Primary: line is broken, the other's primary is closed and
    # empty: the good one is closed, the broken one is on stderr and in the
    # closed report, and the run exits 1 once every pass has run.
    monkeypatch.setenv("VRG_EPIC_SWEEP", "1")

    def primary_of(ref: IssueRef) -> IssueRef:
        if ref.number == 3:
            raise proxies.ProxyLinkError("proxy org/.github#3 has no 'Primary:' line in its body")
        return _CLOSED_PRIMARY

    run = MagicMock()
    writers = _no_op_writers()
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        patch(f"{_MOD}.epic_audit.list_home_epics", return_value=_proxy_listing(3, 4)),
        patch(f"{_MOD}.epic_audit.proxy_closures", wraps=_PROXY_CLOSURES),
        patch(f"{_PX}.primary_of", side_effect=primary_of),
        patch(f"{_MOD}.epic_audit.epics.issue_state", return_value="CLOSED"),
        patch(f"{_MOD}.epic_audit.epics.child_states", return_value=[]),
        patch(f"{_MOD}.epic_audit.github.run", run),
        # The primary was closed by the trusted human.
        patch("vergil_tooling.lib.github.graphql", side_effect=trust_stubs.FakeItems()),
        _patched(_close_patches(**writers)),
    ):
        rc = main(["--close"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "vrg-epic-audit: proxy link error: proxy org/.github#3 has no 'Primary:' line in its body\n"
    )
    assert "## Proxies closed (primary closed, no open task)" in captured.out
    assert "- org/.github#4" in captured.out
    assert "- proxy org/.github#3 has no 'Primary:' line in its body" in captured.out
    assert run.call_count == 1
    assert run.call_args.args[:5] == ("issue", "close", "4", "--repo", "org/.github")
    for writer in writers.values():
        writer.assert_called_once()


def test_proxy_link_error_does_not_stop_other_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_EPIC_SWEEP", "1")
    reopen = MagicMock(return_value=[])
    close_proxies = MagicMock(return_value=[])
    writers = _no_op_writers()
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]) as drain,
        patch(
            f"{_MOD}.epic_audit.proxy_closures", return_value=([], ["proxy org/.github#3 broke"])
        ),
        patch(f"{_MOD}.epic_audit.close_proxies", close_proxies),
        _patched(_close_patches(reopen_epics_with_open_children=reopen, **writers)),
    ):
        rc = main(["--close"])
    assert rc == 1
    reopen.assert_called_once()
    drain.assert_called_once()
    close_proxies.assert_called_once_with([])
    for writer in writers.values():
        writer.assert_called_once()


def test_close_runs_close_proxies_after_close_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_EPIC_SWEEP", "1")
    order: list[str] = []
    plan = [
        epic_audit.ProxyClosure(IssueRef("org", ".github", 4), _CLOSED_PRIMARY, 0),
    ]
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        patch(f"{_MOD}.epic_audit.proxy_closures", return_value=(plan, [])),
        patch(
            f"{_MOD}.epic_audit.close_proxies",
            side_effect=lambda closures: order.append("proxies") or [],
        ) as close_proxies,
        _patched(
            _close_patches(
                close_drift=MagicMock(side_effect=lambda *_a, **_k: order.append("drift") or []),
                **_no_op_writers(),
            )
        ),
    ):
        assert main(["--close"]) == 0
    assert order == ["drift", "proxies"]
    close_proxies.assert_called_once_with(plan)


def test_preview_lists_proxy_closures(capsys: pytest.CaptureFixture[str]) -> None:
    plan = [
        epic_audit.ProxyClosure(IssueRef("org", ".github", 4), _CLOSED_PRIMARY, 0),
        epic_audit.ProxyClosure(IssueRef("org", ".github", 5), _CLOSED_PRIMARY, 2),
    ]
    close_proxies = MagicMock()
    with (
        _patched(_report_patches()),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.proxy_closures", return_value=(plan, [])),
        patch(f"{_MOD}.epic_audit.close_proxies", close_proxies),
    ):
        rc = main([])
    assert rc == 0
    close_proxies.assert_not_called()
    out = capsys.readouterr().out
    assert "## Proxies whose primary has closed (`--close` closes the empty ones)" in out
    assert "- org/.github#4 — its primary vergil-project/.github#1417 is closed" in out
    assert (
        "- org/.github#5 — its primary vergil-project/.github#1417 is closed, but 2 task(s)"
        " under it are still open; left open"
    ) in out


def test_preview_with_a_proxy_link_error_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        _patched(_report_patches()),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(
            f"{_MOD}.epic_audit.proxy_closures", return_value=([], ["proxy org/.github#3 broke"])
        ),
    ):
        rc = main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err == "vrg-epic-audit: proxy link error: proxy org/.github#3 broke\n"
    assert "## Proxy link errors (the run fails; fix each link by hand)" in captured.out
    assert "- proxy org/.github#3 broke" in captured.out


# --- misplaced ad-hoc tasks (epic .github#1417) -----------------------------------


@pytest.mark.parametrize("close", [False, True], ids=["report", "close"])
def test_audit_without_configuration_exits_1_before_any_pass(
    close: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every pass reads through the trust layer, so a missing configuration
    # stops the run before any read or write, with or without --close.
    error = TrustedAuthorsNotConfiguredError("trusted authors are not configured: no file")
    drift = MagicMock()
    with (
        patch.dict("os.environ", {"VRG_EPIC_SWEEP": "1"}),
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.load_trusted_authors", side_effect=error),
        patch(f"{_MOD}.epic_audit.task_drift", drift),
    ):
        rc = main(["--close"] if close else [])
    assert rc == 1
    drift.assert_not_called()
    captured = capsys.readouterr()
    assert captured.err == "vrg-epic-audit: trusted authors are not configured: no file\n"
    assert captured.out == ""


@pytest.mark.parametrize("close", [False, True], ids=["report", "close"])
def test_audit_skips_and_reports_unlisted_org(
    close: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    # An org with no [orgs.<org>] stanza is not a Vergil org: it is reported
    # and never swept, with a clean exit (spec §5.1a of epic .github#1467).
    repo_exists = MagicMock(return_value=True)
    drift = MagicMock()
    with (
        patch.dict("os.environ", {"VRG_EPIC_SWEEP": "1"}),
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="Stranger"),
        patch(f"{_MOD}.epics.github.repo_exists", repo_exists),
        patch(f"{_MOD}.epic_audit.task_drift", drift),
    ):
        rc = main(["--close"] if close else [])
    assert rc == 0
    drift.assert_not_called()
    repo_exists.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "vrg-epic-audit: Stranger is not a Vergil org (the configuration has no"
        " [orgs.Stranger] stanza); it is not swept\n"
    )


def test_audit_matches_a_listed_org_case_insensitively() -> None:
    # GitHub org names ignore case, as the configuration's lookup does.
    drift = MagicMock(return_value=[])
    with (
        _patched(_report_patches()),
        patch(f"{_MOD}.github.detect_org", return_value="ORG"),
        patch(f"{_MOD}.epic_audit.task_drift", drift),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
    ):
        main([])
    drift.assert_called_once()


def test_report_renders_misplaced_adhoc(capsys: pytest.CaptureFixture[str]) -> None:
    issues = [object()]
    task = IssueRef("org", "tool", 3)
    misplaced = MagicMock(
        return_value=[
            epic_audit.MisplacedTask(task, "titled `Logbook:`; it belongs under the hoshin epic")
        ]
    )
    with (
        _patched(_report_patches()),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch(f"{_MOD}.epic_audit.open_issues", return_value=issues),
        _patched([patch(f"{_MOD}.epic_audit.{n}", m) for n, m in _derivations().items()]),
        patch(f"{_MOD}.epic_audit.misplaced_adhoc", misplaced),
    ):
        rc = main([])
    assert rc == 0  # report-only
    misplaced.assert_called_once_with("org", issues=issues)
    assert (
        "- org/tool#3 — titled `Logbook:`; it belongs under the hoshin epic"
        in capsys.readouterr().out
    )


# --- per-item stops under --close (epic .github#1467, Task 8b) ---------------------


def _summaries(*numbers: int) -> list[Any]:
    return [
        roadmap.EpicSummary(n, f"Epic {n}", "2026-06-01", None, (), 1, 1, f"u{n}") for n in numbers
    ]


def _run_close_with_an_outsider_close(
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str, MagicMock]:
    """A sweep --close over two all-closed epics, #4's child closed by an outsider."""
    run = MagicMock()
    items = trust_stubs.FakeItems(closers={("org/tool", 40): trust_stubs.OUTSIDER})
    with (
        patch.dict("os.environ", {"VRG_EPIC_SWEEP": "1"}),
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.closed_epic_open_child", return_value=[]),
        patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=_summaries(4, 5)),
        patch(
            "vergil_tooling.lib.epics.child_states",
            side_effect=lambda epic: [
                ChildState(IssueRef("org", "tool", epic.number * 10), "CLOSED")
            ],
        ),
        patch("vergil_tooling.lib.github.graphql", side_effect=items),
        patch("vergil_tooling.lib.github.run", run),
        _patched([patch(f"{_MOD}.epic_audit.{n}", m) for n, m in _no_op_writers().items()]),
    ):
        rc = main(["--close"])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err, run


def test_close_stop_is_per_item_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An untrusted close stops its epic alone: the other epic is closed, every
    # later pass still runs, the stop is named on stderr (metadata only), and
    # the run exits 1 at the end (spec §5.3 of epic .github#1467).
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    rc, out, err, run = _run_close_with_an_outsider_close(capsys)
    assert rc == 1
    assert [c.args[:5] for c in run.call_args_list] == [
        ("issue", "close", "5", "--repo", "org/.github")
    ]
    assert "- org/.github#5" in out
    assert "org/.github#4" not in out
    assert "Orphans attached to intake: 0." in out
    assert err == (
        f"stopped: org/tool#40 event drive-by at {trust_stubs.EDITED} on close (untrusted)\n"
    )


def test_close_stop_under_actions_is_counted_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc, _out, err, _run = _run_close_with_an_outsider_close(capsys)
    assert rc == 1
    assert err == "stopped 1 item(s) (untrusted: 1)\n"
    assert "drive-by" not in err


@pytest.mark.parametrize(
    "error",
    [
        trust.UntrustedContentError(
            trust.Skipped(
                "logical-minds-foundry/.github", 320, "issue", "drive-by", "t", "body", "untrusted"
            ),
            "the issue body is untrusted",
        ),
        trust.TrustReadError("read_issue logical-minds-foundry/.github#320", "truncated"),
    ],
    ids=["untrusted", "unreadable"],
)
def test_vrg_epic_audit_epic_untrusted_proxy_body_exits_1(
    error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    # A proxy body the trust layer does not trust gives no link: the listing
    # stops, metadata only, with no JSON.
    with (
        patch(f"{_MOD}.github.current_repo", return_value="vergil-project/.github"),
        patch(f"{_MOD}.epics.is_epic", return_value=True),
        patch(f"{_MOD}.epic_audit.proxy_link_problems", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_children", side_effect=error),
    ):
        rc = main(["--epic", _PROXY.slug])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"vrg-epic-audit: {error}\n"
