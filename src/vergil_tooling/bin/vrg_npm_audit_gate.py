"""Gate an ``npm audit --json`` report, honoring the repo's audit exceptions.

The TypeScript AUDIT check runs ``npm audit``, keeps its JSON as the
``npm-audit.json`` evidence, then runs this command on it. The command drops
the advisories a live entry of ``.npm-audit-exceptions`` covers, lists them so
an acknowledged risk stays visible in the log, and fails on any other
advisory at high or critical. An expired or malformed entry fails the gate;
an entry no advisory matches is reported. See :mod:`vergil_tooling.lib.npm_audit`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.npm_audit import (
    EXCEPTIONS_FILE,
    GATING_SEVERITIES,
    Advisory,
    ExceptionFileError,
    evaluate,
    parse_exceptions,
    parse_report,
)
from vergil_tooling.lib.output import emit_error, emit_warning


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-npm-audit-gate",
        description=(
            "Gate an `npm audit --json` report: drop the advisories a live entry "
            f"of {EXCEPTIONS_FILE} covers, list them, and fail on any other "
            "advisory at high or critical. An expired or malformed entry fails; "
            "an entry that matches no advisory is reported."
        ),
    )
    parser.add_argument("report", type=Path, metavar="REPORT", help="npm audit --json report")
    parser.add_argument(
        "--npm-exit",
        type=int,
        required=True,
        metavar="STATUS",
        help="the exit status of `npm audit --audit-level=high` that wrote REPORT",
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=Path(EXCEPTIONS_FILE),
        metavar="PATH",
        help=f"the exceptions file (default: {EXCEPTIONS_FILE}; absent means none)",
    )
    args = parser.parse_args(argv)

    try:
        advisories = parse_report(json.loads(args.report.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        emit_error(f"cannot read the npm audit report {args.report}: {exc}")
        return 1

    gating = any(advisory.severity in GATING_SEVERITIES for advisory in advisories)
    if gating != (args.npm_exit != 0):
        emit_error(
            f"npm audit exited {args.npm_exit}, but its report at {args.report} "
            f"{'has' if gating else 'has no'} advisory at high or critical: "
            "the report does not match the run"
        )
        return 1

    try:
        text = args.exceptions.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        emit_error(f"cannot read {args.exceptions}: {exc}")
        return 1
    try:
        exceptions = parse_exceptions(text, today=_today(), source=str(args.exceptions))
    except ExceptionFileError as exc:
        for problem in exc.problems:
            emit_error(f"{args.exceptions}: {problem}", file=str(args.exceptions))
        return 1

    verdict = evaluate(advisories, exceptions)
    for entry, advisory in verdict.applied:
        print(
            f"npm audit exception applied: {entry.advisory_id} ({advisory.severity}, "
            f"{', '.join(advisory.packages)}) until {entry.expires.isoformat()}, "
            f"tracked in {entry.tracking}: {entry.reason}"
        )
    for entry in verdict.unmatched:
        emit_warning(
            f"{args.exceptions}: line {entry.line}: the exception for {entry.advisory_id} "
            "matches no advisory in the report: remove it",
            file=str(args.exceptions),
            line=entry.line,
        )
    for advisory in verdict.failing:
        emit_error(_describe(advisory))
    print(
        f"npm audit gate: {len(verdict.failing)} advisory(ies) at high or critical, "
        f"{len(verdict.applied)} covered by an exception, "
        f"{len(verdict.unmatched)} unmatched exception(s)"
    )
    return 1 if verdict.failing else 0


def _today() -> datetime.date:
    """The day the gate runs, in UTC, against which every expiry is checked."""
    return datetime.datetime.now(datetime.UTC).date()


def _describe(advisory: Advisory) -> str:
    name = advisory.advisory_id or "an advisory with no GHSA ID"
    return (
        f"{name} ({advisory.severity}, {', '.join(advisory.packages)}): "
        f"{advisory.title} <{advisory.url}>"
    )


if __name__ == "__main__":
    sys.exit(main())
