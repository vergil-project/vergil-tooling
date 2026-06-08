# PR Workflow Oracle — Phase 1 (Engine Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fully tested `vrg-pr-workflow` oracle — state model, state-machine engine, a pluggable transport interface, the `LocalFileTransport`, and the CLI — with no agent/skill wiring, driven entirely by tests.

**Architecture:** A transport-agnostic **engine** (pure functions over a `WorkflowState` dataclass: init, handshake, reports, rollup, directives) sits behind a **transport interface**. `LocalFileTransport` serializes state to `.vergil/pr-workflow.json` via the existing `atomic_write` + SHA-256 poll primitives and snapshots git facts via `lib/git.py`. A thin `vrg-pr-workflow` CLI wires verbs (`next`, `report-ready`, `report-fixes`, `submit-review`, `escalate`, `resolve`, `status`) to engine + transport.

**Tech Stack:** Python 3.12+, stdlib only (`json`, `uuid`, `datetime`, `argparse`, `dataclasses`, `abc`), `pytest` + `unittest.mock`. No new dependencies (validation is hand-rolled, matching `pr_template.py` / `config.py`).

**Spec:** `docs/specs/2026-06-08-pr-workflow-oracle-design.md` (this plan implements §12 Phase 1).

**Working directory:** all paths are relative to the worktree root `/.worktrees/issue-1534-pr-workflow-oracle/`. Run all `git` operations as `vrg-git` from inside the worktree. Run validation with `vrg-container-run -- vrg-validate`.

**Deferred to later phases (do NOT implement here):** the six check *prompts* (Phase 2 — this phase ships only the check-ID registry); the `vergil:implement`/`vergil:audit` skill rewrites and `vrg-submit-pr` integration (Phase 3); human-identity enforcement of human-only verbs (Phase 3, where identity wiring lands — Phase 1 selects the actor by CLI flag); `GitHubTransport` (later).

---

## File Structure

**New package** `src/vergil_tooling/lib/pr_workflow/`:

- `__init__.py` — empty package marker.
- `errors.py` — `WorkflowError` exception.
- `state.py` — `WorkflowState` dataclass; value constants; JSON (de)serialization + validation.
- `registry.py` — the canonical check-ID list (`CHECK_IDS`, `check_ids()`). Prompts are Phase 2.
- `engine.py` — pure state-machine functions: `init_state`, `audit_ack`, `apply_report_ready`, `apply_report_fixes`, `apply_review`, `rollup_status`, `apply_escalate`, `apply_resolve`, `directive_for`, plus guards `_require_owner` / `_validate_review`.
- `transport.py` — abstract `Transport` base (the interface both transports honor).
- `local_transport.py` — `LocalFileTransport` (state store on disk + git snapshots).

**New CLI** `src/vergil_tooling/bin/vrg_pr_workflow.py` — verb dispatch.

**Modified** `pyproject.toml` — add the console-script entry.

**New tests** under `tests/vergil_tooling/pr_workflow/`:

- `test_state.py`, `test_registry.py`, `test_engine_init.py`, `test_engine_reports.py`, `test_engine_directives.py`, `test_local_transport.py`, `test_transport_contract.py`, `test_cli_e2e.py`, `test_integration_paired.py`.

Each engine concern is its own module-level function so it can be unit-tested in isolation against a fabricated `WorkflowState`. Files are split by responsibility (state vs registry vs engine vs transport vs CLI), each small enough to hold in context.

---

## Task 1: Package scaffold, `WorkflowError`, and the state model

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/__init__.py`
- Create: `src/vergil_tooling/lib/pr_workflow/errors.py`
- Create: `src/vergil_tooling/lib/pr_workflow/state.py`
- Test: `tests/vergil_tooling/pr_workflow/__init__.py` (empty), `tests/vergil_tooling/pr_workflow/test_state.py`

- [ ] **Step 1: Create the empty package markers**

Create `src/vergil_tooling/lib/pr_workflow/__init__.py` with a single line:

```python
"""The PR workflow oracle: state model, engine, and transports."""
```

Create `tests/vergil_tooling/pr_workflow/__init__.py` empty (zero bytes).

- [ ] **Step 2: Write the failing test for the state model**

Create `tests/vergil_tooling/pr_workflow/test_state.py`:

```python
"""Tests for vergil_tooling.lib.pr_workflow.state."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _minimal() -> WorkflowState:
    return WorkflowState(
        issue="1534",
        branch="feature/1534-x",
        base="origin/develop",
        mode="paired",
        owner="audit",
        status="implementing",
        round=0,
        created_at="2026-06-08T15:00:00Z",
        updated_at="2026-06-08T15:00:00Z",
        participants={"user": {"token": "u-1", "present_at": "2026-06-08T15:00:00Z"}, "audit": None},
        git={"base_sha": "b0", "head_sha": "h0", "last_reviewed_sha": None},
    )


def test_roundtrip_through_json_preserves_fields() -> None:
    state = _minimal()
    restored = WorkflowState.from_json(state.to_json())
    assert restored.to_dict() == state.to_dict()


def test_to_dict_has_stable_top_level_keys() -> None:
    keys = set(_minimal().to_dict())
    assert keys == {
        "schema_version", "issue", "branch", "base", "phase", "mode", "owner",
        "status", "round", "created_at", "updated_at", "participants",
        "pr_metadata", "git", "checks", "escalation", "error", "history",
    }


def test_from_json_rejects_non_json() -> None:
    with pytest.raises(WorkflowError, match="not valid JSON"):
        WorkflowState.from_json("{not json")


def test_from_dict_rejects_missing_required_field() -> None:
    data = _minimal().to_dict()
    del data["owner"]
    with pytest.raises(WorkflowError, match="owner"):
        WorkflowState.from_dict(data)


def test_validate_rejects_bad_owner() -> None:
    state = _minimal()
    state.owner = "nobody"
    with pytest.raises(WorkflowError, match="invalid owner"):
        state.validate()


def test_from_dict_rejects_unknown_schema_version() -> None:
    data = _minimal().to_dict()
    data["schema_version"] = 99
    with pytest.raises(WorkflowError, match="schema_version"):
        WorkflowState.from_dict(data)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_state.py -q`
Expected: FAIL — `ModuleNotFoundError: vergil_tooling.lib.pr_workflow.errors`.

- [ ] **Step 4: Implement `errors.py`**

Create `src/vergil_tooling/lib/pr_workflow/errors.py`:

```python
"""Domain errors for the PR workflow oracle."""

from __future__ import annotations


class WorkflowError(Exception):
    """Raised when workflow state is malformed or a verb is invalid for the state."""
```

- [ ] **Step 5: Implement `state.py`**

Create `src/vergil_tooling/lib/pr_workflow/state.py`:

```python
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
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_state.py -q`
Expected: PASS (6 passed).

- [ ] **Step 7: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1534-pr-workflow-oracle
vrg-git add src/vergil_tooling/lib/pr_workflow/ tests/vergil_tooling/pr_workflow/
vrg-commit --type feat --scope prw --message "add the PR workflow state model and errors" \
  --body "Introduce the pr_workflow package: WorkflowError plus a WorkflowState dataclass with JSON (de)serialization and value-invariant validation. Pure data, no I/O."
```

---

## Task 2: The check-ID registry

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/registry.py`
- Test: `tests/vergil_tooling/pr_workflow/test_registry.py`

- [ ] **Step 1: Write the failing test**

Create `tests/vergil_tooling/pr_workflow/test_registry.py`:

```python
"""Tests for vergil_tooling.lib.pr_workflow.registry."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow import registry


def test_check_ids_are_the_six_seed_checks() -> None:
    assert registry.check_ids() == (
        "site-docs-reflection",
        "docstring-accuracy",
        "pr-description-fidelity",
        "commit-message-fidelity",
        "scope-coherence",
        "test-adequacy",
    )


def test_check_ids_have_no_duplicates() -> None:
    ids = registry.check_ids()
    assert len(ids) == len(set(ids))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: ...registry`.

- [ ] **Step 3: Implement `registry.py`**

Create `src/vergil_tooling/lib/pr_workflow/registry.py`:

```python
"""The judgment-check registry.

Phase 1 holds only the canonical check IDs — the engine uses them to list the
checks in an audit directive and to validate a review payload. The prompts that
define how each check is performed are authored in Phase 2; adding a check is a
one-line edit here plus one prompt, with no engine change.
"""

