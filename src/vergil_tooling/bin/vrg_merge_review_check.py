"""Check a stored merge-review result against the request it answers.

``vrg-merge-review-check --request <path> --result <path>`` runs the result
check of ``docs/specs/merge-review-protocol.md`` on the reviewer's reply,
stored unchanged in the result file by the plugin's capture hook. It first
checks the hook's capture record, ``capture-<step>.json`` in the result's
directory (``<step>`` from the request), and refuses a result whose bytes are
not the ones the hook captured (rule 10). It then checks the coverage line
(rule 11): the reply's last non-empty line must read
``Diff read: <read> of <total> lines``, with ``<total>`` the diff's line count
from the request and ``<read>`` equal to it, so a partial read is refused. In
a check, the line before it must read ``Fix diff read: <read> of <total>
lines`` the same way, for the fix diff. A diff over 2000 lines is also listed
in parts in the request (``diff_parts``, ``fix_diff_parts``), which the check
verifies sum to the total. On success it exits 0 and prints a
JSON summary on stdout::

    {"step": "review",
     "counts": {"defect": 1, "design-question": 0, "out-of-scope": 0, "pre-existing": 0},
     "security": 0,
     "fix_status": {}}

``counts`` counts every finding by kind, ``security`` is the number of
security findings and ``fix_status`` maps each ID the request listed to its
status (empty in a review). On any violation it prints ``ERROR: <rule>`` on
stderr, naming the rule broken but never the refused value, and exits 1.
It reads only the two files and the capture record, and writes nothing.

Exit codes: ``0`` the result passed, ``1`` it failed or a file could not be
read, ``2`` a usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.merge_review.capture import capture_path, check_capture
from vergil_tooling.lib.merge_review.result import (
    Result,
    ResultError,
    check_request,
    parse_result,
)
from vergil_tooling.lib.merge_review.schema import KINDS

EXIT_OK = 0
EXIT_ERROR = 1


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result_path = Path(args.result)
    try:
        request = _load_request(Path(args.request))
        checked = check_request(request)
        result_bytes = _read_bytes(result_path, "result")
        check_capture(
            capture_path(result_path, checked.step),
            result_bytes,
            step=checked.step,
            request_id=checked.request_id,
        )
        result = parse_result(_decode(result_bytes, result_path, "result"), request)
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
    return _decode(_read_bytes(path, what), path, what)


def _read_bytes(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        msg = f"cannot read the {what} file {path}: {exc.strerror}"
        raise ResultError(msg) from exc


def _decode(raw: bytes, path: Path, what: str) -> str:
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
