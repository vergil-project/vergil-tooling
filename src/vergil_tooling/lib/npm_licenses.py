"""The TypeScript license gate's verdict on a repo's ``package-lock.json``.

The gate reads every package the committed lockfile locks, dev dependencies
included, and judges the ``license`` field npm records for each against the
TypeScript allowlist (``_TYPESCRIPT_LICENSES_ALLOWLIST`` in
:mod:`vergil_tooling.lib.languages`). The field is read as an SPDX license
expression (SPDX 2.3, Annex D): license IDs, an optional ``+``, ``WITH``
exceptions, ``AND``, ``OR`` and parentheses, ``AND`` binding tighter than
``OR``. IDs match the allowlist without regard to case; the operators are
upper case only. The parse checks syntax only, so a well-formed ID the
allowlist does not name (``UNLICENSED``) parses and is disallowed.

A package whose license is disallowed, unparseable or missing fails unless a
live entry of :data:`EXCEPTIONS_FILE` covers it. The file is one entry per
line; a line starting with ``#`` and a blank line are ignored::

    <package> <license value> exp:<YYYY-MM-DD> <tracking issue> <reason>

The license value is the lockfile's ``license`` as a JSON string literal
(``"SEE LICENSE IN LICENSE"``), the compact sorted-key JSON of a non-string
value as a JSON string literal, or the bare :data:`MISSING_TOKEN` for a
package that records no license. An entry covers a package only when the
name matches and the recorded value equals the entry's value exactly, so a
version whose license changed is not covered. The expiry, tracking issue and
the one-run report of every bad line follow
:mod:`vergil_tooling.lib.exception_file`, with a window of at most
:data:`MAX_EXCEPTION_DAYS` days.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib.exception_file import (
    ExceptionFileError,
    content_lines,
    is_tracking_ref,
    parse_expiry,
    window_problem,
)

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterable
    from pathlib import Path

__all__ = [
    "EXCEPTIONS_FILE",
    "MAX_EXCEPTION_DAYS",
    "MISSING_TOKEN",
    "And",
    "Expr",
    "ExceptionFileError",
    "LicenseException",
    "LicenseExpressionError",
    "LicenseRow",
    "LicenseTerm",
    "LicenseVerdict",
    "LockedPackage",
    "LockfileError",
    "Or",
    "allowed",
    "canonical",
    "judge",
    "license_key",
    "parse_exceptions",
    "parse_expression",
    "read_lockfile",
    "report",
]

EXCEPTIONS_FILE = ".npm-license-exceptions"

# The longest a license exception may run, counted from the day the gate runs.
# A license quirk (a non-SPDX string on an otherwise allowed package) is
# usually permanent, so a yearly review is enough; the audit's advisories keep
# their 90 days.
MAX_EXCEPTION_DAYS = 365

# The bare token an exception line uses for a package that records no license.
# Every recorded value is written quoted, so it never collides with this.
MISSING_TOKEN = "-"  # noqa: S105 - a file-format marker, not a secret

_FIX_LOCKFILE = "regenerate it with a current npm: npm install --package-lock-only"


# -- License expressions ------------------------------------------------------


class LicenseExpressionError(ValueError):
    """The text is not an SPDX license expression."""


@dataclass(frozen=True)
class LicenseTerm:
    """One license: an ID, with ``+`` and a ``WITH`` exception when given."""

    license_id: str
    plus: bool = False
    exception: str | None = None


@dataclass(frozen=True)
class And:
    """Every operand must be allowed."""

    operands: tuple[Expr, ...]


@dataclass(frozen=True)
class Or:
    """Any operand may be allowed."""

    operands: tuple[Expr, ...]


Expr = LicenseTerm | And | Or

# An idstring is letters, digits, ``-`` and ``.``; a ``DocumentRef-…:`` prefix
# joins two with a colon. ``+`` follows an ID with no space.
_TOKEN = re.compile(r"\(|\)|[A-Za-z0-9.\-]+(?::[A-Za-z0-9.\-]+)?\+?|\S")
_OPERATORS = frozenset({"AND", "OR", "WITH"})
_ID = re.compile(r"^[A-Za-z0-9.\-]+(?::[A-Za-z0-9.\-]+)?$")


def parse_expression(text: str) -> Expr:
    """Parse an SPDX license expression, raising :class:`LicenseExpressionError`."""
    tokens = _TOKEN.findall(text)
    if not tokens:
        msg = "the expression is empty"
        raise LicenseExpressionError(msg)
    parser = _Parser(tokens)
    expr = parser.or_expr()
    if parser.pos != len(tokens):
        msg = f"unexpected {tokens[parser.pos]!r}"
        raise LicenseExpressionError(msg)
    return expr


class _Parser:
    """A recursive-descent parser over the expression's tokens."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            msg = "the expression ends early"
            raise LicenseExpressionError(msg)
        self.pos += 1
        return token

    def or_expr(self) -> Expr:
        operands = [self.and_expr()]
        while self._peek() == "OR":
            self.pos += 1
            operands.append(self.and_expr())
        return operands[0] if len(operands) == 1 else Or(tuple(operands))

    def and_expr(self) -> Expr:
        operands = [self.primary()]
        while self._peek() == "AND":
            self.pos += 1
            operands.append(self.primary())
        return operands[0] if len(operands) == 1 else And(tuple(operands))

    def primary(self) -> Expr:
        word = self._take()
        if word == "(":
            expr = self.or_expr()
            if self._take() != ")":
                msg = "a '(' is not closed"
                raise LicenseExpressionError(msg)
            return expr
        term = _term(word)
        if self._peek() == "WITH":
            self.pos += 1
            exception = self._take()
            if exception in _OPERATORS or not _ID.match(exception):
                msg = f"{exception!r} is not a license exception ID"
                raise LicenseExpressionError(msg)
            term = LicenseTerm(term.license_id, term.plus, exception)
        return term


