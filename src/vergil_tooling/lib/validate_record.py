"""The record of what the last ``vrg-validate`` run checked and passed.

``vrg-validate`` writes ``.vergil/vrg-validate-last.json`` in the repository
root after every pipeline run, pass or fail, interrupted or not::

    {"head": "<sha>", "exit_code": 0, "full": true, "clean_at_start": true,
     "stages": [{"name": "common", "status": "ok"}, ...]}

``head`` is the commit checked out when the run started, ``full`` is true
when the run had no ``--check`` (an ``--all-versions`` run only adds test
stages, so it is full), and ``clean_at_start`` is whether the working tree
was clean when it started, so the stages judged exactly ``head``.

The merge-review request builder reads it back to refuse a review of a
branch that was not fully validated at its current commit
(epic vergil-project/.github#881, spec §6.1 step 1, §8.2). ``.vergil/`` is
ignored by git, so writing the record never dirties the tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.progress import StageStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.progress import StageResult

#: The record's path, relative to the repository root.
RECORD_PATH = Path(".vergil") / "vrg-validate-last.json"

#: The stage statuses that count as passed. Every other status (``failed``,
#: ``skipped``, ``interrupted``) does not.
PASSED_STATUSES = frozenset({"ok", "warn"})

_STATUSES = frozenset(get_args(StageStatus))
_FIELDS = frozenset({"head", "exit_code", "full", "clean_at_start", "stages"})
_STAGE_FIELDS = frozenset({"name", "status"})


class ValidateRecordError(Exception):
    """The record is missing, unreadable or malformed."""


@dataclass(frozen=True)
class ValidateRecord:
    """A read-back record. ``passed`` and ``not_passed`` keep the stages' order."""

    head: str
    exit_code: int
    full: bool
    clean_at_start: bool
    passed: tuple[str, ...]
    not_passed: tuple[str, ...]


def write_record(
    repo_root: Path,
    *,
    head: str,
    exit_code: int,
    full: bool,
    clean_at_start: bool,
    results: Sequence[StageResult],
) -> Path:
    """Write the record of one run into *repo_root* and return its path.

    The file is replaced atomically, so a reader never sees half a record.
    """
    path = repo_root / RECORD_PATH
    data = {
        "head": head,
        "exit_code": exit_code,
        "full": full,
        "clean_at_start": clean_at_start,
        "stages": [{"name": r.name, "status": r.status} for r in results],
    }
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode())
    return path


def read_record(repo_root: Path) -> ValidateRecord:
    """Read the record in *repo_root*, refusing anything not exactly its shape."""
    path = repo_root / RECORD_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        msg = f"no vrg-validate record at {path}: run vrg-container-run -- vrg-validate"
        raise ValidateRecordError(msg) from exc
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read the vrg-validate record {path}: {exc}"
        raise ValidateRecordError(msg) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"the vrg-validate record {path} is not valid JSON: {exc.msg}"
        raise ValidateRecordError(msg) from exc
    return _parse(data, path)


def _parse(data: Any, path: Path) -> ValidateRecord:
    bad = f"the vrg-validate record {path} is malformed"
    if not isinstance(data, dict) or set(data) != _FIELDS:
        msg = f"{bad}: it must hold exactly {', '.join(sorted(_FIELDS))}"
        raise ValidateRecordError(msg)
    head, exit_code = data["head"], data["exit_code"]
    if not isinstance(head, str) or not head:
        msg = f"{bad}: head must be a commit SHA"
        raise ValidateRecordError(msg)
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        msg = f"{bad}: exit_code must be an integer"
        raise ValidateRecordError(msg)
    for key in ("full", "clean_at_start"):
        if not isinstance(data[key], bool):
            msg = f"{bad}: {key} must be true or false"
            raise ValidateRecordError(msg)
    if not isinstance(data["stages"], list):
        msg = f"{bad}: stages must be a list"
        raise ValidateRecordError(msg)
    passed: list[str] = []
    not_passed: list[str] = []
    for position, stage in enumerate(data["stages"], start=1):
        if (
            not isinstance(stage, dict)
            or set(stage) != _STAGE_FIELDS
            or not isinstance(stage["name"], str)
            or not isinstance(stage["status"], str)
            or stage["status"] not in _STATUSES
        ):
            msg = f"{bad}: stage {position} must be {{name, status}} with a known status"
            raise ValidateRecordError(msg)
        (passed if stage["status"] in PASSED_STATUSES else not_passed).append(stage["name"])
    return ValidateRecord(
        head=head,
        exit_code=exit_code,
        full=data["full"],
        clean_at_start=data["clean_at_start"],
        passed=tuple(passed),
        not_passed=tuple(not_passed),
    )
