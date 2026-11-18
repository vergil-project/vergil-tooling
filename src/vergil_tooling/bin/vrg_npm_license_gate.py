"""Gate a TypeScript repo's dependency licenses on its ``package-lock.json``.

The TypeScript AUDIT check runs this command after the npm audit. It judges
every package the lockfile locks, dev dependencies included, against the
TypeScript license allowlist, honoring the live entries of the repo's
``.npm-license-exceptions`` (read from the lockfile's directory), and writes
the ``licenses.json`` evidence. See :mod:`vergil_tooling.lib.npm_licenses`.

The exit status is the gate's own. It exits 0 only when the lockfile was
read, the exceptions file (when present) parsed, and no package fails:

- a lockfile it cannot read exits 1 and writes no report;
- an exceptions file it cannot read or parse exits 1, naming every bad line,
  and still writes the report, judged against no exceptions, so the evidence
  shows each package's real standing;
- otherwise it writes the report, then exits 1 when any package fails.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.languages import _TYPESCRIPT_LICENSES_ALLOWLIST
from vergil_tooling.lib.npm_licenses import (
    EXCEPTIONS_FILE,
    MAX_EXCEPTION_DAYS,
    ExceptionFileError,
    LicenseException,
    LicenseRow,
    LockfileError,
    judge,
    license_key,
    parse_exceptions,
    read_lockfile,
    report,
)
from vergil_tooling.lib.output import emit_error, emit_warning

_ALLOWLIST_HOME = "_TYPESCRIPT_LICENSES_ALLOWLIST in vergil-tooling's lib/languages.py"


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-npm-license-gate",
        description=(
            "Gate the licenses of every package in package-lock.json, dev "
            "dependencies included, against the TypeScript license allowlist, "
            f"honoring the live entries of {EXCEPTIONS_FILE} beside the lockfile, "
            "and write the licenses report."
        ),
    )
    parser.add_argument(
        "--lockfile",
        type=Path,
        default=Path("package-lock.json"),
        metavar="PATH",
        help="the npm v2 or v3 lockfile (default: package-lock.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("licenses.json"),
        metavar="PATH",
        help="the licenses report to write (default: licenses.json)",
    )
    args = parser.parse_args(argv)

    # A report left by an earlier run must never stand in for this one's.
    args.output.unlink(missing_ok=True)
    try:
        packages = read_lockfile(args.lockfile)
    except LockfileError as exc:
        emit_error(str(exc), file=str(args.lockfile))
        return 1

    exceptions_path = args.lockfile.parent / EXCEPTIONS_FILE
    exceptions, exceptions_ok = _load_exceptions(exceptions_path)
    verdict = judge(
        packages,
        allowlist=_TYPESCRIPT_LICENSES_ALLOWLIST.split(";"),
        exceptions=exceptions,
    )
    try:
        args.output.write_text(json.dumps(report(verdict), indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        emit_error(f"cannot write the licenses report {args.output}: {exc}")
        return 1

    for entry, row in verdict.applied:
        print(
            f"npm license exception applied: {_describe_package(row)} until "
            f"{entry.expires.isoformat()}, tracked in {entry.tracking}: {entry.reason}"
        )
    for entry, why in verdict.unmatched:
        _warn_unmatched(exceptions_path, entry, why)
    for row in verdict.failing:
        emit_error(f"{_describe_package(row)}: {row.reason}")
    if verdict.failing:
        print(
            f"To fix a failing package: replace it, add a reviewed entry to "
            f"{EXCEPTIONS_FILE} (at most {MAX_EXCEPTION_DAYS} days, keyed by the "
            f"package and its exact license value), or change the allowlist "
            f"({_ALLOWLIST_HOME})."
        )
    print(
        f"npm license gate: {len(verdict.rows)} package(s) checked, "
        f"{len(verdict.failing)} failing, {len(verdict.applied)} covered by an "
        f"exception, {len(verdict.unmatched)} unmatched exception(s); "
        f"report written to {args.output}"
    )
    return 1 if verdict.failing or not exceptions_ok else 0


def _load_exceptions(path: Path) -> tuple[list[LicenseException], bool]:
    """The live exceptions, and whether the file was read and parsed cleanly.

    A missing file means no exceptions. A file the gate cannot read or parse
    is reported and judged as none, so the report still shows every package.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], True
    except OSError as exc:
        emit_error(f"cannot read {path}: {exc}", file=str(path))
        return [], False
    try:
        return parse_exceptions(text, today=_today(), source=str(path)), True
    except ExceptionFileError as exc:
        for problem in exc.problems:
            emit_error(f"{path}: {problem}", file=str(path))
        return [], False


def _warn_unmatched(path: Path, entry: LicenseException, why: str) -> None:
    emit_warning(
        f"{path}: line {entry.line}: the exception for {entry.label} {why}: remove it",
        file=str(path),
        line=entry.line,
    )


def _today() -> datetime.date:
    """The day the gate runs, in UTC, against which every expiry is checked."""
    return datetime.datetime.now(datetime.UTC).date()


def _describe_package(row: LicenseRow) -> str:
    value = license_key(row.package.license)
    shown = "no license field" if value is None else f"license {json.dumps(value)}"
    return f"{row.package.name}@{row.package.version} ({shown})"


if __name__ == "__main__":
    sys.exit(main())