def _term(token: str) -> LicenseTerm:
    plus = token.endswith("+")
    license_id = token[:-1] if plus else token
    if license_id in _OPERATORS or not _ID.match(license_id):
        msg = f"{token!r} is not a license ID"
        raise LicenseExpressionError(msg)
    return LicenseTerm(license_id, plus)


def _term_key(term: LicenseTerm) -> str:
    """The case-folded whole term, as an allowlist entry is compared."""
    key = term.license_id.casefold() + ("+" if term.plus else "")
    if term.exception is not None:
        key += " with " + term.exception.casefold()
    return key


@dataclass(frozen=True)
class _Allowlist:
    """The allowlist's terms as compared (``keys``) and as spelled (``spelling``)."""

    keys: frozenset[str]
    spelling: dict[str, str]

    @classmethod
    def of(cls, allowlist: Iterable[str]) -> _Allowlist:
        keys: set[str] = set()
        spelling: dict[str, str] = {}
        for entry in allowlist:
            term = parse_expression(entry)
            if not isinstance(term, LicenseTerm):
                msg = f"the allowlist entry {entry!r} is not a single license"
                raise ValueError(msg)
            keys.add(_term_key(term))
            spelling.setdefault(term.license_id.casefold(), term.license_id)
            if term.exception is not None:
                spelling.setdefault(term.exception.casefold(), term.exception)
        return cls(frozenset(keys), spelling)


def allowed(expr: Expr, allowlist: Iterable[str]) -> bool:
    """Whether the expression is allowed by the allowlist.

    ``OR`` needs any operand allowed and ``AND`` every one. A term is allowed
    only when the whole term matches an entry: a ``WITH`` term needs an entry
    naming the same exception, and an ID with ``+`` an entry spelled with
    ``+``. IDs compare without regard to case.
    """
    return _allowed(expr, _Allowlist.of(allowlist).keys)


