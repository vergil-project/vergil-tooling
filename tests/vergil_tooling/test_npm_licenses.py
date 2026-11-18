"""Tests for vergil_tooling.lib.npm_licenses (epic vergil-project/.github#1339)."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

import pytest

from vergil_tooling.lib.exception_file import ExceptionFileError
from vergil_tooling.lib.languages import _TYPESCRIPT_LICENSES_ALLOWLIST
from vergil_tooling.lib.npm_licenses import (
    EXCEPTIONS_FILE,
    MAX_EXCEPTION_DAYS,
    MISSING_TOKEN,
    And,
    LicenseException,
    LicenseExpressionError,
    LicenseTerm,
    LockedPackage,
    LockfileError,
    Or,
    allowed,
    canonical,
    judge,
    license_key,
    parse_exceptions,
    parse_expression,
    read_lockfile,
    report,
)

TODAY = datetime.date(2026, 11, 18)
ALLOWLIST = _TYPESCRIPT_LICENSES_ALLOWLIST.split(";")


def _ok(text: str, allowlist: list[str] | None = None) -> bool:
    return allowed(parse_expression(text), ALLOWLIST if allowlist is None else allowlist)


def _pkg(name: str, license_value: object = "MIT", version: str = "1.0.0") -> LockedPackage:
    return LockedPackage(name=name, version=version, license=license_value, dev=True)


def _exceptions(text: str) -> list[LicenseException]:
    return parse_exceptions(text, today=TODAY, source=EXCEPTIONS_FILE)


def _problems(text: str) -> list[str]:
    with pytest.raises(ExceptionFileError) as exc:
        _exceptions(text)
    return exc.value.problems


def _lockfile(tmp_path: Path, packages: dict[str, Any], version: int = 3) -> Path:
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps({"lockfileVersion": version, "packages": packages}))
    return path


# -- Constants ----------------------------------------------------------------


def test_constants() -> None:
    assert EXCEPTIONS_FILE == ".npm-license-exceptions"
    assert MAX_EXCEPTION_DAYS == 365
    assert MISSING_TOKEN == "-"


# -- Expressions ----------------------------------------------------------------


def test_or_any_branch() -> None:
    assert _ok("(MIT OR GPL-3.0-only)")
    assert _ok("GPL-3.0-only OR MIT")
    assert not _ok("GPL-3.0-only OR SSPL-1.0")


def test_and_every_branch() -> None:
    assert not _ok("(MIT AND GPL-3.0-only)")
    assert _ok("MIT AND ISC")


def test_and_binds_tighter_than_or() -> None:
    assert parse_expression("MIT OR ISC AND GPL-3.0-only") == Or(
        (LicenseTerm("MIT"), And((LicenseTerm("ISC"), LicenseTerm("GPL-3.0-only"))))
    )
    assert _ok("MIT OR ISC AND GPL-3.0-only")
    assert not _ok("(MIT OR ISC) AND GPL-3.0-only")


def test_with_needs_whole_term() -> None:
    expr = parse_expression("Apache-2.0 WITH LLVM-exception")
    assert expr == LicenseTerm("Apache-2.0", exception="LLVM-exception")
    # A bare ID never admits an exception clause the allowlist has not named.
    assert not allowed(expr, ALLOWLIST)
    assert allowed(expr, ["apache-2.0 WITH llvm-exception"])
    assert not allowed(expr, ["Apache-2.0 WITH Classpath-exception-2.0"])
    # The whole-term entry does not admit the bare ID either.
    assert not _ok("Apache-2.0", ["Apache-2.0 WITH LLVM-exception"])


def test_plus_needs_plus_entry() -> None:
    assert parse_expression("GPL-2.0+") == LicenseTerm("GPL-2.0", plus=True)
    assert not _ok("GPL-2.0+", ["GPL-2.0"])
    assert _ok("GPL-2.0+", ["GPL-2.0+"])
    assert not _ok("GPL-2.0", ["GPL-2.0+"])
    assert _ok("GPL-2.0+ WITH Classpath-exception-2.0", ["GPL-2.0+ WITH Classpath-exception-2.0"])


def test_id_case_insensitive() -> None:
    assert _ok("mit")
    assert _ok("APACHE-2.0 OR bsd-3-clause")


def test_lowercase_operator_unparseable() -> None:
    for text in (
        "mit or Apache-2.0",
        "MIT and ISC",
        "MIT Or ISC",
        "Apache-2.0 with LLVM-exception",
    ):
        with pytest.raises(LicenseExpressionError):
            parse_expression(text)


def test_see_license_in_unparseable() -> None:
    with pytest.raises(LicenseExpressionError, match="unexpected 'LICENSE'"):
        parse_expression("SEE LICENSE IN LICENSE")


def test_unknown_ids_disallowed() -> None:
    for text in ("UNLICENSED", "Custom", "LicenseRef-Proprietary", "DocumentRef-a:LicenseRef-b"):
        assert isinstance(parse_expression(text), LicenseTerm)
        assert not _ok(text)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "the expression is empty"),
        ("   ", "the expression is empty"),
        ("MIT AND", "the expression ends early"),
        ("(MIT OR ISC", "the expression ends early"),
        ("(MIT OR ISC ISC)", "a '(' is not closed"),
        ("MIT ISC", "unexpected 'ISC'"),
        ("MIT)", "unexpected ')'"),
        ("AND MIT", "'AND' is not a license ID"),
        ("MIT, ISC", "unexpected ','"),
        (", MIT", "',' is not a license ID"),
        ("MIT WITH", "the expression ends early"),
        ("MIT WITH OR", "'OR' is not a license exception ID"),
        ("MIT WITH (", "'(' is not a license exception ID"),
        ("MIT +", "unexpected '+'"),
        ("+", "'+' is not a license ID"),
        ("WITH+", "'WITH+' is not a license ID"),
    ],
)
def test_malformed_expressions(text: str, message: str) -> None:
    with pytest.raises(LicenseExpressionError, match=re.escape(message)):
        parse_expression(text)


def test_canonical_spelling_and_parentheses() -> None:
    assert canonical(parse_expression("(mit OR apache-2.0)"), ALLOWLIST) == "MIT OR Apache-2.0"
    assert canonical(parse_expression("((MIT))"), ALLOWLIST) == "MIT"
    # AND inside OR and OR inside AND are both parenthesized; same-operator
    # nesting is flattened.
    assert (
        canonical(parse_expression("mit   OR isc AND bsd-2-clause"), ALLOWLIST)
        == "MIT OR (ISC AND BSD-2-Clause)"
    )
    assert (
        canonical(parse_expression("(mit OR isc) AND 0bsd"), ALLOWLIST) == "(MIT OR ISC) AND 0BSD"
    )
    assert canonical(parse_expression("MIT OR (ISC OR 0BSD)"), ALLOWLIST) == "MIT OR ISC OR 0BSD"
    assert (
        canonical(parse_expression("(MIT AND ISC) AND (0BSD OR isc)"), ALLOWLIST)
        == "MIT AND ISC AND (0BSD OR ISC)"
    )
    # Unknown IDs keep their recorded spelling; exceptions take the allowlist's.
    assert canonical(parse_expression("custom+ OR gpl-2.0"), ALLOWLIST) == "custom+ OR gpl-2.0"
    assert (
        canonical(
            parse_expression("apache-2.0 WITH llvm-exception"), ["Apache-2.0 WITH LLVM-exception"]
        )
        == "Apache-2.0 WITH LLVM-exception"
    )


def test_allowlist_entries_are_single_licenses() -> None:
    for entry in ALLOWLIST:
        assert isinstance(parse_expression(entry), LicenseTerm), entry
    with pytest.raises(ValueError, match="'MIT OR ISC' is not a single license"):
        allowed(parse_expression("MIT"), ["MIT OR ISC"])


def test_standards_page_lists_the_allowlist() -> None:
    """The TypeScript standards page's License gate lists exactly the allowlist."""
    page = (
        Path(__file__).resolve().parents[2]
        / "docs/site/docs/standards/development/typescript/overview.md"
    ).read_text(encoding="utf-8")
    section = page.split("### License gate", 1)[1].split("\n#", 1)[0]
    listed = next(line for line in section.splitlines() if line.startswith("`0BSD`"))
    following = section.split(listed, 1)[1].split("\n\n", 1)[0]
    ids = re.findall(r"`([^`]+)`", listed + following)
    assert ids == ALLOWLIST


