"""Check a stored merge-review result against the request it answers.

``vrg-merge-review-check --request <path> --result <path>`` runs the result
check of ``docs/specs/merge-review-protocol.md`` on the reviewer's reply,
stored unchanged in the result file. On success it exits 0 and prints a JSON
summary on stdout::

    {"step": "review",
     "counts": {"defect": 1, "design-question": 0, "out-of-scope": 0, "pre-existing": 0},
     "security": 0,
     "fix_status": {}}

``counts`` counts every finding by kind, ``security`` is the number of
security findings and ``fix_status`` maps each ID the request listed to its
status (empty in a review). On any violation it prints ``ERROR: <rule>`` on
stderr, naming the rule broken but never the refused value, and exits 1.
It reads only the two files, and writes nothing.

Exit codes: ``0`` the result passed, ``1`` it failed or a file could not be
read, ``2`` a usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.merge_review.result import Result, ResultError, parse_result
from vergil_tooling.lib.merge_review.schema import KINDS

EXIT_OK = 0
EXIT_ERROR = 1


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        request = _load_request(Path(args.request))
        result = parse_result(_read_text(Path(args.result), "result"), request)
    except ResultError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(summarize(result)))
    return EXIT_OK


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-merge-review-check",
        description="Check a stored merge-review result against the request it answers.",
    )
    parser.add_argument("--request", required=True, help="the request-<step>.json file")
    parser.add_argument("--result", required=True, help="the result-<step>.md file")
    return parser.parse_args(argv)


def summarize(result: Result) -> dict[str, object]:
    """The JSON summary printed for a result that passed."""
    counts = dict.fromkeys(KINDS, 0)
    for finding in result.findings:
        counts[finding.kind] += 1
    return {
        "step": result.step,
        "counts": counts,
        "security": sum(1 for finding in result.findings if finding.security),
        "fix_status": dict(result.fix_status),
    }


def _read_text(path: Path, what: str) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        msg = f"cannot read the {what} file {path}: {exc.strerror}"
        raise ResultError(msg) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # The offending bytes are not shown: they could carry a detail.
        msg = f"the {what} file {path} is not UTF-8 at byte offset {exc.start}"
        raise ResultError(msg) from exc


def _load_request(path: Path) -> dict[str, object]:
    try:
        data = json.loads(_read_text(path, "request"))
    except json.JSONDecodeError as exc:
        msg = f"the request file {path} is not valid JSON: {exc.msg} at line {exc.lineno}"
        raise ResultError(msg) from exc
    if not isinstance(data, dict):
        msg = f"the request file {path} must hold a JSON object"
        raise ResultError(msg)
    return data


if __name__ == "__main__":
    sys.exit(main())
