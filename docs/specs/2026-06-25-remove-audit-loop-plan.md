# Remove the Audit Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the `vrg-pr-workflow` oracle from an interactive dual-agent (USER/AUDIT) loop to a run-and-done PR-metadata recorder, while leaving the audit *identity* infrastructure untouched.

**Architecture:** The oracle's state machine, CLI, transport, and check registry currently implement a turn-taking loop coordinated through `.vergil/pr-workflow.json`. We strip the loop down to two operations — `report-ready` (initialize-and-record PR metadata) and `status` — slim the state schema to match, trim the transport to read/write, delete the registry/settings, and relocate the six judgment-check prompts to `docs/audit-criteria/` as reference material. The audit GitHub App, `IdentityMode.AUDIT`, `vrg-audit-approve`, and `Role.AUDIT` are explicitly *not* touched.

**Tech Stack:** Python 3.12, dataclasses, argparse, pytest. Git/GitHub via `vergil_tooling.lib.git`. Validation runs in a dev container.

## Global Constraints

- **Validation is container-only.** The single full-validation command is
  `vrg-container-run -- vrg-validate` (transparently expands to
  `uv run vrg-validate` here). For targeted test runs during TDD, use
  `vrg-container-run -- uv run pytest <path> -v`. Never run linters/pytest on
  the host.
- **Git/GitHub via wrappers.** Use `vrg-git` (not `git`) and `vrg-commit` (not
  `git commit`). `vrg-commit` signature:
  `vrg-commit --type {feat,fix,docs,refactor,test,chore} --scope SCOPE --message MSG [--body BODY]`.
  Stage with `vrg-git add <paths>` first. `vrg-commit` resolves co-authors
  itself — do not add a `Co-Authored-By` trailer manually.
- **Worktree-local.** All work happens in
  `.worktrees/issue-1872-remove-audit-loop/` on branch
  `feature/1872-remove-audit-loop`. Use absolute paths or `cd` into the
  worktree for Bash.
- **Do NOT touch the audit identity.** Leave `IdentityMode.AUDIT`,
  `vrg_audit_approve.py`, `pr_provenance.py` `Role.AUDIT`, the `vrg-gh` audit
  allowlists, `vrg-whoami` audit mode, and their tests exactly as they are.
- **Python style:** `from __future__ import annotations`; module docstrings;
  ruff-clean; full type hints. Match the surrounding code.

## Release sequencing (NOT part of this plan — context only)

Per the design spec, two cross-system steps gate the *release* of this work but
are out of scope for this plan: (1) the `vergil-claude-plugin` skill update
(release-blocking predecessor — the skills must stop calling `vrg-pr-workflow
next` before this ships, and all repos must pin v2.1 of the plugin); (2)
relaxing the `vergil-audit/approved` branch-protection check to non-required.
This plan implements only the in-repo tooling change.

## File Structure

**Production (modify):**
- `src/vergil_tooling/lib/pr_workflow/state.py` — slim `WorkflowState` to the run-and-done fields; `SCHEMA_VERSION = 2`.
- `src/vergil_tooling/lib/pr_workflow/engine.py` — keep `init_state`, `apply_report_ready`, `apply_submitted`, `_reject_autoclose`; delete all loop functions.
- `src/vergil_tooling/bin/vrg_pr_workflow.py` — `report-ready` (self-initializing, now requires `--issue`) + `status`; delete all other subcommands.
- `src/vergil_tooling/lib/pr_workflow/transport.py` — trim `Transport` ABC to `read`/`write`/`head_sha`/`merge_base`.
- `src/vergil_tooling/lib/pr_workflow/local_transport.py` — drop the wait/heartbeat machinery.
- `src/vergil_tooling/bin/vrg_submit_pr.py` — reword `_print_pr_watch` (drop "BOTH agent sessions").

**Production (delete):**
- `src/vergil_tooling/lib/pr_workflow/registry.py`
- `src/vergil_tooling/lib/pr_workflow/settings.py`
- `src/vergil_tooling/lib/pr_workflow/prompts/` (after relocating the markdown)

**Production (unchanged, verified safe):**
- `src/vergil_tooling/lib/pr_workflow/submission.py` (uses only retained fields/`apply_submitted`)
- `src/vergil_tooling/lib/pr_workflow/batch.py`, `errors.py`
- `src/vergil_tooling/lib/worktrees.py` (`_probe_pr_workflow` reads only `status`/`pr_metadata`/`submitted`)

**New:**
- `docs/audit-criteria/<six>.md` + `docs/audit-criteria/README.md`

**Tests (rewrite):** `test_state.py`, `test_engine_init.py`, `test_engine_reports.py`, `test_cli_e2e.py`, `test_transport_contract.py`, `test_local_transport.py`, `test_submission.py`, `conftest.py`.
**Tests (delete):** `test_engine_directives.py`, `test_cli_orchestration.py`, `test_integration_paired.py`, `test_registry.py`, `test_settings.py`, `test_prompts.py`.
**Tests (update fixtures elsewhere):** `tests/vergil_tooling/test_worktrees.py` (`_write_state` + status strings), `tests/vergil_tooling/test_vrg_submit_pr.py` (any `WorkflowState`/state-file setup).

**Docs:** `CLAUDE.md`, `docs/site/docs/guides/identity-architecture.md`, `docs/site/docs/reference/dev/submit-pr.md`, superseding notes on the three old oracle/workflow specs.

