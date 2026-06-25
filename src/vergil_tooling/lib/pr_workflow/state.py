"""The PR workflow state model: one JSON document per worktree.

Pure data with validation on the way in. The oracle is the only writer; this
module serializes, deserializes, and checks value invariants. Run-and-done
since #1872: a worktree records its PR metadata and, after the human submits, a
submission marker. The dual-agent coordination fields were removed with the
loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib.pr_workflow.errors import WorkflowError

SCHEMA_VERSION = 2

STATUSES = ("implementing", "ready")

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-faithful dict with a stable key order."""
        return {
            "schema_version": self.schema_version,
            "issue": self.issue,
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
        """Raise ``WorkflowError`` on an out-of-range status."""
        if self.status not in STATUSES:
            raise WorkflowError(f"invalid status {self.status!r}; must be one of {STATUSES}")
