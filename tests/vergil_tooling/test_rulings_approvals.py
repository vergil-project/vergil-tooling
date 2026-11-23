"""Tests for the protected-change and exemption requests (epic .github#1384, Task 6a)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.protected_paths import ProtectedChange
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.approvals import (
    DECISION_LINES,
    REPLY_INSTRUCTION,
    parse_decision,
    render_exemption_request,
    render_protected_change_request,
)
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.marker import Marker, parse_marker
from vergil_tooling.lib.rulings.plain_text import check_plain_text

AGENT = "h-vergil-user"
BLOB = "a" * 40
BLOB256 = "b" * 64
FINGERPRINT = "c" * 64
URL = "https://github.com/o/r/compare/1111...2222"


def _marker(body: str) -> Marker | None:
    return parse_marker(IssueComment(1, AGENT, body, False), AGENT)


# --- the protected-change request ------------------------------------------------


def test_protected_change_request_lists_each_path_and_blob() -> None:
    changes = [
        ProtectedChange(".github/workflows/ci.yml", BLOB),
        ProtectedChange("docs/a file.toml", BLOB256),
        ProtectedChange("vergil.toml", "deleted"),
    ]
    body = render_protected_change_request(changes, URL)
    lines = body.split("\n")
    assert lines[0].startswith("Protected-change approval requested.")
    assert lines[1:-3] == [
        f"Protected: {BLOB} .github/workflows/ci.yml",
        f"Protected: {BLOB256} docs/a file.toml",
        "Protected: deleted vergil.toml",
        f"Diff: {URL}",
    ]
    assert lines[-3:] == [
        REPLY_INSTRUCTION,
        "",
        "<!-- vrg-ruling kind=request type=protected-change -->",
    ]
    assert _marker(body) == Marker("request", None, None, "protected-change")
    check_plain_text(body.rpartition("\n")[0], what="the body")


@pytest.mark.parametrize(
    ("changes", "url", "message"),
    [
        ([], URL, "nothing to approve"),
        ([ProtectedChange("a", BLOB), ProtectedChange("a", BLOB)], URL, "listed twice"),
        ([ProtectedChange("a", "A" * 40)], URL, "neither a git object ID"),
        ([ProtectedChange("a", "abc")], URL, "neither a git object ID"),
        ([ProtectedChange("a\nProtected: x y", BLOB)], URL, "one line"),
        ([ProtectedChange("café", BLOB)], URL, "not plain text"),
        ([ProtectedChange(" a", BLOB)], URL, "whitespace"),
        ([ProtectedChange("", BLOB)], URL, "empty"),
        ([ProtectedChange("a", BLOB)], "http://example.com/x", "https://"),
        ([ProtectedChange("a", BLOB)], "", "empty"),
    ],
)
def test_protected_change_request_refuses_bad_input(
    changes: list[ProtectedChange], url: str, message: str
) -> None:
    with pytest.raises(RulingError, match=message):
        render_protected_change_request(changes, url)


# --- the exemption request -------------------------------------------------------


def test_exemption_request_records_reason_and_fingerprint() -> None:
    body = render_exemption_request("a one-line typo fix", FINGERPRINT)
    lines = body.split("\n")
    assert lines[0].startswith("Merge-review exemption requested.")
    assert lines[1:3] == ["Reason: a one-line typo fix", f"Fingerprint: {FINGERPRINT}"]
    assert lines[3:] == [
        REPLY_INSTRUCTION,
        "",
        "<!-- vrg-ruling kind=request type=merge-review-exemption -->",
    ]
    assert _marker(body) == Marker("request", None, None, "merge-review-exemption")


@pytest.mark.parametrize(
    ("reason", "fingerprint", "message"),
    [
        ("", FINGERPRINT, "empty"),
        ("two\nlines", FINGERPRINT, "one line"),
        ("tab\there", FINGERPRINT, "not plain text"),
        ("ok", "C" * 64, "SHA-256"),
        ("ok", "c" * 63, "SHA-256"),
        ("ok", "", "SHA-256"),
    ],
)
def test_exemption_request_refuses_bad_input(reason: str, fingerprint: str, message: str) -> None:
    with pytest.raises(RulingError, match=message):
        render_exemption_request(reason, fingerprint)


# --- the decision line -----------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "decision"),
    [
        ("Decision: approve", "approve"),
        ("Decision: refuse", "refuse"),
        ("Decision: approve\n", "approve"),
        ("Looked at the workflow diff.\nDecision: approve\nThanks.", "approve"),
        ("I decided: approve\nDecision: refuse", "refuse"),
    ],
)
def test_parse_decision_accepts_only_one_exact_line(body: str, decision: str) -> None:
    assert parse_decision(body) == decision


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "no decision line"),
        ("approve", "no decision line"),
        ("Looks good to me", "no decision line"),
        ("Decision: Approve", "not exactly"),
        ("decision: approve", "not exactly"),
        ("DECISION: refuse", "not exactly"),
        ("Decision: approved", "not exactly"),
        ("Decision:approve", "not exactly"),
        ("Decision:  approve", "not exactly"),
        ("Decision: approve ", "not exactly"),
        (" Decision: approve", "not exactly"),
        ("Decision: yes", "not exactly"),
        ("Decision: approve\nDecision: approve", "2 decision lines"),
        ("Decision: approve\nDecision: refuse", "2 decision lines"),
        ("Decision: approve\n  decision: maybe", "2 decision lines"),
    ],
)
def test_parse_decision_refuses_anything_else(body: str, message: str) -> None:
    with pytest.raises(RulingError, match=message):
        parse_decision(body)


def test_decision_lines_are_the_two_words() -> None:
    assert DECISION_LINES == {"Decision: approve": "approve", "Decision: refuse": "refuse"}
