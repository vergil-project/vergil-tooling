"""Tests for vergil_tooling.lib.exception_file (issue #4721)."""

from __future__ import annotations

import datetime

import pytest

from vergil_tooling.lib import npm_audit
from vergil_tooling.lib.exception_file import (
    ExceptionFileError,
    content_lines,
    is_tracking_ref,
    parse_expiry,
    window_problem,
)

TODAY = datetime.date(2026, 11, 18)


def test_parse_expiry_valid_malformed_and_impossible() -> None:
    assert parse_expiry("exp:2026-12-01") == datetime.date(2026, 12, 1)
    assert parse_expiry("exp:20261231") == "'exp:20261231' is not an expiry (exp:YYYY-MM-DD)"
    assert parse_expiry("2026-12-31") == "'2026-12-31' is not an expiry (exp:YYYY-MM-DD)"
    assert parse_expiry("exp:2026-02-30") == "'exp:2026-02-30' is not a real date"


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("#12", True),
        ("vergil-project/vergil-tooling#4721", True),
        ("https://github.com/vergil-project/vergil-tooling/issues/4721", True),
        ("4721", False),
        ("#", False),
        ("owner#12", False),
        ("https://github.com/vergil-project/vergil-tooling/pull/4721", False),
        ("http://github.com/vergil-project/vergil-tooling/issues/4721", False),
        ("#12x", False),
    ],
)
def test_tracking_ref_forms(token: str, expected: bool) -> None:
    assert is_tracking_ref(token) is expected


def test_window_expired_message() -> None:
    problem = window_problem(
        key="GHSA-fc5q-39xc-vjpf",
        expires=datetime.date(2026, 11, 17),
        tracking="#9",
        today=TODAY,
        max_days=90,
    )
    assert problem == (
        "the exception for GHSA-fc5q-39xc-vjpf expired on 2026-11-17 (tracked in #9): "
        "fix the dependency, or review the risk again and set a new expiry"
    )


def test_window_too_far_message() -> None:
    problem = window_problem(
        key="left-pad",
        expires=datetime.date(2027, 11, 19),
        tracking="#9",
        today=TODAY,
        max_days=365,
    )
    assert problem == (
        "the exception for left-pad expires on 2027-11-19, more than 365 days ahead "
        "(latest allowed: 2027-11-18)"
    )


def test_window_bounds_are_live() -> None:
    for expires in (TODAY, TODAY + datetime.timedelta(days=90)):
        assert (
            window_problem(key="k", expires=expires, tracking="#1", today=TODAY, max_days=90)
            is None
        )


def test_content_lines_skips_comments_and_blanks() -> None:
    text = "# a comment\n\n  first entry  \n   \n\t# indented comment\nsecond\n"
    assert list(content_lines(text)) == [(3, "first entry"), (6, "second")]


def test_exception_file_error_names_source_and_problems() -> None:
    error = ExceptionFileError("f", ["line 1: a", "line 2: b"])
    assert error.problems == ["line 1: a", "line 2: b"]
    assert str(error) == "f: line 1: a; line 2: b"
    assert isinstance(error, ValueError)


def test_npm_audit_re_exports_the_error() -> None:
    assert npm_audit.ExceptionFileError is ExceptionFileError
