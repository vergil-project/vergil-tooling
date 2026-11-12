"""The PR workflow state model: one JSON document per worktree.

Pure data with validation on the way in. The oracle is the only writer; this
module serializes, deserializes, and checks value invariants. Run-and-done
since #1872: a worktree records its PR metadata and, after the human submits, a
submission marker. The dual-agent coordination fields were removed with the
loop.

A batch PR (epic vergil-project/.github#1019) records its ordered issue list in
the optional ``issues`` field, still at schema version 2: ``issue`` is its first
issue, and a single-issue record carries no ``issues`` key, so it reads and
writes exactly as before. Read ``all_issues`` for the list either way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib.pr_workflow.errors import WorkflowError

SCHEMA_VERSION = 2

STATUSES = ("implementing", "ready")

# The fewest issues an ``issues`` list holds: one issue is a single-issue record.
_MIN_ISSUES = 2

_REQUIRED = (
    "issue",
    "branch",
    "base",
    "status",
    "created_at",
    "updated_at",
    "git",
)


@dataclass
class WorkflowState:
    """The single source of truth for one local pre-PR workflow."""

    issue: str
    branch: str
    base: str
    status: str
    created_at: str
    updated_at: str
    git: dict[str, Any]
    pr_metadata: dict[str, str] | None = None
    submitted: dict[str, Any] | None = None
    schema_version: int = SCHEMA_VERSION
    # A batch's issues in order (epic vergil-project/.github#1019); ``issue`` is
    # always the first of them. ``None`` for a single-issue record, whose JSON
    # carries no ``issues`` key, so it is byte-for-byte what it was before.
    issues: list[str] | None = None

    @property
    def all_issues(self) -> list[str]:
        """Every issue this workflow hands off, in order: ``issues`` or ``[issue]``."""
        return list(self.issues) if self.issues is not None else [self.issue]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-faithful dict with a stable key order.

        A batch's ``issues`` follows ``issue``; a single-issue record has no
        ``issues`` key at all.
        """
        batch = {"issues": list(self.issues)} if self.issues is not None else {}
        return {
            "schema_version": self.schema_version,
            "issue": self.issue,
            **batch,
            "branch": self.branch,
            "base": self.base,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "git": self.git,
            "pr_metadata": self.pr_metadata,
            "submitted": self.submitted,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowState:
        version = data.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            msg = f"unsupported schema_version {version!r}; expected {SCHEMA_VERSION}"
            raise WorkflowError(msg)
        for key in _REQUIRED:
            if key not in data:
                msg = f"workflow state is missing required field '{key}'"
                raise WorkflowError(msg)
        state = cls(
            issue=str(data["issue"]),
            branch=data["branch"],
            base=data["base"],
            status=data["status"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            git=data["git"],
            pr_metadata=data.get("pr_metadata"),
            submitted=data.get("submitted"),
            schema_version=version,
            issues=data.get("issues"),
        )
        state.validate()
        return state

    @classmethod
    def from_json(cls, text: str) -> WorkflowState:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            msg = f"workflow state is not valid JSON: {exc}"
            raise WorkflowError(msg) from exc
        return cls.from_dict(data)

    def validate(self) -> None:
        """Raise ``WorkflowError`` on an out-of-range status or a malformed issue list."""
        if self.status not in STATUSES:
            raise WorkflowError(f"invalid status {self.status!r}; must be one of {STATUSES}")
        self._validate_issues()

    def _validate_issues(self) -> None:
        """A batch's ``issues``: a list of 2 or more refs whose first is ``issue``."""
        if self.issues is None:
            return
        if not isinstance(self.issues, list):
            raise WorkflowError(
                f"workflow state 'issues' must be a list, got {type(self.issues).__name__}"
            )
        if len(self.issues) < _MIN_ISSUES:
            raise WorkflowError(
                f"workflow state 'issues' lists a batch of at least {_MIN_ISSUES} issues, "
                f"got {self.issues!r}; a single-issue record carries no 'issues'"
            )
        for entry in self.issues:
            if not isinstance(entry, str) or not entry:
                raise WorkflowError(
                    f"workflow state 'issues' entries must each be a non-empty string, "
                    f"got {entry!r}"
                )
        if self.issues[0] != self.issue:
            raise WorkflowError(
                f"workflow state 'issues' first entry {self.issues[0]!r} is not its "
                f"'issue' {self.issue!r}"
            )