def _allowed(expr: Expr, keys: frozenset[str]) -> bool:
    if isinstance(expr, LicenseTerm):
        return _term_key(expr) in keys
    results = (_allowed(operand, keys) for operand in expr.operands)
    return any(results) if isinstance(expr, Or) else all(results)


def _disallowed_terms(expr: Expr, keys: frozenset[str]) -> list[LicenseTerm]:
    if isinstance(expr, LicenseTerm):
        return [] if _term_key(expr) in keys else [expr]
    return [term for operand in expr.operands for term in _disallowed_terms(operand, keys)]


def canonical(expr: Expr, allowlist: Iterable[str]) -> str:
    """The expression for reading: each known ID in the allowlist's spelling.

    Operators are upper case and singly spaced. A compound operand of the
    other operator is parenthesized, so ``AND`` inside ``OR`` and ``OR``
    inside ``AND`` both read unambiguously; same-operator nesting is
    flattened, and no other parentheses are kept.
    """
    return _render(expr, _Allowlist.of(allowlist).spelling)


def _render(expr: Expr, spelling: dict[str, str]) -> str:
    if isinstance(expr, LicenseTerm):
        text = spelling.get(expr.license_id.casefold(), expr.license_id)
        text += "+" if expr.plus else ""
        if expr.exception is not None:
            text += " WITH " + spelling.get(expr.exception.casefold(), expr.exception)
        return text
    operator = " OR " if isinstance(expr, Or) else " AND "
    parts = []
    for operand in expr.operands:
        rendered = _render(operand, spelling)
        # A term, or a compound of the same operator, needs no parentheses.
        same = isinstance(operand, LicenseTerm) or type(operand) is type(expr)
        parts.append(rendered if same else f"({rendered})")
    return operator.join(parts)


def license_key(value: object) -> str | None:
    """The value an exception matches: the string as recorded, or compact JSON.

    ``None`` stands for a package with no ``license`` field. It is outside the
    string key space, so a package recording the string ``"-"`` is never
    confused with one recording nothing.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# -- The lockfile ---------------------------------------------------------------


class LockfileError(ValueError):
    """The lockfile cannot be read as an npm v2 or v3 lockfile."""


@dataclass(frozen=True)
class LockedPackage:
    """One package the lockfile locks.

    ``license`` is the raw recorded value, or ``None`` when absent. ``dev`` is
    true for an entry marked ``"dev": true`` or ``"devOptional": true``.
    """

    name: str
    version: str
    license: object
    dev: bool


def read_lockfile(path: Path) -> list[LockedPackage]:
    """Every package in a v2 or v3 ``package-lock.json``.

    Raises :class:`LockfileError` naming the file and the fix for a missing,
    unreadable or pre-v2 lockfile, or for entries with no ``version``.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"cannot read {path}: {exc}; {_FIX_LOCKFILE}"
        raise LockfileError(msg) from exc
    packages = data.get("packages") if isinstance(data, dict) else None
    version = data.get("lockfileVersion") if isinstance(data, dict) else None
    if not isinstance(packages, dict) or not isinstance(version, int) or version < 2:
        msg = (
            f"{path} is not an npm v2 or v3 lockfile (lockfileVersion {version!r}, "
            f"no 'packages' map read); {_FIX_LOCKFILE}"
        )
        raise LockfileError(msg)
    locked: list[LockedPackage] = []
    problems: list[str] = []
    for key, entry in packages.items():
        if key == "":
            # The root entry is the repo itself, not a dependency.
            continue
        if not isinstance(entry, dict):
            problems.append(f"the entry {key!r} is not an object")
            continue
        if entry.get("link") is True:
            # A link points at a directory whose own entry is listed separately
            # (workspaces only, which Vergil does not support), so it carries no
            # package of its own.
            continue
        entry_version = entry.get("version")
        if not isinstance(entry_version, str) or not entry_version:
            problems.append(f"the entry {key!r} has no version")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            name = key.rpartition("node_modules/")[2]
        locked.append(
            LockedPackage(
                name=name,
                version=entry_version,
                license=entry.get("license"),
                dev=entry.get("dev") is True or entry.get("devOptional") is True,
            )
        )
    if problems:
        msg = f"{path}: " + "; ".join(problems) + f"; {_FIX_LOCKFILE}"
        raise LockfileError(msg)
    return locked