from __future__ import annotations

CHECK_IDS: tuple[str, ...] = (
    "site-docs-reflection",
    "docstring-accuracy",
    "pr-description-fidelity",
    "commit-message-fidelity",
    "scope-coherence",
    "test-adequacy",
)


def check_ids() -> tuple[str, ...]:
    """Return the canonical, ordered tuple of judgment-check IDs."""
    return CHECK_IDS
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_registry.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/registry.py tests/vergil_tooling/pr_workflow/test_registry.py
vrg-commit --type feat --scope prw --message "add the judgment-check ID registry" \
  --body "Phase 1 registry of the six seed check IDs; prompts are authored in Phase 2."
```

---

## Task 3: Engine — init, handshake, and the ownership guard

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/engine.py`
- Test: `tests/vergil_tooling/pr_workflow/test_engine_init.py`

- [ ] **Step 1: Write the failing test**

Create `tests/vergil_tooling/pr_workflow/test_engine_init.py`:

```python
"""Tests for engine init, handshake, and the ownership guard."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError

_NOW = "2026-06-08T15:00:00Z"


def test_init_paired_assigns_owner_audit_and_records_user_presence() -> None:
    state = engine.init_state(
        issue="1534", branch="feature/1534-x", base="origin/develop",
        mode="paired", head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    assert state.mode == "paired"
    assert state.owner == "audit"
    assert state.status == "implementing"
    assert state.round == 0
    assert state.participants["user"]["token"] == "u-1"
    assert state.participants["audit"] is None
    assert state.git == {"base_sha": "b0", "head_sha": "h0", "last_reviewed_sha": None}
    assert state.history[0]["action"] == "init"
    assert state.history[0]["mode"] == "paired"


def test_init_solo_assigns_owner_user() -> None:
    state = engine.init_state(
        issue="1534", branch="feature/1534-x", base="origin/develop",
        mode="solo", head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    assert state.mode == "solo"
    assert state.owner == "user"


def test_init_rejects_unknown_mode() -> None:
    with pytest.raises(WorkflowError, match="mode"):
        engine.init_state(
            issue="1", branch="b", base="origin/develop", mode="bogus",
            head_sha="h", base_sha="b", user_token="u", now=_NOW,
        )


def test_audit_ack_records_presence_and_flips_owner_to_user() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    engine.audit_ack(state, issue="1534", audit_token="a-1", now="2026-06-08T15:00:05Z")
    assert state.owner == "user"
    assert state.participants["audit"]["token"] == "a-1"
    assert state.history[-1]["action"] == "ack"


def test_audit_ack_rejects_issue_mismatch() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    with pytest.raises(WorkflowError, match="issue mismatch"):
        engine.audit_ack(state, issue="999", audit_token="a-1", now=_NOW)


def test_audit_ack_rejects_solo_workflow() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="solo",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    with pytest.raises(WorkflowError, match="solo"):
        engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_init.py -q`
Expected: FAIL — `ModuleNotFoundError: ...engine`.

- [ ] **Step 3: Implement the init half of `engine.py`**

Create `src/vergil_tooling/lib/pr_workflow/engine.py`:

```python
"""The transport-agnostic state machine.

Pure functions over a WorkflowState: they mutate the passed state in place and
return it (the oracle loads a fresh state per CLI call, so there is no aliasing
across calls). All wall-clock and git facts are passed in as arguments, keeping
every function deterministic and unit-testable.
"""

from __future__ import annotations

from typing import Any

from vergil_tooling.lib.pr_workflow import registry
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import CHECK_STATUSES, MODES, WorkflowState


def init_state(
    *,
    issue: str,
    branch: str,
    base: str,
    mode: str,
    head_sha: str,
    base_sha: str,
    user_token: str,
    now: str,
) -> WorkflowState:
    """Create a fresh workflow. Paired starts owned by AUDIT (handshake
    rendezvous); solo starts owned by USER and skips the handshake."""
    if mode not in MODES:
        raise WorkflowError(f"invalid mode {mode!r}; must be one of {MODES}")
    owner = "user" if mode == "solo" else "audit"
    return WorkflowState(
        issue=str(issue),
        branch=branch,
        base=base,
        mode=mode,
        owner=owner,
        status="implementing",
        round=0,
        created_at=now,
        updated_at=now,
        participants={
            "user": {"token": user_token, "present_at": now},
            "audit": None,
        },
        git={"base_sha": base_sha, "head_sha": head_sha, "last_reviewed_sha": None},
        history=[{"round": 0, "at": now, "actor": "user", "action": "init", "mode": mode}],
    )


def audit_ack(state: WorkflowState, *, issue: str, audit_token: str, now: str) -> WorkflowState:
    """AUDIT confirms presence: it records its token and hands the turn back to
    USER. AUDIT is the current owner here, so this is an owner write."""
    if state.mode == "solo":
        raise WorkflowError("cannot audit a solo (--no-audit) workflow")
    if str(issue) != state.issue:
        msg = (
            f"issue mismatch: workflow file is for #{state.issue}, "
            f"you asked to audit #{issue} — are both sessions in the same worktree?"
        )
        raise WorkflowError(msg)
    state.participants["audit"] = {"token": audit_token, "present_at": now}
    state.owner = "user"
    state.updated_at = now
    state.history.append({"round": state.round, "at": now, "actor": "audit", "action": "ack"})
    return state


def _require_owner(state: WorkflowState, role: str) -> None:
    if state.owner != role:
        msg = f"out-of-turn: {role} cannot write while owner is {state.owner!r}"
        raise WorkflowError(msg)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_init.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/engine.py tests/vergil_tooling/pr_workflow/test_engine_init.py
vrg-commit --type feat --scope prw --message "add engine init, handshake, and ownership guard" \
  --body "init_state (paired -> owner audit, solo -> owner user), audit_ack (records presence, flips to user, rejects issue mismatch and solo), and the _require_owner guard."
```

---

## Task 4: Engine — reports, rollup, escalate, resolve

**Files:**
- Modify: `src/vergil_tooling/lib/pr_workflow/engine.py`
- Test: `tests/vergil_tooling/pr_workflow/test_engine_reports.py`

- [ ] **Step 1: Write the failing test**

Create `tests/vergil_tooling/pr_workflow/test_engine_reports.py`:

