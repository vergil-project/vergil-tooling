"""Tests for :mod:`vergil_tooling.lib.dependabot_alerts`."""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.dependabot_alerts import (
    SEVERITY_ORDER,
    Alert,
    AuditUnavailable,
    classify_error,
    fetch_open_alerts,
    render_report,
)

_PAGINATED = "vergil_tooling.lib.dependabot_alerts.github.read_json_paginated"


def _raw_alert(
    number: int = 1,
    *,
    severity: str = "high",
    ecosystem: str = "pip",
    package: str = "requests",
    manifest_path: str = "uv.lock",
    ghsa_id: str = "GHSA-xxxx-yyyy-zzzz",
) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/o/r/security/dependabot/{number}",
        "dependency": {
            "package": {"ecosystem": ecosystem, "name": package},
            "manifest_path": manifest_path,
        },
        "security_advisory": {"ghsa_id": ghsa_id, "severity": severity},
    }


def _alert(
    severity: str,
    package: str,
    *,
    number: int = 1,
    ghsa_id: str = "GHSA-aaaa-bbbb-cccc",
) -> Alert:
    return Alert(
        severity=severity,
        ecosystem="pip",
        package=package,
        manifest_path="uv.lock",
        ghsa_id=ghsa_id,
        html_url=f"https://github.com/o/r/security/dependabot/{number}",
    )


def _api_error(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "api"), "", stderr)


# --- fetch_open_alerts -------------------------------------------------------


def test_fetch_maps_fields() -> None:
    with patch(_PAGINATED, return_value=[_raw_alert()]) as mock_read:
        alerts = fetch_open_alerts("o/r")
    mock_read.assert_called_once_with("repos/o/r/dependabot/alerts?state=open")
    assert alerts == [
        Alert(
            severity="high",
            ecosystem="pip",
            package="requests",
            manifest_path="uv.lock",
            ghsa_id="GHSA-xxxx-yyyy-zzzz",
            html_url="https://github.com/o/r/security/dependabot/1",
        )
    ]


def test_audit_counts_all_pages() -> None:
    raw = [_raw_alert(n) for n in range(1, 151)]
    with patch(_PAGINATED, return_value=raw):
        assert len(fetch_open_alerts("o/r")) == 150


def test_fetch_empty_returns_no_alerts() -> None:
    with patch(_PAGINATED, return_value=[]):
        assert fetch_open_alerts("o/r") == []


def test_audit_alerts_disabled_exits_2() -> None:
    err = _api_error("gh: Dependabot alerts are disabled for this repository. (HTTP 403)")
    with (
        patch(_PAGINATED, side_effect=err),
        pytest.raises(AuditUnavailable, match=r"^Dependabot alerts are disabled for o/r$"),
    ):
        fetch_open_alerts("o/r")


def test_audit_permission_denied_exits_2() -> None:
    err = _api_error("HTTP 403: Resource not accessible by integration")
    with (
        patch(_PAGINATED, side_effect=err),
        pytest.raises(AuditUnavailable, match="Dependabot alerts: Read"),
    ):
        fetch_open_alerts("o/r")


def test_fetch_missing_token_is_unavailable() -> None:
    stderr = "gh: in a GitHub Actions workflow, set the GH_TOKEN environment variable."
    err = github.MissingGitHubTokenError(4, ("gh", "api"), "", stderr)
    with patch(_PAGINATED, side_effect=err), pytest.raises(AuditUnavailable, match="o/r"):
        fetch_open_alerts("o/r")


def test_fetch_bare_called_process_error_is_unavailable() -> None:
    err = subprocess.CalledProcessError(1, ("gh", "api"))
    with (
        patch(_PAGINATED, side_effect=err),
        pytest.raises(AuditUnavailable, match="could not read Dependabot alerts for o/r"),
    ):
        fetch_open_alerts("o/r")


def test_fetch_malformed_response_is_unavailable() -> None:
    with (
        patch(_PAGINATED, side_effect=ValueError("not a list")),
        pytest.raises(AuditUnavailable, match="unexpected response.*not a list"),
    ):
        fetch_open_alerts("o/r")


