"""Tests for the ``vrg-ops-report`` CLI (``bin/vrg_ops_report.py``)."""

from __future__ import annotations

from typing import Any

import pytest

from vergil_tooling.bin import vrg_ops_report
from vergil_tooling.lib import epic_chain, epics, github, identity_mode, ops_report, trust
from vergil_tooling.lib.automation_identity import AutomationIdentityError
from vergil_tooling.lib.identity_mode import IdentityMode

RUN = "https://github.com/acme/docs/actions/runs/7"
ARGS = ["--repo", "acme/docs", "--job", "docs-refresh", "--run-url", RUN]


class Recorder:
    def __init__(self, outcome: ops_report.Outcome = "created") -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.outcome = outcome

    def __call__(self, *args: Any, **kwargs: Any) -> ops_report.Outcome:
        self.calls.append((args, kwargs))
        return self.outcome


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    fake = Recorder()
    monkeypatch.setattr(ops_report, "report", fake)
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.AUTOMATION)
    return fake


@pytest.mark.parametrize("mode", [IdentityMode.HUMAN, IdentityMode.USER, IdentityMode.AUDIT])
def test_refuses_non_automation_mode(
    monkeypatch: pytest.MonkeyPatch,
    recorder: Recorder,
    capsys: pytest.CaptureFixture[str],
    mode: IdentityMode,
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: mode)
    assert vrg_ops_report.main([*ARGS, "--results", "failure"]) == 1
    assert recorder.calls == []
    assert f"identity mode {mode.value!r}" in capsys.readouterr().err


def test_refuses_unverified_automation_token(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    def unverified() -> IdentityMode:
        raise AutomationIdentityError("the token is not the automation App's")

    monkeypatch.setattr(identity_mode, "current_mode", unverified)
    assert vrg_ops_report.main([*ARGS, "--results", "failure"]) == 1
    assert recorder.calls == []
    assert "not the automation App's" in capsys.readouterr().err


def test_trust_read_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> ops_report.Outcome:
        raise trust.TrustReadError("search_items issue", "HTTP 502")

    monkeypatch.setattr(ops_report, "report", broken)
    assert vrg_ops_report.main([*ARGS, "--results", "failure"]) == 1
    assert "cannot read who wrote the content" in capsys.readouterr().err


def test_report_error_exits_1(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> ops_report.Outcome:
        raise ops_report.OpsReportError("created acme/docs#100 but it is still not listed")

    monkeypatch.setattr(ops_report, "report", broken)
    assert vrg_ops_report.main([*ARGS, "--results", "failure"]) == 1
    assert "acme/docs#100" in capsys.readouterr().err


def test_epic_refusal_exits_1_in_one_line(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal of the chain layer is named on stderr in one line (epic .github#1609)."""
    error = epic_chain.RolloverDisabledError(epics.IssueRef("acme", ".github", 900))

    def refused(*_args: Any, **_kwargs: Any) -> ops_report.Outcome:
        raise error

    monkeypatch.setattr(ops_report, "report", refused)
    assert vrg_ops_report.main([*ARGS, "--results", "failure"]) == 1
    captured = capsys.readouterr()
    assert captured.err == f"vrg-ops-report: acme/docs / docs-refresh: {error}\n"
    assert captured.out == ""


def test_results_combined_from_cli(recorder: Recorder, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_ops_report.main([*ARGS, "--results", "success,failure,skipped", "--step", "x"]) == 0
    [(args, kwargs)] = recorder.calls
    assert args == ("acme/docs", "docs-refresh", "failure")
    assert kwargs["run_url"] == RUN
    assert kwargs["step"] == "x"
    assert "acme/docs / docs-refresh: failure: created" in capsys.readouterr().out


def test_all_skipped_reports_none(recorder: Recorder, capsys: pytest.CaptureFixture[str]) -> None:
    recorder.outcome = "nothing"
    assert vrg_ops_report.main([*ARGS, "--results", "skipped,skipped"]) == 0
    [(args, _)] = recorder.calls
    assert args[2] is None
    assert "every job skipped" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [
        ("--results", "success,neutral", "unknown job result"),
        ("--job", 'x" in:body "y', "invalid job name"),
        ("--run-url", "http://example.invalid/run", "one https:// URL"),
        ("--step", "a\nb", "one non-empty line"),
    ],
)
def test_invalid_value_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    recorder: Recorder,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", _never_resolved)
    argv = {"--repo": "acme/docs", "--job": "docs-refresh", "--results": "failure"}
    argv["--run-url"] = RUN
    argv[flag] = value
    with pytest.raises(SystemExit) as exc:
        vrg_ops_report.main([arg for pair in argv.items() for arg in pair])
    assert exc.value.code == 2
    assert recorder.calls == []
    err = capsys.readouterr().err
    assert flag in err
    assert message in err


def _never_resolved() -> IdentityMode:
    raise AssertionError("a usage error must stop before the identity check")


def test_missing_token_reaches_the_cli_boundary(
    monkeypatch: pytest.MonkeyPatch, recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_token(*_args: Any, **_kwargs: Any) -> ops_report.Outcome:
        raise github.MissingGitHubTokenError(1, ["gh"], stderr="gh: GH_TOKEN environment variable")

    monkeypatch.setattr(ops_report, "report", no_token)
    with pytest.raises(SystemExit) as exc:
        vrg_ops_report.main([*ARGS, "--results", "failure"])
    assert exc.value.code == 1
    assert "vrg-ops-report" in capsys.readouterr().err


def test_bad_repo_is_a_usage_error(recorder: Recorder) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_ops_report.main(["--repo", "docs", "--job", "j", "--results", "failure"])
    assert exc.value.code == 2