```python
"""Tests for engine report/review/rollup/escalate/resolve transitions."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.registry import check_ids

_NOW = "2026-06-08T00:00:00Z"


def _paired_owned_by_user() -> engine.WorkflowState:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)  # owner -> user
    return state


def _all_checks(status: str) -> list[dict]:
    return [{"id": cid, "status": status} for cid in check_ids()]


def test_report_ready_paired_hands_to_audit() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(
        state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
    )
    assert state.owner == "audit"
    assert state.status == "reviewing"
    assert state.pr_metadata == {"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"}
    assert state.git["head_sha"] == "h1"


def test_report_ready_solo_goes_straight_to_approved() -> None:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="solo",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    engine.apply_report_ready(
        state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
    )
    assert state.status == "approved"
    assert state.owner == "user"


def test_report_ready_rejects_out_of_turn() -> None:
    state = _paired_owned_by_user()
    state.owner = "audit"
    with pytest.raises(WorkflowError, match="out-of-turn"):
        engine.apply_report_ready(
            state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
        )


def test_review_all_pass_approves_and_hands_to_user() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    engine.apply_review(state, checks=_all_checks("pass"), head_sha="h1", now=_NOW)
    assert state.status == "approved"
    assert state.owner == "user"
    assert state.git["last_reviewed_sha"] == "h1"


def test_review_with_a_fail_requests_changes() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    assert state.status == "changes-requested"
    assert state.owner == "user"


def test_review_with_an_escalate_goes_to_human() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[1] = {"id": checks[1]["id"], "status": "escalate", "reason": "needs a human"}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    assert state.status == "escalated"
    assert state.owner == "human"
    assert state.escalation["check"] == checks[1]["id"]


def test_review_rejects_unknown_check_id() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass") + [{"id": "made-up", "status": "pass"}]
    with pytest.raises(WorkflowError, match="unknown check"):
        engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)


def test_review_rejects_missing_check() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")[:-1]  # drop one
    with pytest.raises(WorkflowError, match="missing checks"):
        engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)


def test_report_fixes_requires_new_commits() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)  # owner -> user, last_reviewed = h1
    with pytest.raises(WorkflowError, match="no new commits"):
        engine.apply_report_fixes(state, head_sha="h1", note=None, now=_NOW)


def test_report_fixes_bumps_round_and_hands_to_audit() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    engine.apply_report_fixes(state, head_sha="h2", note="addressed", now=_NOW)
    assert state.round == 1
    assert state.owner == "audit"
    assert state.git["head_sha"] == "h2"


def test_report_fixes_escalates_when_round_cap_exceeded() -> None:
    state = _paired_owned_by_user()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = _all_checks("pass")
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 1, "severity": "warning", "note": "fix"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    state.round = 1  # already used the one permitted fix round (max_rounds=1)
    engine.apply_report_fixes(state, head_sha="h2", note=None, now=_NOW, max_rounds=1)
    assert state.round == 2
    assert state.owner == "human"
    assert state.status == "escalated"
    assert "runaway-round cap" in state.escalation["reason"]


def test_apply_error_records_terminal_error() -> None:
    state = _paired_owned_by_user()
    engine.apply_error(state, by="audit", reason="cannot proceed", now=_NOW)
    assert state.status == "error"
    assert state.error == {"by": "audit", "at": _NOW, "reason": "cannot proceed"}
    assert state.history[-1]["action"] == "abort"


def test_escalate_hands_to_human() -> None:
    state = _paired_owned_by_user()
    engine.apply_escalate(state, by="user", reason="stuck", now=_NOW)
    assert state.owner == "human"
    assert state.status == "escalated"
    assert state.escalation["reason"] == "stuck"


def test_resolve_requires_human_owner_and_hands_back() -> None:
    state = _paired_owned_by_user()
    engine.apply_escalate(state, by="user", reason="stuck", now=_NOW)
    engine.apply_resolve(state, to_role="user", note="ok go", now=_NOW)
    assert state.owner == "user"
    assert state.escalation is None


def test_resolve_rejected_when_not_escalated() -> None:
    state = _paired_owned_by_user()
    with pytest.raises(WorkflowError, match="not awaiting the human"):
        engine.apply_resolve(state, to_role="user", note=None, now=_NOW)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_reports.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'apply_report_ready'`.

- [ ] **Step 3: Append the report/review/escalate/resolve functions to `engine.py`**

Add to the end of `src/vergil_tooling/lib/pr_workflow/engine.py`:

```python
def apply_report_ready(
    state: WorkflowState,
    *,
    title: str,
    summary: str,
    notes: str,
    linkage: str,
    head_sha: str,
    now: str,
) -> WorkflowState:
    """USER's initial done-signal. In paired mode it hands the turn to AUDIT; in
    solo mode there is no audit, so it goes straight to approved."""
    _require_owner(state, "user")
    state.pr_metadata = {"title": title, "summary": summary, "notes": notes, "linkage": linkage}
    state.git["head_sha"] = head_sha
    if state.mode == "solo":
        state.status = "approved"
        state.owner = "user"
    else:
        state.status = "reviewing"
        state.owner = "audit"
    state.updated_at = now
    state.history.append(
        {"round": state.round, "at": now, "actor": "user", "action": "report-ready", "head_sha": head_sha}
    )
    return state


def apply_report_fixes(
    state: WorkflowState,
    *,
    head_sha: str,
    note: str | None,
    now: str,
    max_rounds: int = 10,
) -> WorkflowState:
    """USER reports it addressed findings. Requires a genuinely new HEAD so an
    empty round cannot loop. When the new round exceeds ``max_rounds`` the
    workflow auto-escalates to the human instead of looping forever (the
    runaway-round cap, spec §9). ``max_rounds`` is supplied by the CLI from
    ``settings.max_rounds``; the default keeps the engine usable standalone."""
    _require_owner(state, "user")
    if head_sha == state.git.get("last_reviewed_sha"):
        raise WorkflowError("no new commits since the last review; nothing to re-review")
    state.git["head_sha"] = head_sha
    state.round += 1
    state.updated_at = now
    entry: dict[str, Any] = {
        "round": state.round, "at": now, "actor": "user", "action": "report-fixes", "head_sha": head_sha,
    }
    if note:
        entry["note"] = note
    state.history.append(entry)
    if state.round > max_rounds:
        state.owner = "human"
        state.status = "escalated"
        state.escalation = {
            "by": "user",
            "check": None,
            "reason": f"runaway-round cap reached: round {state.round} exceeds max_rounds={max_rounds}",
            "raised_at": now,
        }
        return state
    state.status = "reviewing"
    state.owner = "audit"
    return state


def apply_error(state: WorkflowState, *, by: str, reason: str, now: str) -> WorkflowState:
    """Record a terminal error (a graceful give-up). The counterpart's wait
    detects ``state.error`` and stops with a complementary exception (spec §9)."""
    state.status = "error"
    state.error = {"by": by, "at": now, "reason": reason}
    state.updated_at = now
    state.history.append(
        {"round": state.round, "at": now, "actor": by, "action": "abort", "reason": reason}
    )
    return state


def rollup_status(checks: list[dict[str, Any]]) -> str:
    """Roll a check ledger up to a workflow status: any escalate -> escalated;
    else any fail -> changes-requested; else approved."""
    statuses = [c.get("status") for c in checks]
    if "escalate" in statuses:
        return "escalated"
    if "fail" in statuses:
        return "changes-requested"
    return "approved"


def _validate_review(checks: Any) -> None:
    if not isinstance(checks, list) or not checks:
        raise WorkflowError("review payload must contain a non-empty 'checks' list")
    known = set(registry.check_ids())
    seen: set[str] = set()
    for entry in checks:
        cid = entry.get("id") if isinstance(entry, dict) else None
        if cid not in known:
            raise WorkflowError(f"unknown check id {cid!r}; known checks: {sorted(known)}")
        if entry.get("status") not in CHECK_STATUSES:
            raise WorkflowError(f"check {cid!r} has invalid status {entry.get('status')!r}")
        seen.add(cid)
    missing = known - seen
    if missing:
        raise WorkflowError(f"review is missing checks: {sorted(missing)}")


def apply_review(
    state: WorkflowState, *, checks: list[dict[str, Any]], head_sha: str, now: str
) -> WorkflowState:
    """AUDIT submits its judgments. The oracle validates, stamps the round onto
    each check, records the cursor, and rolls up to the next owner/status."""
    _require_owner(state, "audit")
    _validate_review(checks)
    for entry in checks:
        entry["round"] = state.round
    state.checks = checks
    status = rollup_status(checks)
    state.status = status
    state.git["last_reviewed_sha"] = head_sha
    if status == "escalated":
        state.owner = "human"
        escalated = next(c for c in checks if c.get("status") == "escalate")
        state.escalation = {
            "by": "audit", "check": escalated["id"],
            "reason": escalated.get("reason", ""), "raised_at": now,
        }
    else:
        state.owner = "user"
    state.updated_at = now
    state.history.append(
        {"round": state.round, "at": now, "actor": "audit", "action": "submit-review", "rollup": status}
    )
    return state


def apply_escalate(state: WorkflowState, *, by: str, reason: str, now: str) -> WorkflowState:
    """USER or AUDIT escalates to the human. The escalator must hold the turn."""
    _require_owner(state, by)
    state.owner = "human"
    state.status = "escalated"
    state.escalation = {"by": by, "reason": reason, "raised_at": now}
    state.updated_at = now
    state.history.append(
        {"round": state.round, "at": now, "actor": by, "action": "escalate", "reason": reason}
    )
    return state


def apply_resolve(
    state: WorkflowState, *, to_role: str, note: str | None, now: str
) -> WorkflowState:
    """The human hands control back to an agent after an escalation."""
    if state.owner != "human":
        raise WorkflowError("cannot resolve: the workflow is not awaiting the human")
    if to_role not in ("user", "audit"):
        raise WorkflowError(f"invalid --to {to_role!r}; must be 'user' or 'audit'")
    state.owner = to_role
    state.status = "implementing" if to_role == "user" else "reviewing"
    state.escalation = None
    state.updated_at = now
    entry: dict[str, Any] = {
        "round": state.round, "at": now, "actor": "human", "action": "resolve", "to": to_role,
    }
    if note:
        entry["note"] = note
    state.history.append(entry)
    return state
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_reports.py -q`
Expected: PASS (15 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/engine.py tests/vergil_tooling/pr_workflow/test_engine_reports.py
vrg-commit --type feat --scope prw --message "add engine report, review, rollup, escalate, resolve, and abort" \
  --body "report-ready (paired -> audit, solo -> approved), report-fixes (requires new HEAD, bumps round, auto-escalates on the runaway-round cap), apply_review (validates against the registry, rolls up to owner/status, records the cursor), escalate, resolve, and apply_error (terminal error for graceful give-up / crash propagation)."
```

---

## Task 5: Engine — directives

**Files:**
- Modify: `src/vergil_tooling/lib/pr_workflow/engine.py`
- Test: `tests/vergil_tooling/pr_workflow/test_engine_directives.py`

- [ ] **Step 1: Write the failing test**

Create `tests/vergil_tooling/pr_workflow/test_engine_directives.py`:

```python
"""Tests for engine.directive_for."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.registry import check_ids

_NOW = "2026-06-08T00:00:00Z"


def _user_turn_fresh() -> engine.WorkflowState:
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)
    return state


def test_user_init_directive_names_report_ready() -> None:
    directive = engine.directive_for(_user_turn_fresh(), "user")
    assert directive["then"]["verb"] == "report-ready"
    assert "Implement issue #1534" in directive["do"]


def test_audit_directive_lists_all_checks_and_the_range() -> None:
    state = _user_turn_fresh()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    directive = engine.directive_for(state, "audit")
    assert directive["then"]["verb"] == "submit-review"
    assert directive["checks"] == list(check_ids())
    assert directive["range"] == "origin/develop..h1"


def test_user_changes_directive_carries_findings() -> None:
    state = _user_turn_fresh()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    checks = [{"id": cid, "status": "pass"} for cid in check_ids()]
    checks[0] = {"id": checks[0]["id"], "status": "fail",
                 "findings": [{"file": "x.py", "line": 9, "severity": "warning", "note": "doc it"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    directive = engine.directive_for(state, "user")
    assert directive["then"]["verb"] == "report-fixes"
    assert directive["findings"][0]["check"] == checks[0]["id"]
    assert directive["findings"][0]["note"] == "doc it"


def test_user_approved_directive_is_done() -> None:
    state = _user_turn_fresh()
    engine.apply_report_ready(state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW)
    engine.apply_review(state, checks=[{"id": cid, "status": "pass"} for cid in check_ids()], head_sha="h1", now=_NOW)
    directive = engine.directive_for(state, "user")
    assert directive["done"] is True
    assert directive["reason"] == "approved"


def test_directive_rejects_unknown_role() -> None:
    with pytest.raises(WorkflowError, match="unknown role"):
        engine.directive_for(_user_turn_fresh(), "robot")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_directives.py -q`
Expected: FAIL — `AttributeError: ... 'directive_for'`.

- [ ] **Step 3: Append `directive_for` to `engine.py`**

Add to the end of `src/vergil_tooling/lib/pr_workflow/engine.py`:

```python
def directive_for(state: WorkflowState, role: str) -> dict[str, Any]:
    """Return the single instruction the given role should act on next, or a
    DONE marker. Assumes it is already this role's turn (the transport blocks
    until then)."""
    if role == "user":
        return _user_directive(state)
    if role == "audit":
        since = state.git.get("last_reviewed_sha")
        head = state.git["head_sha"]
        rng = f"{state.base}..{head}"
        focus = since or state.git["base_sha"]
        return {
            "phase": state.phase,
            "role": "audit",
            "round": state.round,
            "do": (
                f"Review the cumulative delta {rng}; focus on commits since {focus}. "
                "Run the judgment checks listed below."
            ),
            "checks": list(registry.check_ids()),
            "range": rng,
            "since": since,
            "then": {"verb": "submit-review", "schema": "review.v1"},
        }
    raise WorkflowError(f"unknown role {role!r}")


def _user_directive(state: WorkflowState) -> dict[str, Any]:
    if state.status == "approved":
        return {"done": True, "reason": "approved", "next_human_action": "run vrg-submit-pr"}
    if state.pr_metadata is None:
        return {
            "phase": state.phase,
            "role": "user",
            "round": state.round,
            "do": (
                f"Implement issue #{state.issue} on branch {state.branch}. "
                "Validate green. Then report PR metadata."
            ),
            "then": {"verb": "report-ready", "schema": "pr-metadata.v1"},
        }
    if state.status == "changes-requested":
        findings: list[dict[str, Any]] = []
        for entry in state.checks:
            if entry.get("status") == "fail":
                for finding in entry.get("findings", []):
                    findings.append({"check": entry["id"], **finding})
        return {
            "phase": state.phase,
            "role": "user",
            "round": state.round,
            "do": "Address these findings, commit fixes, validate green, then report.",
            "findings": findings,
            "then": {"verb": "report-fixes"},
        }
    raise WorkflowError(f"no user directive for status {state.status!r}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_directives.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/engine.py tests/vergil_tooling/pr_workflow/test_engine_directives.py
vrg-commit --type feat --scope prw --message "add engine directive generation" \
  --body "directive_for routes by role and status: user init -> report-ready, user changes -> report-fixes with findings, user approved -> DONE, audit -> submit-review with the check list and base..HEAD range."
```

---

## Task 6: The transport interface

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/transport.py`
- Test: covered by the contract test (Task 8) and `LocalFileTransport` tests (Task 7); no standalone test for the abstract base.

- [ ] **Step 1: Implement `transport.py`**

Create `src/vergil_tooling/lib/pr_workflow/transport.py`:

```python
"""The transport interface.

