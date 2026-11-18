"""The TypeScript AUDIT gate's verdict on an ``npm audit --json`` report.

npm has no ignore list, so an advisory with no patched release would fail the
gate in every repo that has the package until upstream ships a fix
(vergil-project/vergil-tooling#4682). A repo acknowledges such an advisory in
:data:`EXCEPTIONS_FILE`, the npm counterpart of ``.trivyignore``: one reviewed,
time-boxed entry per advisory. The gate drops the findings a live entry covers
and fails on any other finding at :data:`GATING_SEVERITIES`.

The file is one entry per line; a line starting with ``#`` and a blank line
are ignored::

    <GHSA-ID> exp:<YYYY-MM-DD> <tracking issue> <reason>

The tracking issue is ``#<N>``, ``<owner>/<repo>#<N>`` or a GitHub issue URL,
and the reason is the rest of the line. An entry is live through its expiry
date (UTC) and may expire at most :data:`MAX_EXCEPTION_DAYS` days ahead. A
malformed entry, an expired one, a duplicate and one that expires too far
ahead each fail the gate by name; an entry no finding matches is reported, so
a stale one gets removed.

npm's report names each advisory by its GitHub advisory URL and carries no
CVE ID, so an entry names the advisory's GHSA ID. A CVE ID could never match
a finding and is rejected with that reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

# ExceptionFileError is re-exported: the audit gate and its callers import it
# from here.
from vergil_tooling.lib.exception_file import (
    ExceptionFileError,
    content_lines,
    is_tracking_ref,
    parse_expiry,
    window_problem,
)

if TYPE_CHECKING:
    import datetime

__all__ = [
    "EXCEPTIONS_FILE",
    "GATING_SEVERITIES",
    "MAX_EXCEPTION_DAYS",
    "Advisory",
    "AuditException",
    "ExceptionFileError",
    "Verdict",
    "evaluate",
    "parse_exceptions",
    "parse_report",
]

EXCEPTIONS_FILE = ".npm-audit-exceptions"

# The longest an exception may run, counted from the day the gate runs: a
# reviewed risk is reviewed again at least this often.
MAX_EXCEPTION_DAYS = 90

# The severities that fail the gate, as ``npm audit --audit-level=high`` does.
GATING_SEVERITIES = frozenset({"high", "critical"})

_SEVERITIES = frozenset({"info", "low", "moderate", "high", "critical"})

_GHSA_ID = re.compile(r"^GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}$")
_CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)
_ADVISORY_URL = re.compile(r"^https://github\.com/advisories/(GHSA(?:-[0-9a-z]{4}){3})$")


@dataclass(frozen=True)
class AuditException:
    """One entry of :data:`EXCEPTIONS_FILE`."""

    advisory_id: str
    expires: datetime.date
    tracking: str
    reason: str
    line: int


@dataclass(frozen=True)
class Advisory:
    """One advisory in the report, with every package it is reported against.

    ``advisory_id`` is the GHSA ID from the advisory's URL, or ``None`` when
    the URL is not a GitHub advisory, which no exception can then cover.
    """

    advisory_id: str | None
    severity: str
    title: str
    url: str
    packages: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """What the gate makes of a report under a set of exceptions."""

    applied: tuple[tuple[AuditException, Advisory], ...]
    unmatched: tuple[AuditException, ...]
    failing: tuple[Advisory, ...]


def parse_exceptions(text: str, *, today: datetime.date, source: str) -> list[AuditException]:
    """Parse the exceptions file, refusing every bad entry at once.

    Raises :class:`ExceptionFileError` naming each malformed, expired,
    duplicate or too-long entry by line, so one run shows all of them.
    """
    entries: list[AuditException] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for number, line in content_lines(text):
        entry = _parse_entry(line, number)
        if isinstance(entry, str):
            problems.append(f"line {number}: {entry}: {line!r}")
            continue
        if entry.advisory_id in seen:
            problems.append(
                f"line {number}: {entry.advisory_id} is already listed on line "
                f"{seen[entry.advisory_id]}"
            )
            continue
        seen[entry.advisory_id] = number
        window = window_problem(
            key=entry.advisory_id,
            expires=entry.expires,
            tracking=entry.tracking,
            today=today,
            max_days=MAX_EXCEPTION_DAYS,
        )
        if window is not None:
            problems.append(f"line {number}: {window}")
            continue
        entries.append(entry)
    if problems:
        raise ExceptionFileError(source, problems)
    return entries


def _parse_entry(line: str, number: int) -> AuditException | str:
    """One entry, or the reason the line is not one."""
    fields = line.split(maxsplit=3)
    if len(fields) < 4:
        return (
            "an entry is '<GHSA-ID> exp:<YYYY-MM-DD> <tracking issue> <reason>', "
            f"and this line has {len(fields)} of its 4 fields"
        )
    advisory_id, expiry, tracking, reason = fields
    if _CVE_ID.match(advisory_id):
        return (
            f"{advisory_id} is a CVE ID, and npm audit names advisories only by "
            "their GHSA ID: use the GHSA ID from the advisory's github.com/advisories page"
        )
    if not _GHSA_ID.match(advisory_id):
        return f"{advisory_id!r} is not a GHSA ID (GHSA-xxxx-xxxx-xxxx, lower case)"
    expires = parse_expiry(expiry)
    if isinstance(expires, str):
        return expires
    if not is_tracking_ref(tracking):
        return f"{tracking!r} is not a tracking issue (#N, owner/repo#N or a GitHub issue URL)"
    return AuditException(advisory_id, expires, tracking, reason.strip(), number)


def parse_report(report: object) -> list[Advisory]:
    """The advisories of an ``npm audit --json`` (report version 2) report.

    Each advisory appears once, with every package npm reports it against.
    Raises :class:`ValueError` for an npm error report or any shape the gate
    does not know, rather than reading it as clean.
    """
    if not isinstance(report, dict):
        msg = "the report is not a JSON object"
        raise ValueError(msg)
    if "error" in report:
        error = report["error"]
        summary = error.get("summary") if isinstance(error, dict) else error
        msg = f"npm audit reported an error: {summary}"
        raise ValueError(msg)
    if report.get("auditReportVersion") != 2:
        msg = (
            f"unsupported auditReportVersion {report.get('auditReportVersion')!r} "
            "(the gate reads version 2)"
        )
        raise ValueError(msg)
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        msg = "the report has no 'vulnerabilities' object"
        raise ValueError(msg)
    by_url: dict[str, Advisory] = {}
    for package, vulnerability in vulnerabilities.items():
        if not isinstance(vulnerability, dict) or not isinstance(vulnerability.get("via"), list):
            msg = f"the entry for {package!r} has no 'via' list"
            raise ValueError(msg)
        for via in vulnerability["via"]:
            if isinstance(via, str):
                # A package name: the advisory is reported under that package.
                continue
            advisory = _advisory(package, via)
            known = by_url.get(advisory.url)
            if known is not None:
                advisory = Advisory(
                    known.advisory_id,
                    known.severity,
                    known.title,
                    known.url,
                    tuple(sorted({*known.packages, *advisory.packages})),
                )
            by_url[advisory.url] = advisory
    return sorted(by_url.values(), key=lambda a: (a.advisory_id or "", a.url))


def _advisory(package: str, via: object) -> Advisory:
    if not isinstance(via, dict):
        msg = f"the entry for {package!r} has a 'via' item that is not an advisory"
        raise ValueError(msg)
    url, severity, title = via.get("url"), via.get("severity"), via.get("title")
    if not isinstance(url, str) or not url:
        msg = f"an advisory for {package!r} has no URL"
        raise ValueError(msg)
    if severity not in _SEVERITIES:
        msg = f"the advisory {url} has an unknown severity {severity!r}"
        raise ValueError(msg)
    match = _ADVISORY_URL.match(url)
    name = via.get("name")
    return Advisory(
        advisory_id=match.group(1) if match else None,
        severity=severity,
        title=title if isinstance(title, str) else "",
        url=url,
        packages=(name if isinstance(name, str) and name else package,),
    )


def evaluate(advisories: list[Advisory], exceptions: list[AuditException]) -> Verdict:
    """Apply the live exceptions to the advisories.

    An exception covers the advisory of its GHSA ID at any severity. What it
    does not cover fails the gate when its severity is in
    :data:`GATING_SEVERITIES`.
    """
    by_id = {entry.advisory_id: entry for entry in exceptions}
    applied: list[tuple[AuditException, Advisory]] = []
    failing: list[Advisory] = []
    matched: set[str] = set()
    for advisory in advisories:
        entry = by_id.get(advisory.advisory_id) if advisory.advisory_id else None
        if entry is not None:
            applied.append((entry, advisory))
            matched.add(entry.advisory_id)
        elif advisory.severity in GATING_SEVERITIES:
            failing.append(advisory)
    unmatched = tuple(entry for entry in exceptions if entry.advisory_id not in matched)
    return Verdict(tuple(applied), unmatched, tuple(failing))
