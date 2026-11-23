"""Tests for the ``vrg-dependabot-audit`` CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_dependabot_audit
from vergil_tooling.lib import github
from vergil_tooling.lib.dependabot_alerts import Alert, AuditUnavailable

_FETCH = "vergil_tooling.bin.vrg_dependabot_audit.fetch_open_alerts"
_TARGET_ORG = "vergil_tooling.bin.vrg_dependabot_audit.github.target_org"

_ALERT = Alert(
    severity="critical",
    ecosystem="pip",
    package="requests",
    manifest_path="uv.lock",
    ghsa_id="GHSA-xxxx-yyyy-zzzz",
    html_url="https://github.com/o/r/security/dependabot/1",
)


def test_cli_clean_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_FETCH, return_value=[]):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 0
    assert capsys.readouterr().out.strip() == "o/r: no open Dependabot alerts"


def test_report_never_prints_alert_detail(capsys: pytest.CaptureFixture[str]) -> None:
    canary = Alert(
        severity="high",
        ecosystem="npm",
        package="leftpad-canary",
        manifest_path="package-lock.json",
        ghsa_id="GHSA-xxxx-canary",
        html_url="https://github.com/o/r/security/dependabot/7",
    )
    with patch(_FETCH, return_value=[_ALERT, canary]):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "o/r: 2 open Dependabot alerts: 1 critical, 1 high, 0 medium, 0 low\n"
        "Review or dismiss at https://github.com/o/r/security/dependabot\n"
    )
    assert captured.err == ""
    for detail in ("leftpad-canary", "GHSA-xxxx-canary", "requests", "package-lock.json"):
        assert detail not in captured.out


def test_cli_scopes_token_to_repo_owner() -> None:
    with (
        patch(_FETCH, return_value=[]),
        patch(_TARGET_ORG, wraps=github.target_org) as mock_target,
    ):
        assert vrg_dependabot_audit.main(["--repo", "acme/widgets"]) == 0
    mock_target.assert_called_once_with("acme")


def test_audit_alerts_disabled_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    err = AuditUnavailable("Dependabot alerts are disabled for o/r")
    with patch(_FETCH, side_effect=err):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "vrg-dependabot-audit: Dependabot alerts are disabled for o/r"
    assert captured.out == ""


def test_audit_permission_denied_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    err = github.MissingGitHubTokenError(4, ("gh", "api"), "", "set the GH_TOKEN environment")
    with patch(_FETCH, side_effect=err):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 2
    assert "no GitHub token available" in capsys.readouterr().err


def test_cli_no_installation_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    err = github.NoInstallationError("o", ["other"])
    with patch(_FETCH, side_effect=err):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 2
    assert "no installation for owner 'o'" in capsys.readouterr().err


def test_cli_app_auth_error_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    err = github.AppAuthError("o", "123", Path("/k.pem"), "boom")
    with patch(_FETCH, side_effect=err):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 2
    assert capsys.readouterr().err.startswith("vrg-dependabot-audit: ")


@pytest.mark.parametrize("repo", ["noslash", "/r", "o/", "o/r/x"])
def test_cli_rejects_malformed_repo(repo: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        vrg_dependabot_audit.main(["--repo", repo])
    assert exc_info.value.code == 2
    assert "OWNER/REPO" in capsys.readouterr().err


def test_cli_requires_repo() -> None:
    with pytest.raises(SystemExit) as exc_info:
        vrg_dependabot_audit.main([])
    assert exc_info.value.code == 2