def test_validation_matrix_example_parses() -> None:
    """The example ``.npm-license-exceptions`` in the validation matrix is valid."""
    page = (
        Path(__file__).resolve().parents[2] / "docs/site/docs/guides/validation-matrix.md"
    ).read_text(encoding="utf-8")
    section = page.split("### TypeScript license exceptions", 1)[1]
    example = section.split("```text\n", 1)[1].split("```", 1)[0]
    entries = parse_exceptions(example, today=datetime.date(2026, 11, 18), source="example")
    assert [e.license for e in entries] == ["SEE LICENSE IN LICENSE", '{"type":"MIT"}', None]
    assert entries[1].license == license_key({"type": "MIT"})


def test_license_key_forms() -> None:
    assert license_key("MIT") == "MIT"
    assert license_key({"url": "u", "type": "MIT"}) == '{"type":"MIT","url":"u"}'
    assert license_key([{"type": "MIT"}]) == '[{"type":"MIT"}]'
    assert license_key(None) is None
    assert license_key("-") == "-"


# -- The lockfile ---------------------------------------------------------------


def test_read_lockfile_skips_root_and_links(tmp_path: Path) -> None:
    path = _lockfile(
        tmp_path,
        {
            "": {"name": "app", "version": "0.1.0", "license": "UNLICENSED"},
            "node_modules/a": {"version": "1.0.0", "license": "MIT"},
            "node_modules/linked": {"resolved": "packages/linked", "link": True},
        },
    )
    assert read_lockfile(path) == [LockedPackage("a", "1.0.0", "MIT", dev=False)]


