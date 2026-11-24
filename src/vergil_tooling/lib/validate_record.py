"""The record of what the last ``vrg-validate`` run checked and passed.

``vrg-validate`` writes ``.vergil/vrg-validate-last.json`` in the repository
root after every pipeline run, pass or fail, interrupted or not::

    {"head": "<sha>", "exit_code": 0, "full": true, "clean_at_start": true,
     "stages": [{"name": "common", "status": "ok"}, ...]}

``head`` is the commit checked out when the run started, ``full`` is true
when the run had no ``--check`` (an ``--all-versions`` run only adds test
stages, so it is full), and ``clean_at_start`` is whether the working tree
was clean when it started, so the stages judged exactly ``head``.

A stage ``vrg-validate`` skipped because the repository is not bootstrapped
yet (issue #4384) is recorded ``warn`` and also carries why, so a reader
need not repeat the manifest and source tests to tell that skip from any
other warning (issue #5034)::

    {"name": "test", "status": "warn",
     "skip": {"reason": "not-bootstrapped", "language": "python",
              "missing_manifests": ["pyproject.toml", "uv.lock"]}}

``skip`` is present only on such a stage, so the record of a run that skips
nothing keeps exactly the shape above. Its ``reason`` is one of
:data:`SKIP_REASONS`. A reader released before ``skip`` existed refuses a
record that carries one as malformed.

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
    from collections.abc import Mapping, Sequence

    from vergil_tooling.lib.progress import StageResult

#: The record's path, relative to the repository root.
RECORD_PATH = Path(".vergil") / "vrg-validate-last.json"

#: The stage statuses that count as passed. Every other status (``failed``,
#: ``skipped``, ``interrupted``) does not.
PASSED_STATUSES = frozenset({"ok", "warn"})

_STATUSES = frozenset(get_args(StageStatus))
_FIELDS = frozenset({"head", "exit_code", "full", "clean_at_start", "stages"})
_STAGE_FIELDS = frozenset({"name", "status"})
_SKIP_FIELDS = frozenset({"reason", "language", "missing_manifests"})

#: The reason a stage skipped because the repo is not bootstrapped records.
NOT_BOOTSTRAPPED = "not-bootstrapped"

#: The reasons a stage's ``skip`` may give.
SKIP_REASONS = frozenset({NOT_BOOTSTRAPPED})


class ValidateRecordError(Exception):
    """The record is missing, unreadable or malformed."""


@dataclass(frozen=True)
class StageSkip:
    """Why ``vrg-validate`` skipped a stage, recorded as the stage's ``skip``.

    *reason* is one of :data:`SKIP_REASONS`; for :data:`NOT_BOOTSTRAPPED`,
    *language* is the repo's primary language and *missing_manifests* the
    manifests whose absence leaves it not bootstrapped.
    """

    reason: str
    language: str
    missing_manifests: tuple[str, ...]


@dataclass(frozen=True)
class ValidateRecord:
    """A read-back record. ``passed``, ``not_passed`` and ``skips`` keep the stages' order.

    ``skips`` pairs the name of each stage that carries a ``skip`` with it.
    """

    head: str
    exit_code: int
    full: bool
    clean_at_start: bool
    passed: tuple[str, ...]
    not_passed: tuple[str, ...]
    skips: tuple[tuple[str, StageSkip], ...] = ()


def _stage_entry(result: StageResult, skips: Mapping[str, StageSkip]) -> dict[str, Any]:
    """One stage's entry: its name and status, and its ``skip`` when it was skipped.

    A skip is recorded only on a ``warn`` stage, the status a skipped stage
    ends with: one interrupted before it ran records none.
    """
    entry: dict[str, Any] = {"name": result.name, "status": result.status}
    skip = skips.get(result.name)
    if skip is not None and result.status == "warn":
        entry["skip"] = {
            "reason": skip.reason,
            "language": skip.language,
            "missing_manifests": list(skip.missing_manifests),
        }
    return entry


def write_record(
    repo_root: Path,
    *,
    head: str,
    exit_code: int,
    full: bool,
    clean_at_start: bool,
    results: Sequence[StageResult],
    skips: Mapping[str, StageSkip] | None = None,
) -> Path:
    """Write the record of one run into *repo_root* and return its path.

    *skips* maps the name of each stage the run skipped to why. The file is
    replaced atomically, so a reader never sees half a record.
    """
    path = repo_root / RECORD_PATH
    data = {
        "head": head,
        "exit_code": exit_code,
        "full": full,
        "clean_at_start": clean_at_start,
        "stages": [_stage_entry(r, skips or {}) for r in results],
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
    skips: list[tuple[str, StageSkip]] = []
    for position, stage in enumerate(data["stages"], start=1):
        if (
            not isinstance(stage, dict)
            or set(stage) - {"skip"} != _STAGE_FIELDS
            or not isinstance(stage["name"], str)
            or not isinstance(stage["status"], str)
            or stage["status"] not in _STATUSES
        ):
            msg = f"{bad}: stage {position} must be {{name, status}} with a known status"
            raise ValidateRecordError(msg)
        (passed if stage["status"] in PASSED_STATUSES else not_passed).append(stage["name"])
        if "skip" in stage:
            skips.append((stage["name"], _parse_skip(stage, f"{bad}: stage {position}")))
    return ValidateRecord(
        head=head,
        exit_code=exit_code,
        full=data["full"],
        clean_at_start=data["clean_at_start"],
        passed=tuple(passed),
        not_passed=tuple(not_passed),
        skips=tuple(skips),
    )


def _parse_skip(stage: dict[str, Any], bad: str) -> StageSkip:
    """The ``skip`` of a stage entry, refusing anything not exactly its shape."""
    if stage["status"] != "warn":
        msg = f"{bad} records a skip, but a skipped stage is warn, not {stage['status']}"
        raise ValidateRecordError(msg)
    skip = stage["skip"]
    if (
        not isinstance(skip, dict)
        or set(skip) != _SKIP_FIELDS
        or not isinstance(skip["reason"], str)
        or skip["reason"] not in SKIP_REASONS
        or not isinstance(skip["language"], str)
        or not skip["language"]
        or not isinstance(skip["missing_manifests"], list)
        or not skip["missing_manifests"]
        or not all(isinstance(m, str) and m for m in skip["missing_manifests"])
    ):
        msg = (
            f"{bad}'s skip must be {{reason, language, missing_manifests}}, with a known "
            f"reason ({', '.join(sorted(SKIP_REASONS))}), a language and the missing manifests"
        )
        raise ValidateRecordError(msg)
    return StageSkip(
        reason=skip["reason"],
        language=skip["language"],
        missing_manifests=tuple(skip["missing_manifests"]),
    )
