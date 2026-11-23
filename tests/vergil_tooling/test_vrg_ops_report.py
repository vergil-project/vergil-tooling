"""Tests for the ``vrg-ops-report`` CLI (``bin/vrg_ops_report.py``)."""

from __future__ import annotations

from typing import Any

import pytest

from vergil_tooling.bin import vrg_ops_report
from vergil_tooling.lib import identity_mode, ops_report, trust
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


def test_unknown_result_exits_1(recorder: Recorder, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_ops_report.main([*ARGS, "--results", "success,neutral"]) == 1
    assert recorder.calls == []
    assert "--results" in capsys.readouterr().err


def test_bad_repo_is_a_usage_error(recorder: Recorder) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_ops_report.main(["--repo", "docs", "--job", "j", "--results", "failure"])
    assert exc.value.code == 2