def test_read_lockfile_name_from_key(tmp_path: Path) -> None:
    path = _lockfile(
        tmp_path,
        {
            "node_modules/@scope/a": {"version": "1.0.0", "license": "MIT"},
            "node_modules/a/node_modules/b": {"version": "2.0.0"},
            "node_modules/alias": {"name": "real-name", "version": "3.0.0", "license": "ISC"},
        },
    )
    assert [(p.name, p.version, p.license) for p in read_lockfile(path)] == [
        ("@scope/a", "1.0.0", "MIT"),
        ("b", "2.0.0", None),
        ("real-name", "3.0.0", "ISC"),
    ]


def test_dev_flag_from_dev_or_dev_optional(tmp_path: Path) -> None:
    path = _lockfile(
        tmp_path,
        {
            "node_modules/a": {"version": "1.0.0", "dev": True},
            "node_modules/b": {"version": "1.0.0", "devOptional": True},
            "node_modules/c": {"version": "1.0.0", "optional": True},
            "node_modules/d": {"version": "1.0.0", "dev": False},
        },
    )
    assert [(p.name, p.dev) for p in read_lockfile(path)] == [
        ("a", True),
        ("b", True),
        ("c", False),
        ("d", False),
    ]


def test_pre_v2_lockfile_fails(tmp_path: Path) -> None:
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps({"lockfileVersion": 1, "dependencies": {}}))
    with pytest.raises(LockfileError, match="lockfileVersion 1") as exc:
        read_lockfile(path)
    assert str(path) in str(exc.value)
    assert "npm install --package-lock-only" in str(exc.value)
    # A v2 version with no packages map, and a non-object file, are refused too.
    path.write_text(json.dumps({"lockfileVersion": 2}))
    with pytest.raises(LockfileError, match="not an npm v2 or v3 lockfile"):
        read_lockfile(path)
    path.write_text("[]")
    with pytest.raises(LockfileError, match="not an npm v2 or v3 lockfile"):
        read_lockfile(path)


def test_missing_and_unreadable_lockfile_fail(tmp_path: Path) -> None:
    missing = tmp_path / "package-lock.json"
    with pytest.raises(LockfileError, match="cannot read") as exc:
        read_lockfile(missing)
    assert "npm install --package-lock-only" in str(exc.value)
    missing.write_text("{not json")
    with pytest.raises(LockfileError, match="cannot read"):
        read_lockfile(missing)


