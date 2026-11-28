"""The merge-review result check: parse a reviewer's reply against its request.

A reply opens, after optional whitespace, with one fenced block whose info
string is ``merge-review-result`` and which holds one JSON object; free prose
for the human follows. :func:`parse_result` runs every rule of the protocol's
result check (``docs/specs/merge-review-protocol.md``, "The result check") and
returns the checked :class:`Result`, or raises :class:`ResultError` naming the
rule broken.

A refusal never echoes a refused value. It names the rule, the finding by its
ID (or by its position when the ID itself is bad) and, for a bad character,
its code point and offset. Unknown keys are counted, never named. A security
detail placed by mistake in a summary or a key therefore never reaches a
terminal or a log.

The request is checked too, for the fields the check reads, so a malformed
request is refused rather than trusted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib.merge_review.schema import (
    CHANGE_CLASSES,
    CHECK_RESULT_FIELDS,
    DIFF_READ_RE,
    FINDING_FIELDS,
    FINDING_ID_MAX,
    FINDING_OPTIONAL_FIELDS,
    FIX_READ_RE,
    FIX_STATUS_FIELDS,
    FIX_STATUSES,
    KINDS,
    MAX_READ_LINES,
    QUESTIONS,
    REQUEST_ID_RE,
    REQUEST_SCHEMA,
    RESULT_BLOCK,
    RESULT_FIELDS,
    RESULT_SCHEMA,
    STEP_RE,
    SUMMARY_MAX,
    earlier_steps,
    finding_id_re,
    is_check,
    step_id_prefix,
)
from vergil_tooling.lib.rulings.marker import MARKER_PREFIX

_RULE_SHAPE = "rule 1 (shape)"
_RULE_FIELDS = "rule 2 (fields)"
_RULE_IDENTITY = "rule 3 (identity)"
_RULE_VALUES = "rule 4 (values)"
_RULE_IDS = "rule 5 (finding IDs)"
_RULE_SUMMARY = "rule 6 (summaries)"
_RULE_LOCATION = "rule 7 (location)"
_RULE_CHANGE_CLASS = "rule 8 (change class)"
_RULE_CHECK = "rule 9 (check steps)"
_RULE_COVERAGE = "rule 11 (coverage)"

#: The opening block: the first content, an exact fence line, then the JSON
#: up to the first line that is a bare closing fence.
_OPENING_BLOCK = re.compile(
    rf"```{re.escape(RESULT_BLOCK)}[ \t]*\r?\n(?P<body>.*?)^```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)

#: Any fence line, backtick or tilde, whose info string is the result block's.
_ANY_RESULT_FENCE = re.compile(
    rf"^[ \t]*(?:`{{3,}}|~{{3,}})[ \t]*{re.escape(RESULT_BLOCK)}(?![A-Za-z0-9_-])",
    re.MULTILINE,
)


class ResultError(Exception):
    """A result, or the request it answers, fails the result check."""


@dataclass(frozen=True)
class Finding:
    """One checked finding. ``summary`` is ``None`` exactly when ``security`` is true."""

    id: str
    question: str
    kind: str
    security: bool
    location: str
    summary: str | None


@dataclass(frozen=True)
class Result:
    """A checked result. ``fix_status`` maps each listed ID to its status (empty in a review)."""

    step: str
    request_id: str
    findings: tuple[Finding, ...]
    fix_status: dict[str, str]


@dataclass(frozen=True)
class Request:
    """The request fields the check reads, checked.

    ``fix_diff_lines`` is ``None`` in a review, which has no fix diff.
    """

    step: str
    request_id: str
    change_class: str
    earlier_finding_ids: tuple[str, ...]
    diff_lines: int
    fix_diff_lines: int | None


def parse_result(text: str, request: dict[str, object]) -> Result:
    """Check the reply ``text`` against ``request`` and return the checked result.

    Raises :class:`ResultError` naming the rule broken; nothing is returned
    for a result that fails any rule.
    """
    req = check_request(request)
    block = _extract_block(text)
    step = req.step
    if not is_check(step) and "fix_status" in block:
        msg = f"{_RULE_CHECK}: a review result has no fix_status"
        raise ResultError(msg)
    allowed = CHECK_RESULT_FIELDS if is_check(step) else RESULT_FIELDS
    _check_keys(block, required=allowed, optional=frozenset(), where="the result block")
    _check_identity(block, req)
    findings = _check_findings(block["findings"], req)
    fix_status = _check_fix_status(block["fix_status"], req) if is_check(step) else {}
    _check_coverage(text, req)
    return Result(step, req.request_id, findings, fix_status)


def check_request(request: dict[str, object]) -> Request:
    """Check the request fields the result check reads, and return them.

    Raises :class:`ResultError` for a malformed request, which is refused
    rather than trusted.
    """
    if request.get("schema") != REQUEST_SCHEMA:
        msg = f"the request: schema must be {REQUEST_SCHEMA}"
        raise ResultError(msg)
    step = request.get("step")
    if not isinstance(step, str) or STEP_RE.fullmatch(step) is None:
        msg = "the request: step must be review, check or check-<k> with k from 2 to 9"
        raise ResultError(msg)
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or REQUEST_ID_RE.fullmatch(request_id) is None:
        msg = "the request: request_id must be 32 lowercase hex characters"
        raise ResultError(msg)
    change_class = request.get("change_class")
    if not isinstance(change_class, str) or change_class not in CHANGE_CLASSES:
        msg = f"the request: change_class must be one of {', '.join(CHANGE_CLASSES)}"
        raise ResultError(msg)
    earlier: tuple[str, ...] = ()
    if is_check(step):
        earlier = _check_earlier_ids(request.get("earlier_finding_ids"), step)
    diff_lines = _check_count(request, "diff_lines")
    _check_parts(request, "diff_parts", diff_lines)
    fix_diff_lines: int | None = None
    if is_check(step):
        fix_diff_lines = _check_count(request, "fix_diff_lines")
        _check_parts(request, "fix_diff_parts", fix_diff_lines)
    return Request(step, request_id, change_class, earlier, diff_lines, fix_diff_lines)


def _check_count(request: dict[str, object], key: str) -> int:
    """A line count the request must carry: a whole number, never a bool or negative."""
    value = request.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"the request: {key} must be a whole number of lines"
        raise ResultError(msg)
    return value


def _check_parts(request: dict[str, object], key: str, total: int) -> None:
    """The manifest of a large diff, present exactly when *total* exceeds one Read.

    Each entry is ``{"path", "lines"}`` with ``lines`` from 1 to
    :data:`MAX_READ_LINES`, and the entries sum to *total*.
    """
    if total <= MAX_READ_LINES:
        if key in request:
            msg = f"the request: {key} is present, but the diff is not over {MAX_READ_LINES} lines"
            raise ResultError(msg)
        return
    parts = request.get(key)
    if not isinstance(parts, list) or not parts:
        msg = f"the request: {key} must list the parts of a diff over {MAX_READ_LINES} lines"
        raise ResultError(msg)
    summed = 0
    for position, part in enumerate(parts, start=1):
        if not isinstance(part, dict) or set(part) != {"path", "lines"}:
            msg = f"the request: {key} entry {position} must hold exactly path and lines"
            raise ResultError(msg)
        path, lines = part["path"], part["lines"]
        if not isinstance(path, str) or isinstance(lines, bool) or not isinstance(lines, int):
            msg = f"the request: {key} entry {position} must hold a path and a line count"
            raise ResultError(msg)
        if not 1 <= lines <= MAX_READ_LINES:
            msg = f"the request: {key} entry {position} must hold from 1 to {MAX_READ_LINES} lines"
            raise ResultError(msg)
        summed += lines
    if summed != total:
        msg = f"the request: {key} lines sum to {summed}, not the {total} lines of the diff"
        raise ResultError(msg)


def _check_coverage(text: str, req: Request) -> None:
    """Rule 11: the reply's last non-empty line reports the whole diff as read.

    That line must read ``Diff read: <read> of <total> lines``, with ``<total>``
    the request's ``diff_lines`` and ``<read>`` equal to it. In a check, the
    non-empty line before it must read ``Fix diff read: <read> of <total>
    lines`` the same way, against the request's ``fix_diff_lines``. Trailing
    whitespace and a CR are stripped before the match.
    """
    lines = [stripped for stripped in (line.rstrip() for line in text.split("\n")) if stripped]
    diff_match = DIFF_READ_RE.fullmatch(lines[-1]) if lines else None
    if diff_match is None:
        msg = (
            f"{_RULE_COVERAGE}: the last non-empty line must read 'Diff read: <read> of"
            " <total> lines'"
        )
        raise ResultError(msg)
    _check_read(diff_match, "Diff read", req.diff_lines)
    if is_check(req.step):
        fix_match = FIX_READ_RE.fullmatch(lines[-2]) if len(lines) >= 2 else None
        if fix_match is None:
            msg = (
                f"{_RULE_COVERAGE}: the non-empty line before the last must read"
                " 'Fix diff read: <read> of <total> lines'"
            )
            raise ResultError(msg)
        _check_read(fix_match, "Fix diff read", req.fix_diff_lines)


def _check_read(match: re.Match[str], label: str, total: int | None) -> None:
    """Refuse a coverage line whose total is not *total*, or that reads less than all of it.

    A *total* of ``None`` (a check's count missing from the request, which
    :func:`check_request` already refuses) matches no line and so fails closed.
    """
    read, stated = int(match.group(1)), int(match.group(2))
    if stated != total:
        msg = (
            f"{_RULE_COVERAGE}: the {label} line gives {stated} lines in total,"
            f" but the diff has {total}"
        )
        raise ResultError(msg)
    if read != total:
        msg = (
            f"{_RULE_COVERAGE}: the {label} line reads {read} of {total} lines:"
            " the whole diff was not read"
        )
        raise ResultError(msg)


def _check_earlier_ids(raw: object, step: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        msg = "the request: a check's earlier_finding_ids must be a list of finding IDs"
        raise ResultError(msg)
    patterns = [finding_id_re(earlier) for earlier in earlier_steps(step)]
    seen: list[str] = []
    for position, finding_id in enumerate(raw, start=1):
        if not isinstance(finding_id, str) or not any(p.fullmatch(finding_id) for p in patterns):
            msg = (
                f"the request: earlier_finding_ids entry {position} is not a finding ID"
                f" of a step before {step}"
            )
            raise ResultError(msg)
        if finding_id in seen:
            msg = f"the request: earlier_finding_ids lists {finding_id} more than once"
            raise ResultError(msg)
        seen.append(finding_id)
    return tuple(seen)


def _extract_block(text: str) -> dict[str, Any]:
    """The opening block's JSON object (rule 1)."""
    start = len(text) - len(text.lstrip())
    match = _OPENING_BLOCK.match(text, start)
    if match is None:
        msg = (
            f"{_RULE_SHAPE}: the reply must open, after optional whitespace, with a"
            f" ```{RESULT_BLOCK} fenced block closed by a ``` line"
        )
        raise ResultError(msg)
    if _ANY_RESULT_FENCE.search(text, match.end()):
        msg = f"{_RULE_SHAPE}: the reply has a second {RESULT_BLOCK} block"
        raise ResultError(msg)
    try:
        data = json.loads(
            match.group("body"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_no_constant,
        )
    except json.JSONDecodeError as exc:
        msg = (
            f"{_RULE_SHAPE}: the block is not valid JSON: {exc.msg}"
            f" at line {exc.lineno} column {exc.colno}"
        )
        raise ResultError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{_RULE_SHAPE}: the block must hold one JSON object"
        raise ResultError(msg)
    return data


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        # The key is not named: a key could carry a detail.
        msg = f"{_RULE_SHAPE}: the block has an object with a duplicate key"
        raise ResultError(msg)
    return dict(pairs)


def _no_constant(_name: str) -> Any:
    msg = f"{_RULE_SHAPE}: the block uses NaN or Infinity, which is not JSON"
    raise ResultError(msg)


def _check_keys(
    raw: dict[str, Any], *, required: frozenset[str], optional: frozenset[str], where: str
) -> None:
    """Rule 2: every required key present, and no key the schema does not define."""
    extra = set(raw) - required - optional
    if extra:
        # The extra keys are counted, never named: a key could carry a detail.
        msg = (
            f"{_RULE_FIELDS}: {where} may carry only {', '.join(sorted(required | optional))};"
            f" it has {len(extra)} other key(s)"
        )
        raise ResultError(msg)
    missing = sorted(required - set(raw))
    if missing:
        msg = f"{_RULE_FIELDS}: {where} is missing {', '.join(missing)}"
        raise ResultError(msg)


def _check_identity(block: dict[str, Any], req: Request) -> None:
    """Rules 2 and 3 for the top level: types, then schema, step and request ID."""
    for key in ("schema", "step", "request_id"):
        if not isinstance(block[key], str):
            msg = f"{_RULE_FIELDS}: the result's {key} must be a string"
            raise ResultError(msg)
    if not isinstance(block["findings"], list):
        msg = f"{_RULE_FIELDS}: the result's findings must be a list"
        raise ResultError(msg)
    if is_check(req.step) and not isinstance(block["fix_status"], list):
        msg = f"{_RULE_FIELDS}: the result's fix_status must be a list"
        raise ResultError(msg)
    if block["schema"] != RESULT_SCHEMA:
        msg = f"{_RULE_IDENTITY}: the result's schema must be {RESULT_SCHEMA}"
        raise ResultError(msg)
    if block["step"] != req.step:
        msg = f"{_RULE_IDENTITY}: the result's step is not the request's step ({req.step})"
        raise ResultError(msg)
    if block["request_id"] != req.request_id:
        msg = f"{_RULE_IDENTITY}: the result's request_id is not the request's"
        raise ResultError(msg)


def _check_findings(raw_findings: list[Any], req: Request) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    seen: set[str] = set()
    id_re = finding_id_re(req.step)
    for position, raw in enumerate(raw_findings, start=1):
        finding = _check_finding(raw, position, req, id_re)
        if finding.id in seen:
            msg = f"{_RULE_IDS}: finding {finding.id} appears more than once"
            raise ResultError(msg)
        seen.add(finding.id)
        findings.append(finding)
    return tuple(findings)


def _check_finding(raw: Any, position: int, req: Request, id_re: re.Pattern[str]) -> Finding:
    where = f"finding {position}"
    if not isinstance(raw, dict):
        msg = f"{_RULE_FIELDS}: {where} is not a JSON object"
        raise ResultError(msg)
    _check_keys(raw, required=FINDING_FIELDS, optional=FINDING_OPTIONAL_FIELDS, where=where)
    finding_id = raw["id"]
    if (
        not isinstance(finding_id, str)
        or len(finding_id) > FINDING_ID_MAX
        or id_re.fullmatch(finding_id) is None
    ):
        msg = (
            f"{_RULE_IDS}: {where} has no valid id for step {req.step}: an id is"
            f" {step_id_prefix(req.step)}<n> with <n> from 1 to 999, at most"
            f" {FINDING_ID_MAX} characters"
        )
        raise ResultError(msg)
    where = f"finding {finding_id}"
    for key in ("question", "kind", "location"):
        if not isinstance(raw[key], str):
            msg = f"{_RULE_FIELDS}: {where}: {key} must be a string"
            raise ResultError(msg)
    if not isinstance(raw["security"], bool):
        msg = f"{_RULE_FIELDS}: {where}: security must be true or false"
        raise ResultError(msg)
    if raw["question"] not in QUESTIONS:
        msg = f"{_RULE_VALUES}: {where}: question must be one of {', '.join(QUESTIONS)}"
        raise ResultError(msg)
    if raw["kind"] not in KINDS:
        msg = f"{_RULE_VALUES}: {where}: kind must be one of {', '.join(KINDS)}"
        raise ResultError(msg)
    summary = _check_summary(raw, where)
    _check_line(raw["location"], rule=_RULE_LOCATION, what=f"{where}: the location")
    if req.change_class == "docs-only" and raw["question"] == "production-safety":
        msg = (
            f"{_RULE_CHANGE_CLASS}: {where}: a docs-only change gets only the"
            " merge-quality question, not production-safety"
        )
        raise ResultError(msg)
    if is_check(req.step) and raw["kind"] == "pre-existing":
        msg = f"{_RULE_CHECK}: {where}: a check never raises a pre-existing finding"
        raise ResultError(msg)
    return Finding(
        finding_id, raw["question"], raw["kind"], raw["security"], raw["location"], summary
    )


def _check_summary(raw: dict[str, Any], where: str) -> str | None:
    """Rule 6: no summary on a security finding; a checked one on any other."""
    if raw["security"]:
        if "summary" in raw:
            msg = f"{_RULE_SUMMARY}: {where}: a security finding carries no summary"
            raise ResultError(msg)
        return None
    if "summary" not in raw:
        msg = f"{_RULE_SUMMARY}: {where}: a finding that is not security needs a summary"
        raise ResultError(msg)
    summary = raw["summary"]
    if not isinstance(summary, str):
        msg = f"{_RULE_FIELDS}: {where}: summary must be a string"
        raise ResultError(msg)
    if len(summary) > SUMMARY_MAX:
        msg = f"{_RULE_SUMMARY}: {where}: the summary is over {SUMMARY_MAX} characters"
        raise ResultError(msg)
    _check_line(summary, rule=_RULE_SUMMARY, what=f"{where}: the summary")
    return summary


def _check_line(value: str, *, rule: str, what: str) -> None:
    """One non-blank line of printable ASCII, with no vergil marker prefix."""
    for offset, char in enumerate(value):
        if not " " <= char <= "~":
            msg = (
                f"{rule}: {what} has character U+{ord(char):04X} at offset {offset};"
                " only printable ASCII (U+0020 to U+007E) is allowed"
            )
            raise ResultError(msg)
    if not value.strip():
        msg = f"{rule}: {what} is empty"
        raise ResultError(msg)
    # The value is ASCII here, so lower() keeps every offset in place.
    offset = value.lower().find(MARKER_PREFIX.lower())
    if offset != -1:
        msg = f"{rule}: {what} contains the vergil marker prefix at offset {offset}"
        raise ResultError(msg)


def _check_fix_status(raw_entries: list[Any], req: Request) -> dict[str, str]:
    """Rule 9: exactly one status for each listed ID, and none for any other."""
    statuses: dict[str, str] = {}
    for position, raw in enumerate(raw_entries, start=1):
        where = f"fix_status entry {position}"
        if not isinstance(raw, dict):
            msg = f"{_RULE_FIELDS}: {where} is not a JSON object"
            raise ResultError(msg)
        _check_keys(raw, required=FIX_STATUS_FIELDS, optional=frozenset(), where=where)
        entry_id = raw["id"]
        if entry_id not in req.earlier_finding_ids:
            # Named by position: an ID the request did not list is a refused value.
            msg = f"{_RULE_CHECK}: {where} is for an ID the request did not list"
            raise ResultError(msg)
        if entry_id in statuses:
            msg = f"{_RULE_CHECK}: fix_status has more than one entry for {entry_id}"
            raise ResultError(msg)
        if raw["status"] not in FIX_STATUSES:
            msg = (
                f"{_RULE_VALUES}: the fix_status entry for {entry_id}: status must be"
                f" one of {', '.join(FIX_STATUSES)}"
            )
            raise ResultError(msg)
        statuses[entry_id] = raw["status"]
    missing = [finding_id for finding_id in req.earlier_finding_ids if finding_id not in statuses]
    if missing:
        msg = f"{_RULE_CHECK}: fix_status has no entry for {', '.join(missing)}"
        raise ResultError(msg)
    return statuses