The engine never touches this directly; the CLI orchestrates engine + transport.
``LocalFileTransport`` implements it now; a future ``GitHubTransport`` will
implement the same contract (enforced by the shared contract test) so the
identical loop can drive a live PR. Turn detection and termination live here,
behind the interface — never in the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from vergil_tooling.lib.pr_workflow.state import WorkflowState


class Transport(ABC):
    """Read/write the workflow state and block until it is a role's turn."""

    @abstractmethod
    def read(self) -> WorkflowState | None:
        """Return the current state, or None if no workflow exists yet."""

    @abstractmethod
    def write(self, state: WorkflowState) -> None:
        """Persist the state atomically."""

    @abstractmethod
    def wait_until_present(self, *, timeout: float) -> WorkflowState:
        """Block until a workflow exists. Raise WorkflowError on timeout."""

    @abstractmethod
    def wait_until_owner(self, role: str, *, timeout: float) -> WorkflowState:
        """Block until ``owner == role``. Raise WorkflowError on timeout, or if
        the counterpart recorded a terminal error."""

    @abstractmethod
    def head_sha(self) -> str:
        """Return the current HEAD commit SHA."""

    @abstractmethod
    def merge_base(self) -> str:
        """Return the merge-base SHA of the base ref and HEAD."""
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `vrg-container-run -- uv run python -c "from vergil_tooling.lib.pr_workflow.transport import Transport; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/transport.py
vrg-commit --type feat --scope prw --message "add the transport interface" \
  --body "Abstract Transport base: read/write/wait_until_present/wait_until_owner/head_sha/merge_base. Turn and termination detection live behind this interface, never in the engine."
```

---