---

### Task 1: Slim the oracle core (state + engine + CLI)

These three change together — the CLI calls the engine, which constructs the
state. They cannot be green independently, so they are one task. After this
task the suite is green: the soon-to-be-deleted modules (`registry`,
`settings`, transport waits) still exist and their own tests still pass; they
are simply no longer referenced by the core.

**Files:**
- Modify: `src/vergil_tooling/lib/pr_workflow/state.py`
- Modify: `src/vergil_tooling/lib/pr_workflow/engine.py`
- Modify: `src/vergil_tooling/bin/vrg_pr_workflow.py`
- Rewrite: `tests/vergil_tooling/pr_workflow/test_state.py`, `test_engine_init.py`, `test_engine_reports.py`, `test_cli_e2e.py`
- Delete: `tests/vergil_tooling/pr_workflow/test_engine_directives.py`, `test_cli_orchestration.py`, `test_integration_paired.py`
- Update fixtures: `tests/vergil_tooling/test_worktrees.py`, `tests/vergil_tooling/test_vrg_submit_pr.py`

**Interfaces:**
- Produces — `state.WorkflowState(issue, branch, base, status, created_at, updated_at, git, pr_metadata=None, submitted=None, schema_version=2)`; `state.SCHEMA_VERSION = 2`; `state.STATUSES = ("implementing", "ready")`.
- Produces — `engine.init_state(*, issue, branch, base, head_sha, base_sha, now) -> WorkflowState`.
- Produces — `engine.apply_report_ready(state, *, title, summary, notes, linkage, head_sha, now) -> WorkflowState` (idempotent; sets `status="ready"`).
- Produces — `engine.apply_submitted(state, *, pr_url, pr_number, now) -> WorkflowState` (unchanged signature; no `history` append).
- Consumes — `submission.py` calls `engine.apply_submitted` and reads `state.pr_metadata`/`state.submitted`/`state.base` (all retained).

- [ ] **Step 1: Rewrite `test_state.py` to the slim schema**

Replace the file contents with tests for the new shape:

```python
"""Tests for the slimmed run-and-done WorkflowState (#1872)."""

from __future__ import annotations

import json

import pytest

from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import SCHEMA_VERSION, WorkflowState


def _state(**overrides: object) -> WorkflowState:
    base = {
        "issue": "42",
        "branch": "feature/42-x",
        "base": "origin/develop",
        "status": "ready",
        "created_at": "2026-06-25T00:00:00Z",
        "updated_at": "2026-06-25T00:00:00Z",
        "git": {"base_sha": "aaa", "head_sha": "bbb"},
    }
    base.update(overrides)
    return WorkflowState(**base)  # type: ignore[arg-type]


def test_round_trips_through_json() -> None:
    state = _state(pr_metadata={"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"})
    restored = WorkflowState.from_json(state.to_json())
    assert restored == state


def test_schema_version_is_two() -> None:
    assert SCHEMA_VERSION == 2
    assert json.loads(_state().to_json())["schema_version"] == 2


def test_unsupported_schema_version_rejected() -> None:
    payload = json.loads(_state().to_json())
    payload["schema_version"] = 1
    with pytest.raises(WorkflowError, match="unsupported schema_version"):
        WorkflowState.from_json(json.dumps(payload))


def test_missing_required_field_rejected() -> None:
    payload = json.loads(_state().to_json())
    del payload["branch"]
    with pytest.raises(WorkflowError, match="missing required field 'branch'"):
        WorkflowState.from_json(json.dumps(payload))


def test_invalid_status_rejected() -> None:
    with pytest.raises(WorkflowError, match="invalid status"):
        WorkflowState.from_json(_state(status="reviewing").to_json())
```

- [ ] **Step 2: Run the state tests; expect failure**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_state.py -v`
Expected: FAIL — current `WorkflowState` still requires `mode`/`owner`/etc., and `SCHEMA_VERSION` is 1.

- [ ] **Step 3: Replace `state.py` with the slim model**

Full new contents:

```python
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
```

- [ ] **Step 4: Run the state tests; expect pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_state.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Delete the three loop-only test files**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-git rm tests/vergil_tooling/pr_workflow/test_engine_directives.py \
           tests/vergil_tooling/pr_workflow/test_cli_orchestration.py \
           tests/vergil_tooling/pr_workflow/test_integration_paired.py
```

- [ ] **Step 6: Rewrite `test_engine_init.py`**

```python
"""Tests for run-and-done init_state (#1872)."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow import engine


def test_init_state_is_run_and_done() -> None:
    state = engine.init_state(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        head_sha="bbb",
        base_sha="aaa",
        now="2026-06-25T00:00:00Z",
    )
    assert state.issue == "42"
    assert state.branch == "feature/42-x"
    assert state.base == "origin/develop"
    assert state.status == "implementing"
    assert state.pr_metadata is None
    assert state.submitted is None
    assert state.git == {"base_sha": "aaa", "head_sha": "bbb"}
```

- [ ] **Step 7: Rewrite `test_engine_reports.py`**

