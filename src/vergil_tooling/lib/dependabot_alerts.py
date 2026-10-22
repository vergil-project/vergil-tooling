"""Read and report a repository's open Dependabot alerts.

The library half of ``vrg-dependabot-audit`` (epic vergil-project/.github#420):
the fleet runs Dependabot report-only, with alerts on and PRs off, and the
nightly ops job fails on **any** open alert. There is no severity threshold and
no in-repo suppression: only ``state=open`` alerts are read, so dismissing an
alert on GitHub is the sole escape hatch.

"Could not check" is never a pass. Every way the alert list can fail to load
(alerts disabled, missing permission, unknown repo, malformed payload) becomes
:class:`AuditUnavailable`, whose message is the human diagnostic, so the CLI
can exit 2 instead of a false 0 or 1.

Public repositories get a counts-only report plus a link to the Security tab:
package names, manifest paths and GHSA IDs never reach a world-readable log.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass

from vergil_tooling.lib import github

SEVERITY_ORDER = ("critical", "high", "medium", "low")


@dataclass(frozen=True)
class Alert:
    """One open Dependabot alert, reduced to the fields the report prints."""

    severity: str
    ecosystem: str
    package: str
    manifest_path: str
    ghsa_id: str
    html_url: str


class AuditUnavailable(Exception):  # noqa: N818 - the name is the plan's interface
    """The alerts could not be read; the message is the human diagnostic."""


def classify_error(repo: str, exc: subprocess.CalledProcessError) -> str:
    """Turn a failed alerts read into an actionable one-line diagnostic."""
    stderr = exc.stderr or ""
    if "disabled" in stderr.lower():
        return f"Dependabot alerts are disabled for {repo}"
    if "403" in stderr or "Resource not accessible" in stderr:
        return f"token lacks the 'Dependabot alerts: Read' permission for {repo}"
    if "404" in stderr:
        return f"repository {repo} not found or not accessible"
    lines = stderr.strip().splitlines()
    first = lines[0] if lines else f"exit status {exc.returncode}"
    return f"could not read Dependabot alerts for {repo}: {first}"


def _field(item: object, *path: str) -> str:
    """The non-empty string at *path* inside *item*, else :class:`KeyError`."""
    node = item
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(".".join(path))
        node = node[key]
    if not isinstance(node, str) or not node:
        raise KeyError(".".join(path))
    return node


def _to_alert(item: object) -> Alert:
    return Alert(
        severity=_field(item, "security_advisory", "severity"),
        ecosystem=_field(item, "dependency", "package", "ecosystem"),
        package=_field(item, "dependency", "package", "name"),
        manifest_path=_field(item, "dependency", "manifest_path"),
        ghsa_id=_field(item, "security_advisory", "ghsa_id"),
        html_url=_field(item, "html_url"),
    )


def fetch_open_alerts(repo: str) -> list[Alert]:
    """Every open Dependabot alert on *repo*, across all pages.

    Raises :class:`AuditUnavailable` when the list cannot be read or an alert
    is missing a field the report needs.
    """
    try:
        items = github.read_json_paginated(f"repos/{repo}/dependabot/alerts?state=open")
    except subprocess.CalledProcessError as exc:
        # GitHubAPIError and MissingGitHubTokenError are CalledProcessError
        # subclasses, as is the bare error gh raises with no output.
        raise AuditUnavailable(classify_error(repo, exc)) from exc
    except ValueError as exc:
        msg = f"unexpected response reading Dependabot alerts for {repo}: {exc}"
        raise AuditUnavailable(msg) from exc
    try:
        return [_to_alert(item) for item in items]
    except KeyError as exc:
        msg = f"unexpected Dependabot alert payload for {repo}: missing or empty {exc}"
        raise AuditUnavailable(msg) from exc


def _severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)


def _totals_line(repo: str, alerts: list[Alert]) -> str:
    counts = Counter(alert.severity for alert in alerts)
    parts = [f"{counts[sev]} {sev}" for sev in SEVERITY_ORDER]
    parts.extend(f"{counts[sev]} {sev}" for sev in sorted(set(counts) - set(SEVERITY_ORDER)))
    noun = "alert" if len(alerts) == 1 else "alerts"
    return f"{repo}: {len(alerts)} open Dependabot {noun}: {', '.join(parts)}"


def render_report(repo: str, alerts: list[Alert], *, public: bool) -> str:
    """The audit report: totals always, itemized lines only when not *public*."""
    if not alerts:
        return f"{repo}: no open Dependabot alerts"
    lines = [_totals_line(repo, alerts)]
    if not public:
        ordered = sorted(alerts, key=lambda a: (_severity_rank(a.severity), a.package))
        lines.extend(
            f"  {a.severity} {a.ecosystem} {a.package} {a.manifest_path} {a.ghsa_id} {a.html_url}"
            for a in ordered
        )
    lines.append(f"Review or dismiss at https://github.com/{repo}/security/dependabot")
    return "\n".join(lines)
