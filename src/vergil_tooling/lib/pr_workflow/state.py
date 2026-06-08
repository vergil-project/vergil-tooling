"""The PR workflow state model: one JSON document per worktree.

Pure data with validation on the way in. The oracle is the only writer; this
module just serializes, deserializes, and checks the value invariants. Nested
structures (participants, git, checks, history) are plain dicts/lists kept
JSON-faithful; only the top level is a dataclass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from vergil_tooling.lib.pr_workflow.errors import WorkflowError

SCHEMA_VERSION = 1

MODES = ("paired", "solo")
OWNERS = ("user", "audit", "human")
STATUSES = (
    "implementing",
    "reviewing",
    "changes-requested",
    "approved",
    "escalated",
    "error",
)
CHECK_STATUSES = ("pass", "fail", "escalate")

_REQUIRED = (
    "issue", "branch", "base", "mode", "owner", "status", "round",
    "created_at", "updated_at", "participants", "git",
)


@dataclass
class WorkflowState:
    """The single source of truth for one local pre-PR workflow."""

    issue: str
    branch: str
    base: str
    mode: str
    owner: str
    status: str
    round: int
    created_at: str
    updated_at: str
    participants: dict[str, Any]
    git: dict[str, Any]
    pr_metadata: dict[str, str] | None = None
    escalation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    phase: str = "local"
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-faithful dict with a stable key order."""
        return {
            "schema_version": self.schema_version,
            "issue": self.issue,
            "branch": self.branch,
            "base": self.base,
            "phase": self.phase,
            "mode": self.mode,
            "owner": self.owner,
            "status": self.status,
            "round": self.round,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "participants": self.participants,
            "pr_metadata": self.pr_metadata,
            "git": self.git,
            "checks": self.checks,
            "escalation": self.escalation,
            "error": self.error,
            "history": self.history,
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
            mode=data["mode"],
            owner=data["owner"],
            status=data["status"],
            round=int(data["round"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            participants=data["participants"],
            git=data["git"],
            pr_metadata=data.get("pr_metadata"),
            escalation=data.get("escalation"),
            error=data.get("error"),
            checks=data.get("checks", []),
            history=data.get("history", []),
            phase=data.get("phase", "local"),
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
        """Raise ``WorkflowError`` on any out-of-range enum value."""
        if self.mode not in MODES:
            raise WorkflowError(f"invalid mode {self.mode!r}; must be one of {MODES}")
        if self.owner not in OWNERS:
            raise WorkflowError(f"invalid owner {self.owner!r}; must be one of {OWNERS}")
        if self.status not in STATUSES:
            raise WorkflowError(f"invalid status {self.status!r}; must be one of {STATUSES}")