```python
"""Tests for apply_report_ready / apply_submitted run-and-done semantics (#1872)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError


def _fresh() -> object:
    return engine.init_state(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        head_sha="bbb",
        base_sha="aaa",
        now="2026-06-25T00:00:00Z",
    )


def test_report_ready_records_metadata_and_marks_ready() -> None:
    state = _fresh()
    engine.apply_report_ready(
        state,
        title="t",
        summary="s",
        notes="n",
        linkage="Ref",
        head_sha="ccc",
        now="2026-06-25T01:00:00Z",
    )
    assert state.status == "ready"
    assert state.pr_metadata == {"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"}
    assert state.git["head_sha"] == "ccc"
    assert state.updated_at == "2026-06-25T01:00:00Z"


def test_report_ready_is_idempotent_and_overwrites() -> None:
    state = _fresh()
    engine.apply_report_ready(
        state, title="t1", summary="s1", notes="n1", linkage="Ref",
        head_sha="ccc", now="2026-06-25T01:00:00Z",
    )
    engine.apply_report_ready(
        state, title="t2", summary="s2", notes="n2", linkage="Ref",
        head_sha="ddd", now="2026-06-25T02:00:00Z",
    )
    assert state.pr_metadata == {"title": "t2", "summary": "s2", "notes": "n2", "linkage": "Ref"}
    assert state.git["head_sha"] == "ddd"


def test_report_ready_rejects_autoclose_keyword() -> None:
    state = _fresh()
    with pytest.raises(WorkflowError, match="auto-close keyword"):
        engine.apply_report_ready(
            state, title="t", summary="Closes #42", notes="n", linkage="Ref",
            head_sha="ccc", now="2026-06-25T01:00:00Z",
        )


def test_apply_submitted_records_marker() -> None:
    state = _fresh()
    engine.apply_submitted(
        state, pr_url="https://github.com/o/r/pull/7", pr_number=7,
        now="2026-06-25T03:00:00Z",
    )
    assert state.submitted == {
        "pr_url": "https://github.com/o/r/pull/7",
        "pr_number": 7,
        "at": "2026-06-25T03:00:00Z",
    }
```

- [ ] **Step 8: Run the new engine tests; expect failure**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_init.py tests/vergil_tooling/pr_workflow/test_engine_reports.py -v`
Expected: FAIL — `init_state` still requires `mode`/`user_token`; `apply_report_ready` still guards on owner.

- [ ] **Step 9: Replace `engine.py` with the slim state machine**

Full new contents:

```python
"""The transport-agnostic state machine.

Pure functions over a WorkflowState: they mutate the passed state in place and
return it (the oracle loads a fresh state per CLI call, so there is no aliasing
across calls). All wall-clock and git facts are passed in as arguments, keeping
every function deterministic and unit-testable.

Run-and-done since #1872: a worktree initializes, records PR metadata, and is
marked submitted once the human opens the PR. No turn-taking, no audit.
"""

from __future__ import annotations

from vergil_tooling.lib.commit_message import find_autoclose
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _reject_autoclose(verb: str, **fields: str | None) -> None:
    """Reject any PR-metadata field that carries a GitHub auto-close keyword.

    On merge, GitHub auto-closes the linked issue when the PR body contains
    ``Closes/Fixes/Resolves #N`` — violating the fleet policy that an issue
    stays open until its post-merge workflows succeed. The structured
    ``--issue`` already emits ``Ref #N``; the free-text fields must never carry
    an issue-*closing* reference. Rejecting at entry (before state is written)
    keeps the keyword from ever reaching ``.vergil/pr-workflow.json`` or the
    rendered PR body; the submit-time check stays as defense-in-depth."""
    for flag, value in fields.items():
        if value is None:
            continue
        match = find_autoclose(value)
        if match:
            raise WorkflowError(
                f'{verb}: --{flag} contains an auto-close keyword ("{match}"). '
                "Issues must stay open until post-merge workflows succeed; the "
                'structured --issue already emits "Ref #N". '
                'Use "Ref #N" or drop the reference.'
            )


def init_state(
    *,
    issue: str,
    branch: str,
    base: str,
    head_sha: str,
    base_sha: str,
    now: str,
) -> WorkflowState:
    """Create a fresh run-and-done workflow with no PR metadata yet."""
    return WorkflowState(
        issue=str(issue),
        branch=branch,
        base=base,
        status="implementing",
        created_at=now,
        updated_at=now,
        git={"base_sha": base_sha, "head_sha": head_sha},
    )


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
    """Record the PR metadata and mark the workflow ready to submit.

    Idempotent: there is no turn-taking to guard, so re-running overwrites the
    metadata. An agent can correct a mistaken title/summary by calling
    ``report-ready`` again any time before the human submits."""
    _reject_autoclose("report-ready", title=title, summary=summary, notes=notes)
    state.pr_metadata = {"title": title, "summary": summary, "notes": notes, "linkage": linkage}
    state.git["head_sha"] = head_sha
    state.status = "ready"
    state.updated_at = now
    return state


def apply_submitted(
    state: WorkflowState, *, pr_url: str, pr_number: int | None, now: str
) -> WorkflowState:
    """Mark the workflow submitted after ``vrg-submit-pr`` opens the PR.

    The state file is retained (not deleted) so the worktree scanner can report
    the worktree as in-flight rather than re-submitting it."""
    state.submitted = {"pr_url": pr_url, "pr_number": pr_number, "at": now}
    state.updated_at = now
    return state