# -- Exceptions -----------------------------------------------------------------


@dataclass(frozen=True)
class LicenseException:
    """One entry of :data:`EXCEPTIONS_FILE`.

    ``license`` is the excused value, ``None`` from the bare
    :data:`MISSING_TOKEN`.
    """

    package: str
    license: str | None
    expires: datetime.date
    tracking: str
    reason: str
    line: int

    @property
    def label(self) -> str:
        """The entry's key as written: the package and its quoted value."""
        value = MISSING_TOKEN if self.license is None else json.dumps(self.license)
        return f"{self.package} {value}"


_ENTRY_SHAPE = "an entry is '<package> <license value> exp:<YYYY-MM-DD> <tracking issue> <reason>'"


def parse_exceptions(text: str, *, today: datetime.date, source: str) -> list[LicenseException]:
    """Parse the license exceptions file, refusing every bad entry at once.

    Raises :class:`ExceptionFileError` naming each malformed, expired,
    duplicate or too-long entry by line, so one run shows all of them.
    """
    entries: list[LicenseException] = []
    problems: list[str] = []
    seen: dict[tuple[str, str | None], int] = {}
    for number, line in content_lines(text):
        entry = _parse_entry(line, number)
        if isinstance(entry, str):
            problems.append(f"line {number}: {entry}: {line!r}")
            continue
        key = (entry.package, entry.license)
        if key in seen:
            problems.append(f"line {number}: {entry.label} is already listed on line {seen[key]}")
            continue
        seen[key] = number
        window = window_problem(
            key=entry.label,
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


def _parse_entry(line: str, number: int) -> LicenseException | str:
    """One entry, or the reason the line is not one."""
    parts = line.split(maxsplit=1)
    if len(parts) < 2:
        return f"{_ENTRY_SHAPE}, and this line has 1 of its 5 fields"
    package, rest = parts
    value = _parse_value(rest)
    if isinstance(value, str):
        return value
    license_value, rest = value
    fields = rest.split(maxsplit=2)
    if len(fields) < 3:
        return f"{_ENTRY_SHAPE}, and this line has {2 + len(fields)} of its 5 fields"
    expiry, tracking, reason = fields
    expires = parse_expiry(expiry)
    if isinstance(expires, str):
        return expires
    if not is_tracking_ref(tracking):
        return f"{tracking!r} is not a tracking issue (#N, owner/repo#N or a GitHub issue URL)"
    return LicenseException(package, license_value, expires, tracking, reason.strip(), number)


def _parse_value(rest: str) -> tuple[str | None, str] | str:
    """The license value at the start of *rest* and what follows it, or a problem."""
    if rest.startswith(MISSING_TOKEN) and _ends_field(rest, len(MISSING_TOKEN)):
        return None, rest[len(MISSING_TOKEN) :]
    problem = (
        "the license value must be a JSON string literal, such as "
        f'"SEE LICENSE IN LICENSE", or {MISSING_TOKEN} for a package with no license'
    )
    if not rest.startswith('"'):
        return problem
    try:
        value, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return problem
    if not isinstance(value, str) or not _ends_field(rest, end):
        return problem
    return value, rest[end:]


def _ends_field(text: str, end: int) -> bool:
    """Whether a field ending at *end* is followed by whitespace or nothing."""
    return end == len(text) or text[end].isspace()


# -- The verdict ----------------------------------------------------------------

ALLOWED = "allowed"
EXCEPTED = "excepted"
FAILING = "failing"


@dataclass(frozen=True)
class LicenseRow:
    """The gate's verdict on one locked package."""

    package: LockedPackage
    verdict: str
    canonical: str | None
    reason: str | None = None
    exception: LicenseException | None = None


@dataclass(frozen=True)
class LicenseVerdict:
    """What the gate makes of a lockfile under a set of exceptions.

    ``unmatched`` pairs each exception that covers no failing package with
    why: it matches no package, or only packages that pass without it.
    """

    rows: tuple[LicenseRow, ...]
    applied: tuple[tuple[LicenseException, LicenseRow], ...]
    unmatched: tuple[tuple[LicenseException, str], ...]
    failing: tuple[LicenseRow, ...]


def judge(
    packages: Iterable[LockedPackage],
    *,
    allowlist: Iterable[str],
    exceptions: Iterable[LicenseException],
) -> LicenseVerdict:
    """Judge each package, then apply the live exceptions to the failing ones.

    A package is covered when an exception's ``package`` equals its name and
    its ``license`` equals :func:`license_key` of the recorded value.
    """
    terms = _Allowlist.of(allowlist)
    by_key = {(entry.package, entry.license): entry for entry in exceptions}
    rows: list[LicenseRow] = []
    applied: list[tuple[LicenseException, LicenseRow]] = []
    failing: list[LicenseRow] = []
    covered_failing: set[tuple[str, str | None]] = set()
    covered_passing: set[tuple[str, str | None]] = set()
    for package in sorted(packages, key=lambda p: (p.name, p.version)):
        key = (package.name, license_key(package.license))
        reason, rendered = _standing(package, terms)
        entry = by_key.get(key)
        if reason is None:
            if entry is not None:
                covered_passing.add(key)
            rows.append(LicenseRow(package, ALLOWED, rendered))
        elif entry is not None:
            covered_failing.add(key)
            row = LicenseRow(package, EXCEPTED, rendered, reason, entry)
            rows.append(row)
            applied.append((entry, row))
        else:
            row = LicenseRow(package, FAILING, rendered, reason)
            rows.append(row)
            failing.append(row)
    unmatched: list[tuple[LicenseException, str]] = []
    for key, entry in by_key.items():
        if key in covered_failing:
            continue
        why = (
            "matches only packages that pass without it"
            if key in covered_passing
            else "matches no package in the lockfile"
        )
        unmatched.append((entry, why))
    unmatched.sort(key=lambda pair: pair[0].line)
    return LicenseVerdict(tuple(rows), tuple(applied), tuple(unmatched), tuple(failing))


def _standing(package: LockedPackage, terms: _Allowlist) -> tuple[str | None, str | None]:
    """Why the package fails on its own (``None`` when allowed), and its canonical form."""
    if package.license is None:
        return "it records no license", None
    if not isinstance(package.license, str):
        return "its license is not an SPDX expression (a legacy object or array)", None
    try:
        expr = parse_expression(package.license)
    except LicenseExpressionError as exc:
        return f"its license is not an SPDX expression ({exc})", None
    rendered = _render(expr, terms.spelling)
    if _allowed(expr, terms.keys):
        return None, rendered
    named = ", ".join(_render(term, {}) for term in _disallowed_terms(expr, terms.keys))
    return f"its license is not allowed ({named} not on the allowlist)", rendered


def report(verdict: LicenseVerdict) -> list[dict[str, object]]:
    """The ``licenses.json`` evidence: one object per package the gate checked."""
    out: list[dict[str, object]] = []
    for row in verdict.rows:
        item: dict[str, object] = {
            "name": row.package.name,
            "version": row.package.version,
            "license": license_key(row.package.license),
            "license_canonical": row.canonical,
            "dev": row.package.dev,
            "verdict": row.verdict,
        }
        if row.exception is not None:
            item["exception"] = {
                "tracking": row.exception.tracking,
                "expires": row.exception.expires.isoformat(),
            }
        out.append(item)
    return out