def test_entry_without_version_fails(tmp_path: Path) -> None:
    path = _lockfile(
        tmp_path,
        {
            "node_modules/a": {"license": "MIT"},
            "node_modules/b": {"version": "", "license": "MIT"},
            "node_modules/c": "not an object",
            "node_modules/d": {"version": "1.0.0", "license": "MIT"},
        },
    )
    with pytest.raises(LockfileError) as exc:
        read_lockfile(path)
    message = str(exc.value)
    assert "the entry 'node_modules/a' has no version" in message
    assert "the entry 'node_modules/b' has no version" in message
    assert "the entry 'node_modules/c' is not an object" in message
    assert "npm install --package-lock-only" in message


# -- Exceptions -----------------------------------------------------------------


def test_parse_exceptions_forms() -> None:
    entries = _exceptions(
        "# a comment\n"
        "\n"
        'pkg-a "SEE LICENSE IN LICENSE" exp:2027-06-30 owner/repo#5 vendor license, reviewed\n'
        "pkg-b - exp:2027-01-01 #6 no license field upstream\n"
        'pkg-c "{\\"type\\":\\"MIT\\"}"\texp:2027-01-01\thttps://github.com/o/r/issues/7 legacy\n'
    )
    assert [(e.package, e.license, e.tracking, e.reason, e.line) for e in entries] == [
        ("pkg-a", "SEE LICENSE IN LICENSE", "owner/repo#5", "vendor license, reviewed", 3),
        ("pkg-b", None, "#6", "no license field upstream", 4),
        ("pkg-c", '{"type":"MIT"}', "https://github.com/o/r/issues/7", "legacy", 5),
    ]
    assert entries[0].expires == datetime.date(2027, 6, 30)
    assert entries[0].label == 'pkg-a "SEE LICENSE IN LICENSE"'
    assert entries[1].label == "pkg-b -"


def test_malformed_value_literal_fails() -> None:
    problems = _problems(
        "pkg SEE-LICENSE exp:2027-01-01 #1 r\n"
        'pkg "unterminated exp:2027-01-01 #1 r\n'
        'pkg "MIT"exp:2027-01-01 #1 r\n'
        "pkg -x exp:2027-01-01 #1 r\n"
    )
    assert len(problems) == 4
    for number, problem in enumerate(problems, start=1):
        assert problem.startswith(f"line {number}: the license value must be a JSON string literal")


def test_too_few_fields_fails() -> None:
    problems = _problems('pkg\npkg "MIT"\npkg - exp:2027-01-01 #1\n')
    assert problems[0].startswith(
        "line 1: an entry is '<package> <license value> exp:<YYYY-MM-DD> <tracking issue> "
        "<reason>', and this line has 1 of its 5 fields"
    )
    assert "this line has 2 of its 5 fields" in problems[1]
    assert "this line has 4 of its 5 fields" in problems[2]


def test_bad_expiry_and_tracking_fail() -> None:
    problems = _problems(
        'pkg "MIT" 2027-01-01 #1 r\n'
        'pkg "MIT" exp:2027-02-30 #1 r\n'
        'pkg "MIT" exp:2027-01-01 issue-1 r\n'
    )
    assert problems == [
        "line 1: '2027-01-01' is not an expiry (exp:YYYY-MM-DD): 'pkg \"MIT\" 2027-01-01 #1 r'",
        "line 2: 'exp:2027-02-30' is not a real date: 'pkg \"MIT\" exp:2027-02-30 #1 r'",
        "line 3: 'issue-1' is not a tracking issue (#N, owner/repo#N or a GitHub issue URL): "
        "'pkg \"MIT\" exp:2027-01-01 issue-1 r'",
    ]


def test_expired_entry_fails() -> None:
    (problem,) = _problems('pkg "MIT" exp:2026-11-17 #1 r\n')
    assert problem.startswith(
        'line 1: the exception for pkg "MIT" expired on 2026-11-17 (tracked in #1)'
    )
    # Live through its expiry date.
    assert _exceptions('pkg "MIT" exp:2026-11-18 #1 r\n')