```

- [ ] **Step 10: Run the engine tests; expect pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_engine_init.py tests/vergil_tooling/pr_workflow/test_engine_reports.py -v`
Expected: PASS.

- [ ] **Step 11: Rewrite `test_cli_e2e.py` to the run-and-done CLI**

Replace the file with a focused end-to-end test of the two surviving
subcommands. This drives `main()` in-process against a real temp git repo.
Reuse whatever git-repo fixture the existing file used (inspect the current
`test_cli_e2e.py` header for its `tmp git repo` helper and keep it); the new
behavioural tests are:

```python
"""End-to-end tests for the run-and-done vrg-pr-workflow CLI (#1872)."""

from __future__ import annotations

import json

from vergil_tooling.bin import vrg_pr_workflow


def test_report_ready_initializes_and_records(in_git_repo, capsys) -> None:
    # in_git_repo: fixture that chdirs into a temp repo on a feature branch with
    # origin/develop reachable (keep the existing fixture from this file).
    rc = vrg_pr_workflow.main(
        ["report-ready", "--issue", "42", "--title", "t", "--summary", "s", "--notes", "n"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "status": "ready"}

    rc = vrg_pr_workflow.main(["status"])
    assert rc == 0
    state = json.loads(capsys.readouterr().out)
    assert state["issue"] == "42"
    assert state["status"] == "ready"
    assert state["pr_metadata"]["title"] == "t"


def test_report_ready_rerun_overwrites(in_git_repo, capsys) -> None:
    vrg_pr_workflow.main(
        ["report-ready", "--issue", "42", "--title", "t1", "--summary", "s", "--notes", "n"]
    )
    capsys.readouterr()
    vrg_pr_workflow.main(
        ["report-ready", "--issue", "42", "--title", "t2", "--summary", "s", "--notes", "n"]
    )
    capsys.readouterr()
    vrg_pr_workflow.main(["status"])
    state = json.loads(capsys.readouterr().out)
    assert state["pr_metadata"]["title"] == "t2"


def test_report_ready_rejects_stale_issue(in_git_repo, capsys) -> None:
    vrg_pr_workflow.main(
        ["report-ready", "--issue", "42", "--title", "t", "--summary", "s", "--notes", "n"]
    )
    capsys.readouterr()
    rc = vrg_pr_workflow.main(
        ["report-ready", "--issue", "99", "--title", "t", "--summary", "s", "--notes", "n"]
    )
    assert rc == 1  # stale workflow file guard


def test_status_with_no_file(in_git_repo, capsys) -> None:
    rc = vrg_pr_workflow.main(["status"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"exists": False}
```

> If the existing `test_cli_e2e.py` fixture is named differently (e.g.
> `tmp_repo`), keep that name and adjust the parameter. Do not invent a new
> fixture; reuse the file's existing git-repo setup.