## Task 7: `LocalFileTransport`

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/local_transport.py`
- Test: `tests/vergil_tooling/pr_workflow/test_local_transport.py`

(`head_sha`/`merge_base` are exercised end-to-end against a real repo in Tasks 10–11; these unit tests cover the state-store + waiting behavior with patched time.)

- [ ] **Step 1: Write the failing test**

Create `tests/vergil_tooling/pr_workflow/test_local_transport.py`:

```python
"""Tests for LocalFileTransport (state store + waiting)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.pr_workflow.local_transport"
_NOW = "2026-06-08T00:00:00Z"


def _state(owner: str = "audit"):
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    state.owner = owner
    return state


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    assert LocalFileTransport(tmp_path).read() is None


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path)
    transport.write(_state())
    restored = transport.read()
    assert restored is not None
    assert restored.owner == "audit"
    assert (tmp_path / ".vergil" / "pr-workflow.json").is_file()


def test_wait_until_owner_returns_when_owner_matches(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    transport.write(_state(owner="user"))
    with patch(f"{_MOD}.time.sleep") as slept:
        state = transport.wait_until_owner("user", timeout=5.0)
    assert state.owner == "user"
    slept.assert_not_called()


def test_wait_until_owner_blocks_then_returns(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    transport.write(_state(owner="audit"))

    def flip(_seconds: float) -> None:
        transport.write(_state(owner="user"))

    with patch(f"{_MOD}.time.sleep", side_effect=flip) as slept:
        state = transport.wait_until_owner("user", timeout=5.0)
    assert state.owner == "user"
    slept.assert_called_once()


def test_wait_until_owner_times_out(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    transport.write(_state(owner="audit"))
    # monotonic advances past the deadline on the second reading.
    with patch(f"{_MOD}.time.monotonic", side_effect=[0.0, 0.0, 100.0]), \
         patch(f"{_MOD}.time.sleep"):
        with pytest.raises(WorkflowError, match="timed out"):
            transport.wait_until_owner("user", timeout=5.0)


def test_wait_until_owner_raises_on_counterpart_error(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    state = _state(owner="audit")
    state.error = {"by": "audit", "at": _NOW, "reason": "crashed hard"}
    transport.write(state)
    with patch(f"{_MOD}.time.sleep"):
        with pytest.raises(WorkflowError, match="counterpart reported an error"):
            transport.wait_until_owner("user", timeout=5.0)


def test_wait_until_present_times_out_when_no_file(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    with patch(f"{_MOD}.time.monotonic", side_effect=[0.0, 0.0, 100.0]), \
         patch(f"{_MOD}.time.sleep"):
        with pytest.raises(WorkflowError, match="timed out waiting for the workflow file"):
            transport.wait_until_present(timeout=5.0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_local_transport.py -q`
Expected: FAIL — `ModuleNotFoundError: ...local_transport`.

- [ ] **Step 3: Implement `local_transport.py`**

Create `src/vergil_tooling/lib/pr_workflow/local_transport.py`:

```python
"""The local, file-based transport.

State lives in ``.vergil/pr-workflow.json`` in the shared worktree. Writes are
atomic (temp + rename, via await_file); waits poll by re-reading the file each
interval. Change detection is SHA-256-of-content via re-read — never mtime,
matching await_file's deliberate decision (mtime semantics vary across the host
mount that the two agents share). Git facts come from lib/git, run in the
process CWD (the worktree).
"""

from __future__ import annotations

import time
from pathlib import Path

from vergil_tooling.lib import git
from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

_DIR = ".vergil"
_FILE = "pr-workflow.json"
_POLL_INTERVAL = 1.0


class LocalFileTransport(Transport):
    def __init__(
        self,
        worktree_root: Path,
        *,
        base: str = "origin/develop",
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self.worktree_root = worktree_root
        self.base = base
        self.poll_interval = poll_interval

    @property
    def path(self) -> Path:
        return self.worktree_root / _DIR / _FILE

    def read(self) -> WorkflowState | None:
        if not self.path.is_file():
            return None
        return WorkflowState.from_json(self.path.read_text())

    def write(self, state: WorkflowState) -> None:
        atomic_write(self.path, state.to_json())

    def wait_until_present(self, *, timeout: float) -> WorkflowState:
        deadline = time.monotonic() + timeout
        while True:
            state = self.read()
            if state is not None:
                return state
            if time.monotonic() >= deadline:
                msg = (
                    f"timed out waiting for the workflow file after {timeout}s — "
                    "is the implement session running in this worktree?"
                )
                raise WorkflowError(msg)
            time.sleep(self.poll_interval)

    def wait_until_owner(self, role: str, *, timeout: float) -> WorkflowState:
        deadline = time.monotonic() + timeout
        while True:
            state = self.read()
            if state is not None:
                if state.error is not None:
                    reason = state.error.get("reason", "unknown")
                    raise WorkflowError(f"counterpart reported an error: {reason}")
                if state.owner == role:
                    return state
            if time.monotonic() >= deadline:
                raise WorkflowError(f"timed out after {timeout}s waiting for owner={role!r}")
            time.sleep(self.poll_interval)

    def head_sha(self) -> str:
        return git.commit_sha("HEAD")

    def merge_base(self) -> str:
        return git.read_output("merge-base", self.base, "HEAD")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_local_transport.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/local_transport.py tests/vergil_tooling/pr_workflow/test_local_transport.py
vrg-commit --type feat --scope prw --message "add LocalFileTransport" \
  --body "File-backed transport over .vergil/pr-workflow.json: atomic writes, SHA-of-content change detection via re-read (no mtime), wait_until_present / wait_until_owner with timeouts and counterpart-error detection, and git HEAD/merge-base snapshots."
```

---

## Task 8: The shared transport contract test

**Files:**
- Test: `tests/vergil_tooling/pr_workflow/test_transport_contract.py`

This suite asserts the behavior every transport must honor. It is parametrized over a transport-factory fixture; Phase 1 supplies only `LocalFileTransport`, and a future `GitHubTransport` joins the same `params` list later.

- [ ] **Step 1: Write the contract test**

Create `tests/vergil_tooling/pr_workflow/test_transport_contract.py`:

```python
"""Contract every Transport implementation must satisfy.

Parametrized over a transport factory. Add GitHubTransport to ``_FACTORIES``
when it lands; it must pass this suite unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.transport import Transport

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-06-08T00:00:00Z"

# Each factory takes a tmp_path and returns a Transport whose poll loop will not
# actually sleep (poll_interval 0); time.sleep is patched per test where needed.
_FACTORIES: list[Callable[["Path"], Transport]] = [
    lambda root: LocalFileTransport(root, poll_interval=0.0),
]


def _state(owner: str):
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    state.owner = owner
    return state


@pytest.fixture(params=_FACTORIES)
def transport(request: pytest.FixtureRequest, tmp_path: Path) -> Transport:
    return request.param(tmp_path)


def test_read_is_none_before_any_write(transport: Transport) -> None:
    assert transport.read() is None


def test_write_read_roundtrip(transport: Transport) -> None:
    transport.write(_state("user"))
    restored = transport.read()
    assert restored is not None
    assert restored.owner == "user"
    assert restored.issue == "1534"


def test_wait_until_owner_returns_immediately_when_matching(transport: Transport) -> None:
    transport.write(_state("audit"))
    # No sleep should be needed; patch it so a bug would surface as a call.
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep") as slept:
        state = transport.wait_until_owner("audit", timeout=5.0)
    assert state.owner == "audit"
    slept.assert_not_called()


def test_wait_until_present_returns_existing(transport: Transport) -> None:
    transport.write(_state("user"))
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep") as slept:
        state = transport.wait_until_present(timeout=5.0)
    assert state.issue == "1534"
    slept.assert_not_called()


def test_wait_until_owner_raises_on_error_state(transport: Transport) -> None:
    state = _state("audit")
    state.error = {"by": "audit", "at": _NOW, "reason": "boom"}
    transport.write(state)
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep"):
        with pytest.raises(WorkflowError, match="counterpart reported an error"):
            transport.wait_until_owner("user", timeout=5.0)
```

- [ ] **Step 2: Run the contract test**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_transport_contract.py -q`
Expected: PASS (5 passed).

- [ ] **Step 3: Commit**

```bash
vrg-git add tests/vergil_tooling/pr_workflow/test_transport_contract.py
vrg-commit --type test --scope prw --message "add the shared transport contract test" \
  --body "One parametrized suite that every Transport must satisfy (read/write roundtrip, wait_until_owner/present, error-state propagation). GitHubTransport joins _FACTORIES later and must pass unchanged."
```

---

## Task 8b: The `pr-workflow` settings reader (runaway-round cap)

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/settings.py`
- Test: `tests/vergil_tooling/pr_workflow/test_settings.py`

The CLI reads the configurable runaway-round cap from `vergil.toml` and passes it
into `apply_report_fixes`. A small dedicated reader (the structured `VergilConfig`
dataclass does not model a `[pr-workflow]` stanza) with a graceful default.

- [ ] **Step 1: Write the failing test**

Create `tests/vergil_tooling/pr_workflow/test_settings.py`:

```python
"""Tests for vergil_tooling.lib.pr_workflow.settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.pr_workflow import settings
from vergil_tooling.lib.pr_workflow.errors import WorkflowError

if TYPE_CHECKING:
    from pathlib import Path


def test_default_when_no_vergil_toml(tmp_path: Path) -> None:
    assert settings.max_rounds(tmp_path) == 10


def test_default_when_key_absent(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[project]\nname = 'x'\n")
    assert settings.max_rounds(tmp_path) == 10


def test_reads_configured_cap(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[pr-workflow]\nmax-rounds = 3\n")
    assert settings.max_rounds(tmp_path) == 3


def test_rejects_non_positive_cap(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[pr-workflow]\nmax-rounds = 0\n")
    with pytest.raises(WorkflowError, match="max-rounds"):
        settings.max_rounds(tmp_path)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_settings.py -q`
Expected: FAIL — `ModuleNotFoundError: ...settings`.

- [ ] **Step 3: Implement `settings.py`**

Create `src/vergil_tooling/lib/pr_workflow/settings.py`:

```python
"""Per-repo settings for the PR workflow oracle, read from vergil.toml.

