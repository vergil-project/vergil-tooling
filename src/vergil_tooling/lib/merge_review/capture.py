"""The capture rule: check a stored result against the capture hook's record.

The plugin's ``capture-merge-review-result`` SubagentStop hook stores the
reviewer's last message byte for byte as ``result-<step>.md`` and writes a
capture record beside it, ``capture-<step>.json``::

    {"schema": "merge-review-capture/1", "step": "<step>",
     "request_id": "<the request's request_id>",
     "result_sha256": "<lowercase hex SHA-256 of result-<step>.md>",
     "agent_id": "<the reviewer subagent's ID>"}

:func:`check_capture` runs rule 10 of the result check
(``docs/specs/merge-review-protocol.md``, "The result check"): the record is
present and well formed, answers the same step and request, and holds the
SHA-256 of the result's bytes as read. A result edited after the capture is
therefore refused. A refusal never echoes a value from the record.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.merge_review.result import ResultError
from vergil_tooling.lib.merge_review.schema import (
    CAPTURE_FIELDS,
    CAPTURE_SCHEMA,
    SHA256_RE,
    capture_file,
)

if TYPE_CHECKING:
    from pathlib import Path

RULE_CAPTURE = "rule 10 (capture)"


def capture_path(result_path: Path, step: str) -> Path:
    """The capture record of ``step``'s result: ``capture-<step>.json`` beside it."""
    return result_path.parent / capture_file(step)


def check_capture(record_path: Path, result_bytes: bytes, *, step: str, request_id: str) -> None:
    """Refuse unless the record at ``record_path`` vouches for ``result_bytes``.

    ``step`` and ``request_id`` are the checked request's. Raises
    :class:`ResultError` naming rule 10 and what is wrong, never a value.
    """
    record = _load_record(record_path)
    if record["schema"] != CAPTURE_SCHEMA:
        msg = f"{RULE_CAPTURE}: the capture record's schema must be {CAPTURE_SCHEMA}"
        raise ResultError(msg)
    if record["step"] != step:
        msg = f"{RULE_CAPTURE}: the capture record's step is not the request's step ({step})"
        raise ResultError(msg)
    if record["request_id"] != request_id:
        msg = f"{RULE_CAPTURE}: the capture record's request_id is not the request's"
        raise ResultError(msg)
    if SHA256_RE.fullmatch(record["result_sha256"]) is None:
        msg = (
            f"{RULE_CAPTURE}: the capture record's result_sha256 must be 64 lowercase"
            " hex characters"
        )
        raise ResultError(msg)
    if hashlib.sha256(result_bytes).hexdigest() != record["result_sha256"]:
        msg = (
            f"{RULE_CAPTURE}: the result file's SHA-256 is not the capture record's"
            " result_sha256; the result was changed after the capture"
        )
        raise ResultError(msg)


def _load_record(path: Path) -> dict[str, str]:
    """The record as an object of exactly the five string fields."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        msg = f"{RULE_CAPTURE}: the result has no capture record {path}"
        raise ResultError(msg) from exc
    except OSError as exc:
        msg = f"{RULE_CAPTURE}: cannot read the capture record {path}: {exc.strerror}"
        raise ResultError(msg) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{RULE_CAPTURE}: the capture record {path} is not UTF-8 at byte offset {exc.start}"
        raise ResultError(msg) from exc
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant)
    except json.JSONDecodeError as exc:
        msg = (
            f"{RULE_CAPTURE}: the capture record {path} is not valid JSON: {exc.msg}"
            f" at line {exc.lineno} column {exc.colno}"
        )
        raise ResultError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{RULE_CAPTURE}: the capture record {path} must hold one JSON object"
        raise ResultError(msg)
    extra = set(data) - CAPTURE_FIELDS
    if extra:
        # Counted, never named: a key could carry a detail.
        msg = (
            f"{RULE_CAPTURE}: the capture record may carry only"
            f" {', '.join(sorted(CAPTURE_FIELDS))}; it has {len(extra)} other key(s)"
        )
        raise ResultError(msg)
    missing = sorted(CAPTURE_FIELDS - set(data))
    if missing:
        msg = f"{RULE_CAPTURE}: the capture record is missing {', '.join(missing)}"
        raise ResultError(msg)
    for key in sorted(CAPTURE_FIELDS):
        if not isinstance(data[key], str) or not data[key]:
            msg = f"{RULE_CAPTURE}: the capture record's {key} must be a non-empty string"
            raise ResultError(msg)
    return data


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]
    if len(set(keys)) != len(keys):
        # The key is not named: a key could carry a detail.
        msg = f"{RULE_CAPTURE}: the capture record has an object with a duplicate key"
        raise ResultError(msg)
    return dict(pairs)


def _no_constant(_name: str) -> Any:
    msg = f"{RULE_CAPTURE}: the capture record uses NaN or Infinity, which is not JSON"
    raise ResultError(msg)
