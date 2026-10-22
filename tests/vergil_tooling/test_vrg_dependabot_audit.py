"""Tests for the ``vrg-dependabot-audit`` CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_dependabot_audit
from vergil_tooling.lib import github
from vergil_tooling.lib.dependabot_alerts import Alert, AuditUnavailable

_FETCH = "vergil_tooling.bin.vrg_dependabot_audit.fetch_open_alerts"
_VISIBILITY = "vergil_tooling.bin.vrg_dependabot_audit.github.repo_visibility"
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
    with patch(_FETCH, return_value=[]), patch(_VISIBILITY) as mock_vis:
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 0
    assert capsys.readouterr().out.strip() == "o/r: no open Dependabot alerts"
    mock_vis.assert_not_called()


def test_cli_alerts_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_FETCH, return_value=[_ALERT]), patch(_VISIBILITY, return_value="PRIVATE"):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 1
    out = capsys.readouterr().out
    assert "1 open Dependabot alert: 1 critical, 0 high, 0 medium, 0 low" in out
    assert "requests" in out
    assert "GHSA-xxxx-yyyy-zzzz" in out


@pytest.mark.parametrize("visibility", ["PUBLIC", "INTERNAL"])
def test_cli_non_private_repo_counts_only(
    visibility: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch(_FETCH, return_value=[_ALERT]), patch(_VISIBILITY, return_value=visibility):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 1
    out = capsys.readouterr().out
    assert "1 critical" in out
    assert "https://github.com/o/r/security/dependabot" in out
    assert "requests" not in out
    assert "GHSA-" not in out


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


def test_cli_visibility_unresolvable_uses_public_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = github.GitHubAPIError(1, ("gh", "repo", "view"), "", "HTTP 403")
    with patch(_FETCH, return_value=[_ALERT]), patch(_VISIBILITY, side_effect=err):
        assert vrg_dependabot_audit.main(["--repo", "o/r"]) == 1
    captured = capsys.readouterr()
    assert (
        "could not resolve visibility for o/r; using the public (counts-only) format"
        in captured.err
    )
    assert "GHSA-" not in captured.out
    assert "requests" not in captured.out
    assert "1 critical" in captured.out


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