def test_exception_window_365() -> None:
    latest = TODAY + datetime.timedelta(days=365)
    assert _exceptions(f'pkg "MIT" exp:{latest.isoformat()} #1 r\n')
    (problem,) = _problems(
        f'pkg "MIT" exp:{(latest + datetime.timedelta(days=1)).isoformat()} #1 r\n'
    )
    assert f"more than 365 days ahead (latest allowed: {latest.isoformat()})" in problem


def test_duplicate_key_fails() -> None:
    (problem,) = _problems('pkg "MIT" exp:2027-01-01 #1 r\npkg "MIT" exp:2027-02-01 #2 r\n')
    assert problem == 'line 2: pkg "MIT" is already listed on line 1'
    # One entry per excused value is allowed.
    assert len(_exceptions('pkg "MIT" exp:2027-01-01 #1 r\npkg "ISC" exp:2027-01-01 #1 r\n')) == 2


def test_every_bad_line_reported_once() -> None:
    with pytest.raises(ExceptionFileError) as exc:
        _exceptions(
            "pkg-a MIT exp:2027-01-01 #1 r\n"
            'pkg-b "MIT" exp:2026-01-01 #1 r\n'
            'pkg-c "MIT" exp:2027-01-01 nope r\n'
            'pkg-d "MIT" exp:2027-01-01 #1 fine\n'
        )
    problems = exc.value.problems
    assert [p.split(":", 1)[0] for p in problems] == ["line 1", "line 2", "line 3"]
    assert str(exc.value).startswith(f"{EXCEPTIONS_FILE}: line 1:")


def test_dash_string_not_missing() -> None:
    entries = _exceptions('pkg - exp:2027-01-01 #1 r\npkg "-" exp:2027-01-01 #1 r\n')
    assert [e.license for e in entries] == [None, "-"]
    missing_entry, dash_entry = entries
    dash_pkg, missing_pkg = _pkg("pkg", "-"), _pkg("pkg", None)
    verdict = judge([dash_pkg], allowlist=ALLOWLIST, exceptions=[missing_entry])
    assert [r.verdict for r in verdict.rows] == ["failing"]
    verdict = judge([missing_pkg], allowlist=ALLOWLIST, exceptions=[dash_entry])
    assert [r.verdict for r in verdict.rows] == ["failing"]
    verdict = judge([dash_pkg, missing_pkg], allowlist=ALLOWLIST, exceptions=entries)
    assert [r.verdict for r in verdict.rows] == ["excepted", "excepted"]


# -- The verdict ----------------------------------------------------------------


def test_exception_covers_only_its_value() -> None:
    entries = _exceptions('pkg "SEE LICENSE IN LICENSE" exp:2027-01-01 #1 vendor\n')
    verdict = judge(
        [_pkg("pkg", "SEE LICENSE IN LICENSE", "1.0.0"), _pkg("pkg", "GPL-3.0-only", "2.0.0")],
        allowlist=ALLOWLIST,
        exceptions=entries,
    )
    assert [(r.package.version, r.verdict) for r in verdict.rows] == [
        ("1.0.0", "excepted"),
        ("2.0.0", "failing"),
    ]
    assert [row.package.version for row in verdict.failing] == ["2.0.0"]
    assert verdict.unmatched == ()


def test_two_versions_two_verdicts() -> None:
    # One package locked at two versions with different licenses: each is
    # judged on its own, and an exception for one value does not cover the other.
    packages = [_pkg("dual", "GPL-3.0-only", "2.0.0"), _pkg("dual", "MIT", "1.0.0")]
    verdict = judge(packages, allowlist=ALLOWLIST, exceptions=[])
    assert [(r.package.version, r.verdict) for r in verdict.rows] == [
        ("1.0.0", "allowed"),
        ("2.0.0", "failing"),
    ]
    entries = _exceptions('dual "GPL-3.0-only" exp:2027-01-01 #1 r\n')
    verdict = judge(packages, allowlist=ALLOWLIST, exceptions=entries)
    assert [(r.package.version, r.verdict) for r in verdict.rows] == [
        ("1.0.0", "allowed"),
        ("2.0.0", "excepted"),
    ]
    entries = _exceptions('dual "MIT" exp:2027-01-01 #1 r\n')
    verdict = judge(packages, allowlist=ALLOWLIST, exceptions=entries)
    assert [r.package.version for r in verdict.failing] == ["2.0.0"]