A small dedicated reader: the structured VergilConfig dataclass does not model a
[pr-workflow] stanza, and this keeps the dependency one-way (the oracle reads its
own optional knobs). Falls back to the default when the file or key is absent.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from vergil_tooling.lib.pr_workflow.errors import WorkflowError

_DEFAULT_MAX_ROUNDS = 10


def max_rounds(worktree_root: Path) -> int:
    """Return ``[pr-workflow].max-rounds`` from vergil.toml, or the default (10)."""
    path = worktree_root / "vergil.toml"
    if not path.is_file():
        return _DEFAULT_MAX_ROUNDS
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    value = data.get("pr-workflow", {}).get("max-rounds", _DEFAULT_MAX_ROUNDS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkflowError(f"[pr-workflow].max-rounds must be a positive integer, got {value!r}")
    return value
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_settings.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/settings.py tests/vergil_tooling/pr_workflow/test_settings.py
vrg-commit --type feat --scope prw --message "add the pr-workflow settings reader" \
  --body "settings.max_rounds reads [pr-workflow].max-rounds from vergil.toml with a default of 10; rejects non-positive values. Feeds the engine's runaway-round cap."
```

---

## Task 9: The `vrg-pr-workflow` CLI

**Files:**
- Create: `src/vergil_tooling/bin/vrg_pr_workflow.py`
- Modify: `pyproject.toml` (add the console-script entry)
- Test: driven by the e2e tests in Tasks 10–11.

- [ ] **Step 1: Implement the CLI**

Create `src/vergil_tooling/bin/vrg_pr_workflow.py`:

```python
"""Drive the local pre-PR workflow: the oracle CLI.

Both agent skills reduce to ``vrg-pr-workflow next --as <role>``; the directive
names the report verb to call next. The oracle owns every write, snapshots git
itself, and blocks until it is the caller's turn. See
docs/specs/2026-06-08-pr-workflow-oracle-design.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vergil_tooling.lib import git
from vergil_tooling.lib.pr_workflow import engine, settings
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

# Short for the startup handshake (register or fail); long for steady-state work.
_SHORT_TIMEOUT = 30.0
_LONG_TIMEOUT = 86400.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def _require_state(transport: LocalFileTransport):
    state = transport.read()
    if state is None:
        raise WorkflowError("no workflow file; run `vrg-pr-workflow next --as user` first")
    return state


def cmd_next(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    if args.as_role == "user":
        return _next_user(args, transport)
    return _next_audit(args, transport)


def _next_user(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        if not args.issue:
            raise WorkflowError("the first `next --as user` must pass --issue to initialize")
        mode = "solo" if args.no_audit else "paired"
        state = engine.init_state(
            issue=args.issue,
            branch=git.current_branch(),
            base=transport.base,
            mode=mode,
            head_sha=transport.head_sha(),
            base_sha=transport.merge_base(),
            user_token=_token("u"),
            now=_now(),
        )
        transport.write(state)
        if mode == "paired":
            state = transport.wait_until_owner("user", timeout=_SHORT_TIMEOUT)
    else:
        if args.issue and str(args.issue) != state.issue:
            raise WorkflowError(
                f"stale workflow file for issue #{state.issue}; you passed #{args.issue}. "
                "Delete .vergil/pr-workflow.json to start fresh."
            )
        if state.owner != "user":
            state = transport.wait_until_owner("user", timeout=_LONG_TIMEOUT)
    _emit(engine.directive_for(state, "user"))
    return 0


def _next_audit(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        state = transport.wait_until_present(timeout=_SHORT_TIMEOUT)
    if state.mode == "solo":
        _emit({"done": True, "reason": "solo", "note": "workflow running --no-audit; nothing to do"})
        return 0
    if state.participants.get("audit") is None:
        if not args.issue:
            raise WorkflowError("the first `next --as audit` must pass --issue")
        engine.audit_ack(state, issue=args.issue, audit_token=_token("a"), now=_now())
        transport.write(state)
    state = transport.wait_until_owner("audit", timeout=_LONG_TIMEOUT)
    _emit(engine.directive_for(state, "audit"))
    return 0


def cmd_report_ready(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    engine.apply_report_ready(
        state, title=args.title, summary=args.summary, notes=args.notes,
        linkage=args.linkage, head_sha=transport.head_sha(), now=_now(),
    )
    transport.write(state)
    _emit({"ok": True, "status": state.status, "owner": state.owner})
    return 0


def cmd_report_fixes(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    engine.apply_report_fixes(
        state,
        head_sha=transport.head_sha(),
        note=args.note,
        now=_now(),
        max_rounds=settings.max_rounds(transport.worktree_root),
    )
    transport.write(state)
    _emit({"ok": True, "round": state.round, "owner": state.owner})
    return 0


def cmd_abort(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    engine.apply_error(state, by=args.as_role, reason=args.reason, now=_now())
    transport.write(state)
    _emit({"ok": True, "status": state.status})
    return 0


def cmd_submit_review(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    try:
        payload = json.loads(Path(args.payload).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot read review payload {args.payload!r}: {exc}") from exc
    engine.apply_review(
        state, checks=payload.get("checks"), head_sha=transport.head_sha(), now=_now(),
    )
    transport.write(state)
    _emit({"ok": True, "status": state.status, "owner": state.owner})
    return 0


def cmd_escalate(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    engine.apply_escalate(state, by=args.as_role, reason=args.reason, now=_now())
    transport.write(state)
    _emit({"ok": True, "owner": state.owner})
    return 0


def cmd_resolve(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = _require_state(transport)
    engine.apply_resolve(state, to_role=args.to, note=args.note, now=_now())
    transport.write(state)
    _emit({"ok": True, "owner": state.owner})
    return 0


def cmd_status(_args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        _emit({"exists": False})
        return 0
    print(state.to_json())
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drive the local pre-PR workflow oracle.")
    parser.add_argument("--base", default="origin/develop", help="Base ref for the delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p_next = sub.add_parser("next", help="Block until your turn, then print the next directive")
    p_next.add_argument("--as", dest="as_role", required=True, choices=["user", "audit"])
    p_next.add_argument("--issue", help="Issue number (required on the first call)")
    p_next.add_argument("--no-audit", action="store_true", help="Solo mode: skip the local audit")
    p_next.set_defaults(func=cmd_next)

    p_ready = sub.add_parser("report-ready", help="USER: initial done-signal with PR metadata")
    p_ready.add_argument("--title", required=True)
    p_ready.add_argument("--summary", required=True)
    p_ready.add_argument("--notes", required=True)
    p_ready.add_argument("--linkage", default="Ref")
    p_ready.set_defaults(func=cmd_report_ready)

    p_fixes = sub.add_parser("report-fixes", help="USER: report fixes for the last findings")
    p_fixes.add_argument("--note", default=None)
    p_fixes.set_defaults(func=cmd_report_fixes)

    p_review = sub.add_parser("submit-review", help="AUDIT: submit the judgment ledger")
    p_review.add_argument("--payload", required=True, help="Path to a review.v1 JSON file")
    p_review.set_defaults(func=cmd_submit_review)

    p_esc = sub.add_parser("escalate", help="Hand control to the human")
    p_esc.add_argument("--as", dest="as_role", required=True, choices=["user", "audit"])
    p_esc.add_argument("--reason", required=True)
    p_esc.set_defaults(func=cmd_escalate)

    p_abort = sub.add_parser("abort", help="Record a terminal error (graceful give-up)")
    p_abort.add_argument("--as", dest="as_role", required=True, choices=["user", "audit"])
    p_abort.add_argument("--reason", required=True)
    p_abort.set_defaults(func=cmd_abort)

    p_res = sub.add_parser("resolve", help="HUMAN: hand control back to an agent")
    p_res.add_argument("--to", required=True, choices=["user", "audit"])
    p_res.add_argument("--note", default=None)
    p_res.set_defaults(func=cmd_resolve)

    p_status = sub.add_parser("status", help="Print the current workflow state")
    p_status.set_defaults(func=cmd_status)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transport = LocalFileTransport(git.repo_root(), base=args.base)
    try:
        return int(args.func(args, transport))
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add the console-script entry to `pyproject.toml`**

In `pyproject.toml`, under `[project.scripts]`, add this line in alphabetical position (immediately after the `vrg-pr-await` / `vrg-pr-fix-body` entries):

```toml
vrg-pr-workflow = "vergil_tooling.bin.vrg_pr_workflow:main"
```

- [ ] **Step 3: Verify the CLI module imports and shows help**

Run: `vrg-container-run -- uv run python -m vergil_tooling.bin.vrg_pr_workflow --help`
Expected: argparse help listing the subcommands (`next`, `report-ready`, `report-fixes`, `submit-review`, `escalate`, `abort`, `resolve`, `status`). Exit 0.

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_pr_workflow.py pyproject.toml
vrg-commit --type feat --scope prw --message "add the vrg-pr-workflow CLI" \
  --body "Wire the verbs (next/report-ready/report-fixes/submit-review/escalate/abort/resolve/status) to the engine and LocalFileTransport, with the startup handshake, solo short-circuit, the short/long timeout regimes, the USER-side stale-file refusal, and the runaway-round cap fed from settings.max_rounds. Register the console script."
```

---

## Task 10: CLI end-to-end (solo path + guards)

**Files:**
- Test: `tests/vergil_tooling/pr_workflow/test_cli_e2e.py`

Drives the installed CLI as a subprocess against a real temporary git repo. The solo path involves no blocking, so it exercises the whole stack (CLI → engine → transport → git) deterministically.

- [ ] **Step 1: Write the e2e test**

Create `tests/vergil_tooling/pr_workflow/test_cli_e2e.py`:

```python
"""End-to-end tests driving the vrg-pr-workflow CLI as a subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

_CLI = ("python", "-m", "vergil_tooling.bin.vrg_pr_workflow")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base commit")
    _git(repo, "checkout", "-b", "feature/1534-x")
    (repo / "feature.py").write_text("x = 1\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-m", "feature work")
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "vergil_tooling.bin.vrg_pr_workflow", "--base", "develop", *args),
        cwd=repo, capture_output=True, text=True,
    )