- [ ] **Step 12: Run the CLI e2e tests; expect failure**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_cli_e2e.py -v`
Expected: FAIL — `report-ready` does not yet accept `--issue` / self-initialize.

- [ ] **Step 13: Replace `vrg_pr_workflow.py` with the run-and-done CLI**

Full new contents:

```python
"""Record PR metadata for the human's submit step: the oracle CLI.

Run-and-done since #1872. The implementing agent calls ``report-ready`` when its
work is green; that writes ``.vergil/pr-workflow.json`` with the PR metadata,
and ``vrg-submit-pr`` (human-run) reads it. ``status`` prints the current state.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from vergil_tooling.lib import git
from vergil_tooling.lib.linkage import normalize_linkage
from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2))


def cmd_report_ready(args: argparse.Namespace, transport: LocalFileTransport) -> int:
    try:
        linkage, linkage_warning = normalize_linkage(args.linkage)
    except ValueError as exc:
        raise WorkflowError(f"report-ready: {exc}") from exc
    state = transport.read()
    if state is None:
        state = engine.init_state(
            issue=args.issue,
            branch=git.current_branch(),
            base=transport.base,
            head_sha=transport.head_sha(),
            base_sha=transport.merge_base(),
            now=_now(),
        )
    elif str(args.issue) != state.issue:
        raise WorkflowError(
            f"stale workflow file for issue #{state.issue}; you passed #{args.issue}. "
            "Delete .vergil/pr-workflow.json to start fresh."
        )
    engine.apply_report_ready(
        state,
        title=args.title,
        summary=args.summary,
        notes=args.notes,
        linkage=linkage,
        head_sha=transport.head_sha(),
        now=_now(),
    )
    transport.write(state)
    response: dict[str, object] = {"ok": True, "status": state.status}
    if linkage_warning:
        response["warning"] = linkage_warning
    _emit(response)
    return 0


def cmd_status(_args: argparse.Namespace, transport: LocalFileTransport) -> int:
    state = transport.read()
    if state is None:
        _emit({"exists": False})
        return 0
    print(state.to_json())
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record PR metadata for the human submit step."
    )
    parser.add_argument("--base", default="origin/develop", help="Base ref for the delta")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ready = sub.add_parser("report-ready", help="Record the PR metadata for this worktree")
    p_ready.add_argument("--issue", required=True)
    p_ready.add_argument("--title", required=True)
    p_ready.add_argument("--summary", required=True)
    p_ready.add_argument("--notes", required=True)
    p_ready.add_argument("--linkage", default="Ref")
    p_ready.set_defaults(func=cmd_report_ready)

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

- [ ] **Step 14: Run the CLI e2e tests; expect pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_cli_e2e.py -v`
Expected: PASS.

- [ ] **Step 15: Fix the out-of-suite `WorkflowState` fixtures**

In `tests/vergil_tooling/test_worktrees.py`, replace the `_write_state` helper
body (it currently passes `mode`/`owner`/`round`/`participants`) with the slim
constructor, and change every `status="approved"` in that file's
`_probe_pr_workflow` tests to `status="ready"` (and the matching assertions):

```python
def _write_state(worktree_root: Path, **overrides: object) -> None:
    """Write a valid pr-workflow.json under *worktree_root* with overrides."""
    state = WorkflowState(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        status="implementing",
        created_at="2026-06-22T00:00:00Z",
        updated_at="2026-06-22T00:00:00Z",
        git={},
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    target = worktree_root / ".vergil"
    target.mkdir(parents=True, exist_ok=True)
    (target / "pr-workflow.json").write_text(state.to_json())
```

Then in the same file change the three `status="approved"` cases and their
expectations to `"ready"` (the run-and-done equivalent of "has metadata,
ready to submit"). Inspect `test_vrg_submit_pr.py` for any direct
`WorkflowState(...)` construction or `.vergil/pr-workflow.json` setup and update
it to the slim schema the same way (use `status="ready"`, drop
`mode`/`owner`/`round`/`participants`).

- [ ] **Step 16: Verify no other test builds the old schema**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop && grep -rn --include="*.py" -e 'mode="solo"' -e 'mode="paired"' -e 'owner="user"' -e 'owner="audit"' -e 'participants=' tests/`
Expected: no matches that construct a `WorkflowState` (argparse/identity hits are fine). Fix any stragglers.

- [ ] **Step 17: Run the full suite; expect green**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS. (`registry.py`, `settings.py`, transport waits, and their tests still exist and pass — they are removed in later tasks.)

- [ ] **Step 18: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-git add src/vergil_tooling/lib/pr_workflow/state.py \
            src/vergil_tooling/lib/pr_workflow/engine.py \
            src/vergil_tooling/bin/vrg_pr_workflow.py \
            tests/vergil_tooling/pr_workflow/test_state.py \
            tests/vergil_tooling/pr_workflow/test_engine_init.py \
            tests/vergil_tooling/pr_workflow/test_engine_reports.py \
            tests/vergil_tooling/pr_workflow/test_cli_e2e.py \
            tests/vergil_tooling/test_worktrees.py \
            tests/vergil_tooling/test_vrg_submit_pr.py
vrg-commit --type refactor --scope pr-workflow \
  --message "collapse the oracle core to run-and-done report-ready" \
  --body "Slim WorkflowState to the run-and-done fields (schema v2), reduce the engine to init_state/apply_report_ready/apply_submitted, and reduce the CLI to a self-initializing report-ready (now takes --issue) plus status. Removes the dual-agent turn-taking. Ref #1872"
```

---

### Task 2: Trim the transport

After Task 1 nothing calls the wait/heartbeat methods, so they can be removed
from both the ABC and the local implementation.

**Files:**
- Modify: `src/vergil_tooling/lib/pr_workflow/transport.py`
- Modify: `src/vergil_tooling/lib/pr_workflow/local_transport.py`
- Rewrite: `tests/vergil_tooling/pr_workflow/test_transport_contract.py`, `test_local_transport.py`
- Modify: `tests/vergil_tooling/pr_workflow/conftest.py`

**Interfaces:**
- Produces — `Transport` ABC with abstract `read`/`write`/`head_sha`/`merge_base` only.
- Produces — `LocalFileTransport(worktree_root, *, base="origin/develop")` (no `poll_interval`).

- [ ] **Step 1: Confirm no caller passes `poll_interval`**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop && grep -rn --include="*.py" 'poll_interval' src/ tests/`
Expected: only `local_transport.py` itself (and its tests, which we rewrite). If `worktrees.py` or `submission.py` pass it, stop and revisit — they should not.

- [ ] **Step 2: Rewrite `test_transport_contract.py` to the trimmed contract**

Keep whatever shared "every Transport implements the contract" structure the
file has, reduced to the four surviving methods. The essential assertions:

```python
"""The Transport ABC contract, trimmed to read/write/git facts (#1872)."""

from __future__ import annotations

import inspect

from vergil_tooling.lib.pr_workflow.transport import Transport


def test_contract_is_read_write_and_git_facts_only() -> None:
    abstract = set(Transport.__abstractmethods__)
    assert abstract == {"read", "write", "head_sha", "merge_base"}
    assert "wait_until_owner" not in dir(Transport)
    assert "wait_until_present" not in dir(Transport)
    # signatures are intact
    assert inspect.isfunction(Transport.read)
```

- [ ] **Step 3: Rewrite `test_local_transport.py`**

Drop every `wait_until_owner`/`wait_until_present` test. Keep read/write
round-trip and the git-fact tests. Reuse the file's existing temp-worktree
fixture. Core assertions:

```python
"""LocalFileTransport read/write round-trip (#1872)."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _state() -> WorkflowState:
    return WorkflowState(
        issue="42", branch="feature/42-x", base="origin/develop", status="ready",
        created_at="2026-06-25T00:00:00Z", updated_at="2026-06-25T00:00:00Z",
        git={"base_sha": "aaa", "head_sha": "bbb"},
    )


def test_read_returns_none_when_absent(tmp_path) -> None:
    assert LocalFileTransport(tmp_path).read() is None


def test_write_then_read_round_trips(tmp_path) -> None:
    transport = LocalFileTransport(tmp_path)
    transport.write(_state())
    assert transport.read() == _state()
```

> Keep any existing git-fact tests (`head_sha`/`merge_base`) that use a real
> temp repo — those methods are unchanged.

- [ ] **Step 4: Run the transport tests; expect failure**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_transport_contract.py tests/vergil_tooling/pr_workflow/test_local_transport.py -v`
Expected: FAIL — the ABC still declares the two wait methods.

- [ ] **Step 5: Trim `transport.py`**

Full new contents:

```python
"""The transport interface.

The CLI orchestrates engine + transport; the engine never touches transport
directly. ``LocalFileTransport`` implements it now; a future ``GitHubTransport``
would implement the same read/write/git-fact contract so the recorder can run
against a remote relay. (The dual-agent polling methods were removed with the
loop in #1872.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


class Transport(ABC):
    """Read/write the workflow state and surface git facts."""

    @abstractmethod
    def read(self) -> WorkflowState | None:
        """Return the current state, or None if no workflow exists yet."""

    @abstractmethod
    def write(self, state: WorkflowState) -> None:
        """Persist the state atomically."""

    @abstractmethod
    def head_sha(self) -> str:
        """Return the current HEAD commit SHA."""

    @abstractmethod
    def merge_base(self) -> str:
        """Return the merge-base SHA of the base ref and HEAD."""
```

- [ ] **Step 6: Trim `local_transport.py`**

Full new contents:

```python
"""The local, file-based transport.

State lives in ``.vergil/pr-workflow.json`` in the worktree. Writes are atomic
(temp + rename, via atomic_write). Git facts come from lib/git, run in the
process CWD (the worktree). (The dual-agent polling/heartbeat waits were removed
with the loop in #1872.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.await_file import atomic_write
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.pr_workflow.transport import Transport

if TYPE_CHECKING:
    from pathlib import Path

_DIR = ".vergil"
_FILE = "pr-workflow.json"


class LocalFileTransport(Transport):
    def __init__(self, worktree_root: Path, *, base: str = "origin/develop") -> None:
        self.worktree_root = worktree_root
        self.base = base

    @property
    def path(self) -> Path:
        return self.worktree_root / _DIR / _FILE

    def read(self) -> WorkflowState | None:
        if not self.path.is_file():
            return None
        return WorkflowState.from_json(self.path.read_text())

    def write(self, state: WorkflowState) -> None:
        atomic_write(self.path, state.to_json())

    def head_sha(self) -> str:
        return git.commit_sha("HEAD")

    def merge_base(self) -> str:
        return git.read_output("merge-base", self.base, "HEAD")
```

- [ ] **Step 7: Simplify `conftest.py`**

The wait-tuning fixture is now moot (the CLI no longer reads
`VRG_PR_WORKFLOW_*`). Replace the file with a minimal placeholder so pytest
still treats the directory as a package with shared config:

```python
"""Shared fixtures for the pr_workflow tests.

The dual-agent wait-tuning fixture was removed with the loop (#1872): the
run-and-done CLI never blocks, so there is nothing to fast-forward.
"""

from __future__ import annotations
```

- [ ] **Step 8: Run the transport tests; expect pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_transport_contract.py tests/vergil_tooling/pr_workflow/test_local_transport.py -v`
Expected: PASS.

- [ ] **Step 9: Full validate + commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/lib/pr_workflow/transport.py \
            src/vergil_tooling/lib/pr_workflow/local_transport.py \
            tests/vergil_tooling/pr_workflow/test_transport_contract.py \
            tests/vergil_tooling/pr_workflow/test_local_transport.py \
            tests/vergil_tooling/pr_workflow/conftest.py
vrg-commit --type refactor --scope pr-workflow \
  --message "trim the transport to read/write/git-fact contract" \
  --body "Drop wait_until_owner/wait_until_present from the Transport ABC and LocalFileTransport, and remove the poll/heartbeat machinery. Keeps the ABC seam for a future GitHubTransport. Ref #1872"
```

---

### Task 3: Remove the registry and settings; relocate the judgment-check prompts

After Tasks 1–2 nothing imports `registry` or `settings`. The six prompt
markdown files become reference criteria under `docs/audit-criteria/`.

**Files:**
- Move: `src/vergil_tooling/lib/pr_workflow/prompts/{six}.md` → `docs/audit-criteria/{six}.md`
- Create: `docs/audit-criteria/README.md`
- Delete: `src/vergil_tooling/lib/pr_workflow/prompts/` (incl. `__init__.py`), `registry.py`, `settings.py`
- Delete: `tests/vergil_tooling/pr_workflow/test_registry.py`, `test_settings.py`, `test_prompts.py`

- [ ] **Step 1: Confirm registry/settings are unreferenced**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop && grep -rn --include="*.py" -e 'pr_workflow.registry' -e 'pr_workflow import registry' -e 'pr_workflow.settings' -e 'pr_workflow import settings' src/`
Expected: no matches.

- [ ] **Step 2: Move the six prompts to `docs/audit-criteria/`**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
mkdir -p docs/audit-criteria
vrg-git mv src/vergil_tooling/lib/pr_workflow/prompts/site-docs-reflection.md docs/audit-criteria/site-docs-reflection.md
vrg-git mv src/vergil_tooling/lib/pr_workflow/prompts/docstring-accuracy.md docs/audit-criteria/docstring-accuracy.md
vrg-git mv src/vergil_tooling/lib/pr_workflow/prompts/pr-description-fidelity.md docs/audit-criteria/pr-description-fidelity.md
vrg-git mv src/vergil_tooling/lib/pr_workflow/prompts/commit-message-fidelity.md docs/audit-criteria/commit-message-fidelity.md
vrg-git mv src/vergil_tooling/lib/pr_workflow/prompts/scope-coherence.md docs/audit-criteria/scope-coherence.md
vrg-git mv src/vergil_tooling/lib/pr_workflow/prompts/test-adequacy.md docs/audit-criteria/test-adequacy.md
```

- [ ] **Step 3: Create `docs/audit-criteria/README.md`**

```markdown
# Audit criteria (reference)

These six markdown files are the judgment criteria a future API-driven agentic
review would apply to a PR:

- `commit-message-fidelity.md`
- `pr-description-fidelity.md`
- `docstring-accuracy.md`
- `site-docs-reflection.md`
- `scope-coherence.md`
- `test-adequacy.md`

They were authored for the interactive dual-agent audit loop, which was removed
in #1872 (see `docs/specs/2026-06-25-remove-audit-loop-design.md`). They are
**reference material only** — no running code reads them today. When the
API-driven review is built, it can reuse them as its prompt set.
```

- [ ] **Step 4: Delete registry, settings, the prompts package, and their tests**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-git rm src/vergil_tooling/lib/pr_workflow/registry.py \
           src/vergil_tooling/lib/pr_workflow/settings.py \
           src/vergil_tooling/lib/pr_workflow/prompts/__init__.py \
           tests/vergil_tooling/pr_workflow/test_registry.py \
           tests/vergil_tooling/pr_workflow/test_settings.py \
           tests/vergil_tooling/pr_workflow/test_prompts.py
```

- [ ] **Step 5: Check pyproject for stale prompts package-data**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop && grep -n 'prompts\|package-data\|package_data\|force-include\|\.md' pyproject.toml`
Expected: no entry that names `pr_workflow/prompts` or `*.md` package data. If
one exists, remove it (the markdown is no longer shipped as package data).

- [ ] **Step 6: Full validate; expect green (watch markdownlint on the moved files)**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS. The moved markdown is now under `docs/` and subject to
markdownlint. If markdownlint flags the relocated files, fix the formatting
(headings, line length, list style) so they pass — they were package data
before and may not have been linted. Do not change their substance.

- [ ] **Step 7: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-git add -A docs/audit-criteria pyproject.toml
vrg-commit --type refactor --scope pr-workflow \
  --message "delete the check registry/settings; relocate prompts to docs/audit-criteria" \
  --body "The judgment-check registry and per-repo settings only served the deleted loop. Move the six judgment prompts to docs/audit-criteria/ as reference criteria for a future API-driven review, with a README explaining their dormant status. Ref #1872"
```

---

### Task 4: Reword the vrg-submit-pr pr-watch handoff line

Drop the dual-agent "paste into BOTH agent sessions" framing. Keep the
`/vergil:pr-watch <url>` command (the plugin's USER-only pr-watch will honor
it). No test asserts the old prose, and the existing tests assert only the
command line, which is retained.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_submit_pr.py` (`_print_pr_watch`)

- [ ] **Step 1: Reword `_print_pr_watch`**

Replace the function with:

```python
def _print_pr_watch(pr_url: str) -> None:
    """Emit the paste-ready post-PR monitoring one-liner.

    Opening the PR auto-triggers the mechanized CI gates; this line starts the
    USER agent's monitoring loop. (The dual-agent framing was removed in #1872.)
    """
    print()
    print("Next — monitor the PR through CI:")
    print()
    print(f"    /vergil:pr-watch {pr_url}")
```

- [ ] **Step 2: Run the submit-pr tests; expect pass (unchanged assertions)**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v`
Expected: PASS — the tests assert `/vergil:pr-watch <url>` is present (or
absent when suppressed); both still hold.

- [ ] **Step 3: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-git add src/vergil_tooling/bin/vrg_submit_pr.py
vrg-commit --type refactor --scope submit-pr \
  --message "drop the dual-agent framing from the pr-watch handoff line" \
  --body "The post-PR one-liner no longer instructs pasting into BOTH agent sessions; pr-watch is becoming USER-only (plugin follow-up). Keeps the /vergil:pr-watch command. Ref #1872"
```

---

### Task 5: Documentation

Update the living docs to the run-and-done reality; mark the old dual-agent
specs superseded. Keep the audit *identity* described as retained-but-dormant.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/site/docs/guides/identity-architecture.md`
- Modify: `docs/site/docs/reference/dev/submit-pr.md`
- Modify (superseding note only): `docs/specs/2026-06-04-vergil-2.1-workflow-design.md`, `docs/specs/2026-06-05-pr-interface-design.md`, `docs/specs/2026-06-08-pr-workflow-oracle-design.md`

- [ ] **Step 1: Update `CLAUDE.md` "Identity modes and PR submission"**

In the numbered handoff list, update item 1 so `report-ready` now takes
`--issue` and drop the dual-agent/oracle framing. Replace the item-1 sentence
with:

```markdown
1. The agent records the PR metadata with `vrg-pr-workflow report-ready
   --issue <N> --title --summary --notes` (optional `--linkage`), which writes
   it to `.vergil/pr-workflow.json`. `title`, `summary`, and `notes` are
   required and non-empty. `linkage` defaults to `Ref` and must stay `Ref`:
   GitHub auto-close keywords (`Closes`/`Fixes`/`Resolves`) are banned repo-wide
   because issues stay open until post-merge workflows succeed, and
   `vrg-submit-pr` rejects any non-`Ref` value before building the PR body.
```

Leave the `VRG_IDENTITY_MODE` paragraph that lists `human`/`user`/`audit`
unchanged — `audit` remains a valid (dormant) identity mode.

- [ ] **Step 2: Update the site docs**

In `docs/site/docs/guides/identity-architecture.md`, keep the audit identity
description but add a note that the local interactive USER/AUDIT loop was
removed in #1872 and the audit identity is retained as dormant infrastructure
for a future API-driven review. In
`docs/site/docs/reference/dev/submit-pr.md`, remove any prose describing the
dual-agent handoff / pasting into both sessions; the handoff is now a single
`report-ready` recording read by the human's `vrg-submit-pr`.

> Inspect both files first and edit only the dual-agent passages. Do not remove
> audit-identity content.

- [ ] **Step 3: Add superseding notes to the three old specs**

At the very top of each of the three spec files (just under the H1 title),
insert:

```markdown
> **Superseded (2026-06-25, #1872):** the interactive dual-agent USER/AUDIT
> loop described here was removed. See
> `docs/specs/2026-06-25-remove-audit-loop-design.md`. The audit *identity*
> infrastructure is retained as dormant; only the loop is gone.
```

Do not rewrite the bodies — these stay as historical record.

- [ ] **Step 4: Full validate (markdownlint on all edited docs)**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS. Fix any markdownlint issues introduced by the edits.

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-git add CLAUDE.md docs/site/docs/guides/identity-architecture.md \
            docs/site/docs/reference/dev/submit-pr.md \
            docs/specs/2026-06-04-vergil-2.1-workflow-design.md \
            docs/specs/2026-06-05-pr-interface-design.md \
            docs/specs/2026-06-08-pr-workflow-oracle-design.md
vrg-commit --type docs --scope audit-removal \
  --message "update docs to run-and-done; mark old dual-agent specs superseded" \
  --body "CLAUDE.md report-ready now documents --issue and drops the oracle/dual-agent framing (audit identity mode retained). Site docs note the local loop is gone. The three old oracle/workflow specs get a superseding banner. Ref #1872"
```

---

### Task 6: Final verification and PR hand-off

- [ ] **Step 1: Full clean validate from a clean tree**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop && vrg-git status && vrg-container-run -- vrg-validate`
Expected: clean working tree, PASS.

- [ ] **Step 2: Grep for any dangling references to removed surface**

Run:
```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
grep -rn --include="*.py" -e 'submit-check' -e 'report-fixes' -e 'audit_ack' -e 'directive_for' -e 'next_pending_check' -e 'wait_until_owner' src/ tests/
```
Expected: no matches. Investigate and remove any stragglers (this excludes the
audit-identity files, which legitimately keep the word "audit").

- [ ] **Step 3: Record the PR hand-off via the (now run-and-done) oracle**

The agent does NOT submit the PR. Record metadata for the human:

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1872-remove-audit-loop
vrg-pr-workflow report-ready --issue 1872 \
  --title "refactor(pr-workflow): remove the interactive dual-agent audit loop" \
  --summary "Collapse vrg-pr-workflow to a run-and-done PR-metadata recorder (report-ready + status), slim the state schema to v2, trim the transport, delete the check registry/settings, and relocate the six judgment prompts to docs/audit-criteria/. The audit identity (App, IdentityMode.AUDIT, vrg-audit-approve, Role.AUDIT) is retained as dormant infrastructure." \
  --notes "Release-sequencing note for the human: the vergil-claude-plugin skill update and the vergil-audit/approved branch-protection relaxation must land per the design spec's Release sequencing section before/with this change."
```

Then stop. The human runs `vrg-submit-pr`.

## Self-Review (completed by plan author)

- **Spec coverage:** §1 oracle collapse → Task 1; §1 transport/ABC trim → Task 2; registry/settings delete + §3 criteria relocation → Task 3; §1 vrg-submit-pr wording → Task 4; §5 docs + superseding notes → Task 5; audit identity "untouched" (§2) → enforced by Global Constraints + Task 6 grep. Merge-gate/plugin sequencing (§4 / Release sequencing) is out-of-repo and noted as context only.
- **Type consistency:** `WorkflowState` slim signature, `init_state`/`apply_report_ready`/`apply_submitted` signatures, and `LocalFileTransport(worktree_root, *, base=...)` are used identically across tasks and tests.
- **Placeholder scan:** production files are given in full; test files give concrete code with explicit "reuse the existing fixture" notes where a temp-git-repo fixture already exists in the file being rewritten.
