"""Tests for vergil_tooling.lib.npm_audit (issue #4682)."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from vergil_tooling.lib.npm_audit import (
    MAX_EXCEPTION_DAYS,
    Advisory,
    AuditException,
    ExceptionFileError,
    evaluate,
    parse_exceptions,
    parse_report,
)

TODAY = datetime.date(2026, 11, 18)
PICOMATCH = "GHSA-fc5q-39xc-vjpf"
PICOMATCH_URL = f"https://github.com/advisories/{PICOMATCH}"


def _via(name: str, url: str, severity: str, title: str = "a title") -> dict[str, Any]:
    return {
        "source": 1,
        "name": name,
        "dependency": name,
        "title": title,
        "url": url,
        "severity": severity,
        "range": "<=4.0.7",
    }


def _report(vulnerabilities: dict[str, Any]) -> dict[str, Any]:
    return {"auditReportVersion": 2, "vulnerabilities": vulnerabilities, "metadata": {}}


def _vuln(severity: str, via: list[Any]) -> dict[str, Any]:
    return {"severity": severity, "via": via, "fixAvailable": False}


def _entry(advisory_id: str = PICOMATCH, expires: str = "2026-12-31") -> AuditException:
    return AuditException(advisory_id, datetime.date.fromisoformat(expires), "#1", "no fix", line=1)


def _advisory(advisory_id: str | None, severity: str = "high") -> Advisory:
    url = f"https://github.com/advisories/{advisory_id}" if advisory_id else "https://x"
    return Advisory(advisory_id, severity, "t", url, ("pkg",))


# -- parse_exceptions -----------------------------------------------------------


def test_parses_entry_with_comments_and_blanks() -> None:
    text = (
        "# picomatch ReDoS, no patched release\n"
        "\n"
        f"{PICOMATCH} exp:2026-12-31 vergil-project/vergil-fixture-ts-lib#11 "
        "ReDoS in picomatch <=4.0.7; dev-only\n"
    )
    [entry] = parse_exceptions(text, today=TODAY, source=".npm-audit-exceptions")
    assert entry == AuditException(
        PICOMATCH,
        datetime.date(2026, 12, 31),
        "vergil-project/vergil-fixture-ts-lib#11",
        "ReDoS in picomatch <=4.0.7; dev-only",
        line=3,
    )


@pytest.mark.parametrize(
    "tracking",
    ["#12", "owner/repo#12", "https://github.com/owner/repo/issues/12"],
)
def test_accepts_tracking_forms(tracking: str) -> None:
    [entry] = parse_exceptions(
        f"{PICOMATCH} exp:2026-12-01 {tracking} reason", today=TODAY, source="f"
    )
    assert entry.tracking == tracking


def test_entry_live_on_its_expiry_day() -> None:
    [entry] = parse_exceptions(f"{PICOMATCH} exp:2026-11-18 #1 r", today=TODAY, source="f")
    assert entry.expires == TODAY


def test_expired_entry_fails_by_name() -> None:
    with pytest.raises(ExceptionFileError) as exc:
        parse_exceptions(f"{PICOMATCH} exp:2026-11-17 #7 r", today=TODAY, source="f")
    [problem] = exc.value.problems
    assert problem.startswith("line 1: the exception for GHSA-fc5q-39xc-vjpf expired on 2026-11-17")
    assert "tracked in #7" in problem


def test_entry_too_far_ahead_fails() -> None:
    latest = TODAY + datetime.timedelta(days=MAX_EXCEPTION_DAYS)
    parse_exceptions(f"{PICOMATCH} exp:{latest.isoformat()} #1 r", today=TODAY, source="f")
    beyond = latest + datetime.timedelta(days=1)
    with pytest.raises(ExceptionFileError) as exc:
        parse_exceptions(f"{PICOMATCH} exp:{beyond.isoformat()} #1 r", today=TODAY, source="f")
    assert f"more than {MAX_EXCEPTION_DAYS} days ahead" in exc.value.problems[0]


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        (f"{PICOMATCH} #1 no expiry at all", "is not an expiry"),
        (f"{PICOMATCH} exp:2026-12-31 #1", "has 3 of its 4 fields"),
        (f"{PICOMATCH} exp:2026-02-30 #1 r", "is not a real date"),
        (f"{PICOMATCH} exp:20261231 #1 r", "is not an expiry"),
        (f"{PICOMATCH} exp:31-12-2026 #1 r", "is not an expiry"),
        (f"{PICOMATCH} exp:2026-12-31 issue-1 r", "is not a tracking issue"),
        ("GHSA-FC5Q-39XC-VJPF exp:2026-12-31 #1 r", "is not a GHSA ID"),
        ("picomatch exp:2026-12-31 #1 r", "is not a GHSA ID"),
        ("CVE-2026-12345 exp:2026-12-31 #1 r", "is a CVE ID"),
    ],
)
def test_malformed_entry_fails_by_line(line: str, needle: str) -> None:
    with pytest.raises(ExceptionFileError) as exc:
        parse_exceptions(f"# header\n{line}\n", today=TODAY, source=".npm-audit-exceptions")
    [problem] = exc.value.problems
    assert problem.startswith("line 2: ")
    assert needle in problem
    assert repr(line) in problem
    assert str(exc.value).startswith(".npm-audit-exceptions: line 2: ")


def test_duplicate_entry_fails() -> None:
    text = f"{PICOMATCH} exp:2026-12-01 #1 r\n{PICOMATCH} exp:2026-12-02 #2 r\n"
    with pytest.raises(ExceptionFileError) as exc:
        parse_exceptions(text, today=TODAY, source="f")
    assert exc.value.problems == [f"line 2: {PICOMATCH} is already listed on line 1"]


def test_every_problem_is_reported_at_once() -> None:
    text = f"bad\n{PICOMATCH} exp:2026-01-01 #1 r\n"
    with pytest.raises(ExceptionFileError) as exc:
        parse_exceptions(text, today=TODAY, source="f")
    assert [p.split(":")[0] for p in exc.value.problems] == ["line 1", "line 2"]


def test_empty_file_has_no_entries() -> None:
    assert parse_exceptions("", today=TODAY, source="f") == []


# -- parse_report -----------------------------------------------------------------


def test_report_collects_each_advisory_once() -> None:
    report = _report(
        {
            "picomatch": _vuln("high", [_via("picomatch", PICOMATCH_URL, "high", "ReDoS")]),
            "tinyglobby": _vuln("high", ["picomatch"]),
            "other": _vuln(
                "moderate",
                [_via("other", "https://github.com/advisories/GHSA-2222-3333-4444", "moderate")],
            ),
        }
    )
    advisories = parse_report(report)
    assert advisories == [
        Advisory("GHSA-2222-3333-4444", "moderate", "a title", advisories[0].url, ("other",)),
        Advisory(PICOMATCH, "high", "ReDoS", PICOMATCH_URL, ("picomatch",)),
    ]


def test_report_merges_packages_of_one_advisory() -> None:
    report = _report(
        {
            "a": _vuln("high", [_via("a", PICOMATCH_URL, "high")]),
            "b": _vuln("high", [_via("b", PICOMATCH_URL, "high")]),
        }
    )
    [advisory] = parse_report(report)
    assert advisory.packages == ("a", "b")


def test_report_advisory_without_ghsa_url_has_no_id() -> None:
    report = _report({"a": _vuln("high", [_via("a", "https://npmjs.com/advisories/1", "high")])})
    [advisory] = parse_report(report)
    assert advisory.advisory_id is None


def test_clean_report_has_no_advisories() -> None:
    assert parse_report(_report({})) == []


@pytest.mark.parametrize(
    ("report", "needle"),
    [
        ([], "not a JSON object"),
        (
            {"error": {"code": "ENOLOCK", "summary": "no lockfile"}},
            "reported an error: no lockfile",
        ),
        ({"auditReportVersion": 1, "vulnerabilities": {}}, "unsupported auditReportVersion 1"),
        ({"auditReportVersion": 2}, "no 'vulnerabilities' object"),
        (_report({"a": {"severity": "high"}}), "has no 'via' list"),
        (_report({"a": _vuln("high", [3])}), "'via' item that is not an advisory"),
        (_report({"a": _vuln("high", [_via("a", "", "high")])}), "has no URL"),
        (_report({"a": _vuln("high", [_via("a", PICOMATCH_URL, "severe")])}), "unknown severity"),
    ],
)
def test_report_refuses_unknown_shapes(report: object, needle: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_report(report)
    assert needle in str(exc.value)


# -- evaluate ---------------------------------------------------------------------


def test_live_exception_covers_its_advisory() -> None:
    entry = _entry()
    advisory = _advisory(PICOMATCH)
    verdict = evaluate([advisory], [entry])
    assert verdict.applied == ((entry, advisory),)
    assert verdict.failing == ()
    assert verdict.unmatched == ()


def test_finding_with_no_exception_fails() -> None:
    advisory = _advisory("GHSA-2222-3333-4444", "critical")
    verdict = evaluate([advisory, _advisory(PICOMATCH)], [_entry()])
    assert verdict.failing == (advisory,)


def test_low_and_moderate_findings_do_not_fail() -> None:
    verdict = evaluate([_advisory("GHSA-2222-3333-4444", "moderate")], [])
    assert verdict.failing == ()


def test_advisory_with_no_id_cannot_be_covered() -> None:
    advisory = _advisory(None)
    assert evaluate([advisory], [_entry()]).failing == (advisory,)


def test_unmatched_exception_is_reported() -> None:
    entry = _entry("GHSA-2222-3333-4444")
    verdict = evaluate([], [entry])
    assert verdict.unmatched == (entry,)


def test_exception_matching_a_moderate_finding_is_applied() -> None:
    entry = _entry()
    advisory = _advisory(PICOMATCH, "moderate")
    verdict = evaluate([advisory], [entry])
    assert verdict.applied == ((entry, advisory),)
    assert verdict.unmatched == ()