def test_solo_happy_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    out = _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    assert out.returncode == 0, out.stderr
    directive = json.loads(out.stdout)
    assert directive["then"]["verb"] == "report-ready"

    out = _run(repo, "report-ready", "--title", "feat: x", "--summary", "did x", "--notes", "n")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["status"] == "approved"

    out = _run(repo, "next", "--as", "user")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "done": True, "reason": "approved", "next_human_action": "run vrg-submit-pr",
    }

    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["mode"] == "solo"
    assert state["history"][0]["action"] == "init"


def test_audit_on_solo_file_exits_clean(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "next", "--as", "audit", "--issue", "1534")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["reason"] == "solo"


def test_first_user_next_without_issue_errors(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    out = _run(repo, "next", "--as", "user")
    assert out.returncode == 1
    assert "must pass --issue" in out.stderr


def test_submit_review_with_bad_payload_errors(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "submit-review", "--payload", str(repo / "missing.json"))
    assert out.returncode == 1
    assert "review payload" in out.stderr
```

- [ ] **Step 2: Run the e2e test**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_cli_e2e.py -q`
Expected: PASS (4 passed).

- [ ] **Step 3: Commit**

```bash
vrg-git add tests/vergil_tooling/pr_workflow/test_cli_e2e.py
vrg-commit --type test --scope prw --message "add CLI end-to-end tests for the solo path and guards" \
  --body "Drive vrg-pr-workflow as a subprocess against a real temp git repo: the solo happy path (init -> report-ready -> approved DONE), audit-on-solo clean exit, missing-issue init guard, and bad review payload error."
```

---

## Task 11: Paired-flow integration (engine + transport, deterministic)

**Files:**
- Test: `tests/vergil_tooling/pr_workflow/test_integration_paired.py`

The paired CLI flow involves blocking waits, which are flaky to drive in a single process. This test exercises the full paired loop deterministically by stepping the **engine + `LocalFileTransport`** directly (the same code the CLI calls between waits), asserting the ledger, ownership flips, and history through a changes round and an approval.

- [ ] **Step 1: Write the integration test**

Create `tests/vergil_tooling/pr_workflow/test_integration_paired.py`:

```python
"""Deterministic integration of the paired loop over engine + LocalFileTransport.

Drives both roles' state transitions directly (no blocking waits), proving the
full handshake -> changes -> fixes -> approve cycle and the recorded history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.registry import check_ids

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-06-08T00:00:00Z"


def _all(status: str) -> list[dict]:
    return [{"id": cid, "status": status} for cid in check_ids()]


def test_paired_full_cycle(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)

    # USER init (paired) -> owner audit.
    state = engine.init_state(
        issue="1534", branch="feature/1534-x", base="develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    transport.write(state)

    # AUDIT acks -> owner user.
    state = transport.read()
    engine.audit_ack(state, issue="1534", audit_token="a-1", now=_NOW)
    transport.write(state)
    assert transport.read().owner == "user"

    # USER reports ready -> owner audit.
    state = transport.read()
    engine.apply_report_ready(
        state, title="t", summary="s", notes="n", linkage="Ref", head_sha="h1", now=_NOW,
    )
    transport.write(state)

    # AUDIT review with one failure -> changes-requested, owner user.
    state = transport.read()
    checks = _all("pass")
    checks[3] = {"id": checks[3]["id"], "status": "fail",
                 "findings": [{"file": "feature.py", "line": 1, "severity": "warning",
                               "note": "commit message overstates the change"}]}
    engine.apply_review(state, checks=checks, head_sha="h1", now=_NOW)
    transport.write(state)
    assert transport.read().status == "changes-requested"

    # USER fixes -> round 1, owner audit.
    state = transport.read()
    engine.apply_report_fixes(state, head_sha="h2", note="reworded commit", now=_NOW)
    transport.write(state)
    assert transport.read().round == 1

    # AUDIT re-review, all pass -> approved, owner user.
    state = transport.read()
    engine.apply_review(state, checks=_all("pass"), head_sha="h2", now=_NOW)
    transport.write(state)

    final = transport.read()
    assert final.status == "approved"
    assert final.owner == "user"
    assert engine.directive_for(final, "user")["done"] is True

    actions = [h["action"] for h in final.history]
    assert actions == ["init", "ack", "report-ready", "submit-review", "report-fixes", "submit-review"]
```

- [ ] **Step 2: Run the integration test**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_integration_paired.py -q`
Expected: PASS (1 passed).

- [ ] **Step 3: Commit**

```bash
vrg-git add tests/vergil_tooling/pr_workflow/test_integration_paired.py
vrg-commit --type test --scope prw --message "add deterministic paired-flow integration test" \
  --body "Step the full handshake -> changes -> fixes -> approve cycle through engine + LocalFileTransport, asserting ownership flips, the round counter, the final approval, and the recorded history sequence."
```

---

## Task 11b: CLI handshake orchestration, stale-file, and abort coverage

**Files:**
- Create: `tests/vergil_tooling/pr_workflow/test_cli_orchestration.py`
- Modify: `tests/vergil_tooling/pr_workflow/test_cli_e2e.py`

The solo CLI path is subprocess-tested (Task 10); the *paired* `_next_user` /
`_next_audit` ack-and-wait glue is not. This task covers it deterministically with
a fake transport whose wait methods return immediately (no threads, no blocking),
plus subprocess coverage for the USER stale-file refusal and the `abort` writer.

- [ ] **Step 1: Write the failing orchestration test**

Create `tests/vergil_tooling/pr_workflow/test_cli_orchestration.py`:

```python
"""Deterministic tests for the paired CLI handshake glue (no real blocking)."""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from vergil_tooling.bin import vrg_pr_workflow as cli
from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

if TYPE_CHECKING:
    import pytest

_NOW = "2026-06-08T00:00:00Z"


class FakeTransport(Transport):
    """In-memory transport whose waits resolve immediately by flipping owner."""

    def __init__(self) -> None:
        self.state: WorkflowState | None = None
        self.writes: list[WorkflowState] = []
        self.base = "origin/develop"
        self.worktree_root = None  # settings.max_rounds is not exercised here

    def read(self) -> WorkflowState | None:
        return self.state

    def write(self, state: WorkflowState) -> None:
        # Independent copies: the writes log is a historical record that later
        # in-place mutations of self.state must not retroactively alter.
        self.state = WorkflowState.from_json(state.to_json())
        self.writes.append(WorkflowState.from_json(state.to_json()))

    def wait_until_present(self, *, timeout: float) -> WorkflowState:
        assert self.state is not None
        return self.state

    def wait_until_owner(self, role: str, *, timeout: float) -> WorkflowState:
        assert self.state is not None
        self.state.owner = role  # simulate the counterpart handing the turn over
        return self.state

    def head_sha(self) -> str:
        return "h0"

    def merge_base(self) -> str:
        return "b0"


def _args(**kw: object) -> argparse.Namespace:
    ns = argparse.Namespace(issue=None, no_audit=False)
    ns.__dict__.update(kw)
    return ns


def test_next_user_init_paired_writes_audit_then_waits(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(cli.git, "current_branch", lambda: "feature/1534-x")
    transport = FakeTransport()
    rc = cli._next_user(_args(as_role="user", issue="1534", no_audit=False), transport)
    assert rc == 0
    assert transport.writes[0].owner == "audit"  # init handed to audit for the handshake
    directive = json.loads(capsys.readouterr().out)
    assert directive["then"]["verb"] == "report-ready"  # wait flipped back to user


def test_next_audit_first_call_acks_and_returns_review_directive(capsys) -> None:
    transport = FakeTransport()
    transport.state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    rc = cli._next_audit(_args(as_role="audit", issue="1534"), transport)
    assert rc == 0
    assert any(w.participants.get("audit") for w in transport.writes)  # ack recorded
    directive = json.loads(capsys.readouterr().out)
    assert directive["then"]["verb"] == "submit-review"


def test_next_audit_solo_exits_clean(capsys) -> None:
    transport = FakeTransport()
    transport.state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="solo",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    rc = cli._next_audit(_args(as_role="audit", issue="1534"), transport)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "solo"
```

- [ ] **Step 2: Run it to verify it fails, then passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_cli_orchestration.py -q`
Expected: PASS (3 passed) once Task 9 is implemented — this test exercises the existing CLI functions, so it should pass directly against the Task 9 code. If it fails, the failure pinpoints a handshake-glue bug to fix in `vrg_pr_workflow.py` before proceeding.

- [ ] **Step 3: Add stale-file and abort subprocess tests**

Append to `tests/vergil_tooling/pr_workflow/test_cli_e2e.py`:

```python
def test_user_next_rejects_stale_different_issue(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "next", "--as", "user", "--issue", "9999")
    assert out.returncode == 1
    assert "stale workflow file" in out.stderr


def test_abort_records_terminal_error(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _run(repo, "next", "--as", "user", "--issue", "1534", "--no-audit")
    out = _run(repo, "abort", "--as", "user", "--reason", "giving up")
    assert out.returncode == 0
    state = json.loads((repo / ".vergil" / "pr-workflow.json").read_text())
    assert state["status"] == "error"
    assert state["error"]["reason"] == "giving up"
```

- [ ] **Step 4: Run the e2e suite**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_cli_e2e.py tests/vergil_tooling/pr_workflow/test_cli_orchestration.py -q`
Expected: PASS (9 passed — 4 original e2e + 2 new e2e + 3 orchestration).

- [ ] **Step 5: Commit**

```bash
vrg-git add tests/vergil_tooling/pr_workflow/test_cli_orchestration.py tests/vergil_tooling/pr_workflow/test_cli_e2e.py
vrg-commit --type test --scope prw --message "cover the paired CLI handshake, stale-file refusal, and abort" \
  --body "Deterministic orchestration tests for _next_user/_next_audit via a fake transport (no blocking), plus subprocess tests for the USER stale-different-issue refusal and the abort terminal-error writer."
```

---

## Task 12: Full validation and Phase 1 wrap

**Files:** none (verification only).

- [ ] **Step 1: Run the entire validation pipeline**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — lint (ruff), typecheck (mypy/ty), the full pytest suite (including every `tests/vergil_tooling/pr_workflow/` file), audit, and common checks all green.

- [ ] **Step 2: Fix any lint/type findings surfaced by `vrg-validate`**

If ruff or mypy flags anything in the new files, fix it in place and re-run `vrg-container-run -- vrg-validate` until green. Typical items: an unused import, a missing return type, or a line-length wrap. Do not suppress — fix the underlying issue.

- [ ] **Step 3: Commit any fixes**

```bash
vrg-git add -A
vrg-commit --type fix --scope prw --message "satisfy lint and typecheck for the pr_workflow package" \
  --body "Address ruff/mypy findings surfaced by vrg-validate across the new pr_workflow modules and tests."
```

(Skip this commit if Step 1 was already green.)

- [ ] **Step 4: Confirm the Phase 1 deliverable**

Phase 1 is complete when `vrg-container-run -- vrg-validate` is green and `vrg-pr-workflow --help` runs. The mechanism is fully testable with no skill wiring. Hand off to the **Phase 2** plan (judgment-check prompts) next.

---

## Self-Review

**Spec coverage (§12 Phase 1):**

- State schema (§4) → Task 1 (`state.py`, all top-level fields incl. `participants`, `mode`, `error`).
- Engine — state machine, rollup, directives, handshake, `--no-audit` (§8) → Tasks 3–5 (init/ack/guard, reports/rollup/escalate/resolve, directives) and the CLI's handshake/solo wiring in Task 9.
- Transport interface (§3.2) → Task 6.
- `LocalFileTransport` with SHA-256 polling, no mtime (§9) → Task 7.
- Shared transport contract test (§10) → Task 8.
- End-to-end subprocess test (§10) → Tasks 10–11 (solo via subprocess; paired via deterministic integration — rationale documented in Task 11) + the paired CLI handshake glue via a fake transport (Task 11b).
- Ownership invariant + bootstrap/ack exceptions (§8.3) → `_require_owner` (Task 3) + tests (Task 4).
- Two timeout regimes (§9) → CLI `_SHORT_TIMEOUT`/`_LONG_TIMEOUT` (Task 9), orchestration-tested (Task 11b).
- Crash propagation (§9) — both halves: writer `engine.apply_error` + `abort` verb (Tasks 4, 9) and detection via `wait_until_owner` error-state propagation (Tasks 7–8); abort e2e (Task 11b).
- Runaway-round cap (§9) → `settings.max_rounds` from `vergil.toml` (Task 8b) feeding `apply_report_fixes`' cap branch (Task 4), wired in the CLI (Task 9).
- USER-side stale-file refusal (§9) → `_next_user` issue check (Task 9), e2e-tested (Task 11b).
- `--no-audit` solo with recorded skip and clean audit exit (§6.2) → init `mode: solo` history entry (Task 3), solo `report-ready → approved` (Task 4), audit-on-solo exit (Tasks 9–10).
- Judgment-only registry of six check IDs (§5) → Task 2.

Deliberately deferred (noted in the header, consistent with §12): check prompts (Phase 2); skill rewrites + `vrg-submit-pr` integration (Phase 3); human-identity *enforcement* of human-only verbs and the identity-misread warning (Phase 3 — Phase 1 selects the actor by flag); `GitHubTransport`.

**Placeholder scan:** No TBD/TODO; every code step contains complete, runnable code; every test step contains real assertions.

**Type/name consistency:** `WorkflowState` (`to_json`/`from_json`/`to_dict`/`from_dict`/`validate`); engine `init_state`, `audit_ack`, `apply_report_ready`, `apply_report_fixes` (with `max_rounds`), `apply_error`, `rollup_status`, `apply_review`, `apply_escalate`, `apply_resolve`, `directive_for`, `_require_owner`, `_validate_review`; `registry.check_ids`; `settings.max_rounds`; `Transport`/`LocalFileTransport` `read`/`write`/`wait_until_present`/`wait_until_owner`/`head_sha`/`merge_base`; CLI verbs `next`/`report-ready`/`report-fixes`/`submit-review`/`escalate`/`abort`/`resolve`/`status` mapping to `cmd_*`/`_next_*` — all referenced names match their definitions across tasks. `pyproject` entry is consistent.