@pytest.mark.parametrize(
    "item",
    [
        "not-a-dict",
        {"html_url": "u"},
        {**_raw_alert(), "html_url": 7},
        {**_raw_alert(), "security_advisory": {"ghsa_id": "GHSA-1", "severity": ""}},
    ],
)
def test_fetch_malformed_alert_is_unavailable(item: object) -> None:
    with (
        patch(_PAGINATED, return_value=[item]),
        pytest.raises(AuditUnavailable, match="unexpected Dependabot alert payload for o/r"),
    ):
        fetch_open_alerts("o/r")


# --- classify_error ----------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (
            "HTTP 403: Dependabot alerts are DISABLED for this repository.",
            "Dependabot alerts are disabled for o/r",
        ),
        (
            "HTTP 403: Resource not accessible by integration",
            "token lacks the 'Dependabot alerts: Read' permission for o/r",
        ),
        (
            "gh: Resource not accessible by personal access token",
            "token lacks the 'Dependabot alerts: Read' permission for o/r",
        ),
        ("gh: Not Found (HTTP 404)", "repository o/r not found or not accessible"),
        (
            "gh: Server Error (HTTP 502)\nmore detail",
            "could not read Dependabot alerts for o/r: gh: Server Error (HTTP 502)",
        ),
        ("", "could not read Dependabot alerts for o/r: exit status 1"),
    ],
)
def test_classify_error(stderr: str, expected: str) -> None:
    assert classify_error("o/r", _api_error(stderr)) == expected


def test_classify_error_none_stderr() -> None:
    exc = subprocess.CalledProcessError(3, ("gh", "api"))
    assert classify_error("o/r", exc) == "could not read Dependabot alerts for o/r: exit status 3"


# --- render_report -----------------------------------------------------------


def test_severity_order() -> None:
    assert SEVERITY_ORDER == ("critical", "high", "medium", "low")


def test_render_clean() -> None:
    assert render_report("o/r", [], public=False) == "o/r: no open Dependabot alerts"
    assert render_report("o/r", [], public=True) == "o/r: no open Dependabot alerts"


def test_render_private_itemizes() -> None:
    low = _alert("low", "urllib3", number=1, ghsa_id="GHSA-low1-low1-low1")
    critical = _alert("critical", "requests", number=2, ghsa_id="GHSA-crit-crit-crit")
    out = render_report("o/r", [low, critical], public=False)
    lines = out.splitlines()
    assert "2 open Dependabot alerts: 1 critical, 0 high, 0 medium, 1 low" in lines[0]
    item_lines = [line for line in lines if "GHSA-" in line]
    assert len(item_lines) == 2
    assert "requests" in item_lines[0]
    assert "urllib3" in item_lines[1]
    for alert, line in zip((critical, low), item_lines, strict=True):
        assert alert.package in line
        assert alert.manifest_path in line
        assert alert.ghsa_id in line
        assert alert.html_url in line


def test_render_private_sorts_by_severity_then_package() -> None:
    alerts = [
        _alert("high", "zeta"),
        _alert("weird", "alpha"),
        _alert("high", "beta"),
        _alert("critical", "omega"),
    ]
    out = render_report("o/r", alerts, public=False)
    packages = [
        word
        for line in out.splitlines()
        if "GHSA-" in line
        for word in ("zeta", "alpha", "beta", "omega")
        if f" {word} " in line
    ]
    assert packages == ["omega", "beta", "zeta", "alpha"]


def test_render_counts_unknown_severity() -> None:
    out = render_report("o/r", [_alert("weird", "pkg"), _alert("low", "pkg2")], public=True)
    assert "2 open Dependabot alerts: 0 critical, 0 high, 0 medium, 1 low, 1 weird" in out


def test_render_single_alert_is_singular() -> None:
    out = render_report("o/r", [_alert("medium", "pkg")], public=True)
    assert "1 open Dependabot alert: 0 critical, 0 high, 1 medium, 0 low" in out


def test_audit_public_repo_counts_only() -> None:
    alerts = [
        _alert("low", "urllib3", number=1, ghsa_id="GHSA-low1-low1-low1"),
        _alert("critical", "requests", number=2, ghsa_id="GHSA-crit-crit-crit"),
    ]
    out = render_report("o/r", alerts, public=True)
    assert "2 open Dependabot alerts: 1 critical, 0 high, 0 medium, 1 low" in out
    assert "https://github.com/o/r/security/dependabot" in out
    assert "requests" not in out
    assert "urllib3" not in out
    assert "uv.lock" not in out
    assert "GHSA-" not in out
    assert "/security/dependabot/" not in out