def test_missing_license_needs_dash_entry() -> None:
    package = _pkg("bare", None)
    verdict = judge([package], allowlist=ALLOWLIST, exceptions=[])
    assert verdict.failing[0].reason == "it records no license"
    entries = _exceptions("bare - exp:2027-01-01 #1 r\n")
    verdict = judge([package], allowlist=ALLOWLIST, exceptions=entries)
    assert verdict.rows[0].verdict == "excepted"
    assert verdict.failing == ()


def test_failing_reasons() -> None:
    verdict = judge(
        [
            _pkg("a", "MIT AND GPL-3.0-only"),
            _pkg("b", "SEE LICENSE IN LICENSE"),
            _pkg("c", {"type": "MIT"}),
            _pkg("d", "UNLICENSED"),
        ],
        allowlist=ALLOWLIST,
        exceptions=[],
    )
    assert [r.reason for r in verdict.failing] == [
        "its license is not allowed (GPL-3.0-only not on the allowlist)",
        "its license is not an SPDX expression (unexpected 'LICENSE')",
        "its license is not an SPDX expression (a legacy object or array)",
        "its license is not allowed (UNLICENSED not on the allowlist)",
    ]


def test_unmatched_entry_warns() -> None:
    entries = _exceptions('ghost "GPL-3.0-only" exp:2027-01-01 #1 r\n')
    verdict = judge([_pkg("a")], allowlist=ALLOWLIST, exceptions=entries)
    assert verdict.failing == ()
    assert [(e.package, why) for e, why in verdict.unmatched] == [
        ("ghost", "matches no package in the lockfile")
    ]


def test_entry_for_passing_package_warns() -> None:
    entries = _exceptions('a "MIT" exp:2027-01-01 #1 r\n')
    verdict = judge([_pkg("a")], allowlist=ALLOWLIST, exceptions=entries)
    assert verdict.rows[0].verdict == "allowed"
    assert verdict.applied == ()
    assert [(e.package, why) for e, why in verdict.unmatched] == [
        ("a", "matches only packages that pass without it")
    ]


def test_report_raw_and_canonical() -> None:
    entries = _exceptions('vendor "SEE LICENSE IN LICENSE" exp:2027-01-01 owner/repo#9 r\n')
    verdict = judge(
        [
            LockedPackage("dual", "1.0.0", "(mit OR Apache-2.0)", dev=False),
            LockedPackage("vendor", "2.0.0", "SEE LICENSE IN LICENSE", dev=True),
            LockedPackage("legacy", "3.0.0", {"type": "MIT", "url": "u"}, dev=True),
            LockedPackage("bare", "4.0.0", None, dev=False),
        ],
        allowlist=ALLOWLIST,
        exceptions=entries,
    )
    assert report(verdict) == [
        {
            "name": "bare",
            "version": "4.0.0",
            "license": None,
            "license_canonical": None,
            "dev": False,
            "verdict": "failing",
        },
        {
            "name": "dual",
            "version": "1.0.0",
            "license": "(mit OR Apache-2.0)",
            "license_canonical": "MIT OR Apache-2.0",
            "dev": False,
            "verdict": "allowed",
        },
        {
            "name": "legacy",
            "version": "3.0.0",
            "license": '{"type":"MIT","url":"u"}',
            "license_canonical": None,
            "dev": True,
            "verdict": "failing",
        },
        {
            "name": "vendor",
            "version": "2.0.0",
            "license": "SEE LICENSE IN LICENSE",
            "license_canonical": None,
            "dev": True,
            "verdict": "excepted",
            "exception": {"tracking": "owner/repo#9", "expires": "2027-01-01"},
        },
    ]
