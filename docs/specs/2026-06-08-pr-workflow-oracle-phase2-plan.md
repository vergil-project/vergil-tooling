# PR Workflow Oracle — Phase 2 (Judgment Registry) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Phase 1 check-ID registry into real audits — author the six judgment-check prompts as package data, load them via `importlib.resources`, and inline the current check's prompt into the per-check audit directive.

**Architecture:** Each check is a markdown prompt under `src/vergil_tooling/lib/pr_workflow/prompts/<check-id>.md`, authored so its output maps 1:1 to a `check.v1` payload. `registry.check_prompt(id)` reads it via `importlib.resources` (robust when pip/uv-installed). The engine stays pure (no I/O); the **CLI** enriches the audit directive with the prompt text (`directive["prompt"] = registry.check_prompt(directive["check"])`) — one prompt per round-trip, matching the per-check loop.

**Tech Stack:** Python 3.12+, stdlib only (`importlib.resources`), `pytest`. No new dependencies.

**Spec:** `docs/specs/2026-06-08-pr-workflow-oracle-design.md` (§5 registry, §6–§8 per-check loop). This plan implements §12 Phase 2.

**Working directory:** all paths are relative to the worktree root `.worktrees/issue-1534-pr-workflow-oracle/`. Run git via `vrg-git` from inside the worktree; validate with `vrg-container-run -- vrg-validate`.

**Builds on:** Phase 1 (per-check engine: `next_pending_check`, `apply_check`; CLI `submit-check`; directive carries `check`). **Deferred (do NOT implement here):** the `vergil:implement`/`vergil:audit` skill rewrites + `vrg-submit-pr` integration + human-identity enforcement (Phase 3); eval-style judgment-quality testing of the prompts (later); `GitHubTransport` (later).

---

## File Structure

**New** `src/vergil_tooling/lib/pr_workflow/prompts/`:
- `__init__.py` — package marker (so setuptools discovers it and `importlib.resources` can address it).
- `site-docs-reflection.md`, `docstring-accuracy.md`, `pr-description-fidelity.md`, `commit-message-fidelity.md`, `scope-coherence.md`, `test-adequacy.md` — one prompt per check.

**Modified:**
- `src/vergil_tooling/lib/pr_workflow/registry.py` — add `check_prompt(check_id) -> str`.
- `pyproject.toml` — declare the prompts as package data.
- `src/vergil_tooling/bin/vrg_pr_workflow.py` — `_next_audit` inlines the prompt into the directive.

**New tests** under `tests/vergil_tooling/pr_workflow/`:
- `test_prompts.py` — each prompt file exists and is structurally conformant (names its check id, the three statuses, and a JSON/`check.v1` output instruction).
- `test_registry.py` (extend) — `check_prompt` loads each id's text and rejects unknown ids.
- `test_cli_orchestration.py` (extend) — the audit directive carries the current check's prompt text.

Each prompt has one responsibility (one judgment). The registry stays the single source of truth for the check set; adding a future check is one `CHECK_IDS` entry + one `.md` file, no engine change.

---

## Task 1: The six judgment-check prompts (package data)

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/prompts/__init__.py`
- Create: the six `.md` prompt files (below)
- Modify: `pyproject.toml`
- Test: `tests/vergil_tooling/pr_workflow/test_prompts.py`

- [ ] **Step 1: Write the failing conformance test**

Create `tests/vergil_tooling/pr_workflow/test_prompts.py`:

```python
"""Structural conformance tests for the judgment-check prompts.

These do NOT judge prompt quality (that is eval-style and deferred); they assert
each prompt names its check, the three statuses, and a check.v1 JSON output
instruction, so the audit agent is always told how to shape its result.
"""

from __future__ import annotations

from importlib.resources import files

import pytest

from vergil_tooling.lib.pr_workflow.registry import check_ids

_PROMPTS = files("vergil_tooling.lib.pr_workflow.prompts")


@pytest.mark.parametrize("check_id", check_ids())
def test_prompt_file_exists_and_is_nonempty(check_id: str) -> None:
    text = (_PROMPTS / f"{check_id}.md").read_text(encoding="utf-8")
    assert text.strip(), f"{check_id}.md is empty"


@pytest.mark.parametrize("check_id", check_ids())
def test_prompt_is_structurally_conformant(check_id: str) -> None:
    text = (_PROMPTS / f"{check_id}.md").read_text(encoding="utf-8")
    assert check_id in text  # names its own check id
    for status in ("pass", "fail", "escalate"):
        assert status in text, f"{check_id}.md does not mention status '{status}'"
    assert "check.v1" in text  # tells the agent the output schema
    assert "JSON" in text or "json" in text


def test_every_check_id_has_a_prompt_file() -> None:
    for check_id in check_ids():
        assert (_PROMPTS / f"{check_id}.md").is_file()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_prompts.py -q`
Expected: FAIL — `ModuleNotFoundError`/`NotADirectoryError` (the prompts package does not exist yet).

- [ ] **Step 3: Create the prompts package marker**

Create `src/vergil_tooling/lib/pr_workflow/prompts/__init__.py`:

```python
"""Judgment-check prompts (package data, loaded by the registry)."""
```

- [ ] **Step 4: Author `site-docs-reflection.md`**

Create `src/vergil_tooling/lib/pr_workflow/prompts/site-docs-reflection.md`:

```markdown
# Check: site-docs-reflection

You are the AUDIT agent performing the `site-docs-reflection` judgment check on a
pull request. This is a non-mechanizable judgment: only a reader who understands
intent can tell whether user-facing changes are reflected in the documentation.

## What you are judging

If the repository publishes site documentation (look for `docs/site/`, `docs/`,
`mkdocs.yml`, a `docs/` Sphinx tree, or similar), do the **user-facing** changes in
this PR's delta have corresponding documentation updates?

## How to perform it

1. Determine whether site docs exist. If none exist, this check is `pass` (nothing
   to reflect into).
2. Read the cumulative delta for the range you were given (`git diff <base>..HEAD`).
   Identify changes that a user or operator would notice: new/changed CLI flags,
   commands, config keys, public APIs, behaviours, or workflows.
3. For each such change, check whether the docs were updated to match. A change with
   no doc update — when docs for that area exist — is a gap.

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "site-docs-reflection",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<doc-or-code path>", "line": <int>, "severity": "warning", "note": "<what is undocumented>" } ],
      "reason": "<why a human is needed>" }

- **pass** — no site docs exist, or every user-facing change is reflected in them.
- **fail** — one or more user-facing changes are missing from existing docs. Emit one
  `findings` entry per gap; point `file`/`line` at the code change that needs
  documenting and describe what is missing.
- **escalate** — you genuinely cannot tell whether docs are required (e.g. the
  docs structure is ambiguous and the call needs a human). Set `reason`.
```

- [ ] **Step 5: Author `docstring-accuracy.md`**

Create `src/vergil_tooling/lib/pr_workflow/prompts/docstring-accuracy.md`:

```markdown
# Check: docstring-accuracy

You are the AUDIT agent performing the `docstring-accuracy` judgment check. A linter
can check that a docstring *exists*; only judgment can tell whether it *truthfully*
describes the code it documents.

## What you are judging

For every docstring added or changed in this PR's delta, does it accurately describe
what the current code actually does — parameters, return values, behaviour, and side
effects?

## How to perform it

1. Read the cumulative delta (`git diff <base>..HEAD`). Find added/modified
   docstrings (module, class, function, method).
2. For each, read the code it documents and compare. Look for: stale parameter or
   return descriptions, described behaviour the code no longer has, omitted side
   effects, or copy-paste from another symbol.

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "docstring-accuracy",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<path>", "line": <int>, "severity": "warning", "note": "<the mismatch>" } ],
      "reason": "<why a human is needed>" }

- **pass** — every changed docstring matches its code (or no docstrings changed).
- **fail** — one or more docstrings misdescribe their code. Emit one `findings`
  entry per mismatch at the docstring's location, naming the discrepancy.
- **escalate** — the intended behaviour is ambiguous enough that you cannot tell
  whether the docstring or the code is wrong. Set `reason`.
```

- [ ] **Step 6: Author `pr-description-fidelity.md`**

Create `src/vergil_tooling/lib/pr_workflow/prompts/pr-description-fidelity.md`:

```markdown
# Check: pr-description-fidelity

You are the AUDIT agent performing the `pr-description-fidelity` judgment check. The
PR description is part of the forensic record of the change; only judgment can tell
whether prose honestly summarises a diff.

## What you are judging

Does the USER-authored PR description (the `summary` and `notes` in `pr_metadata`,
visible via `vrg-pr-workflow status`) honestly and completely match the cumulative
delta — no overclaiming, and no silent omission of significant changes?

## How to perform it

1. Read `pr_metadata.summary` and `pr_metadata.notes` from the workflow state
   (`vrg-pr-workflow status`).
2. Read the cumulative delta (`git diff <base>..HEAD`).
3. Compare: (a) does the description claim work that is not in the delta
   (overclaiming)? (b) does the delta make a significant change the description never
   mentions (silent omission)?

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "pr-description-fidelity",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<path or 'pr_metadata'>", "line": <int>, "severity": "warning", "note": "<overclaim or omission>" } ],
      "reason": "<why a human is needed>" }

- **pass** — the description faithfully reflects the delta.
- **fail** — there is overclaiming or a significant undisclosed change. Emit one
  `findings` entry each; for an omission point at the undocumented code change, for an
  overclaim use `"file": "pr_metadata"` and quote the unsupported claim.
- **escalate** — you cannot judge significance without product context a human holds.
  Set `reason`.
```

- [ ] **Step 7: Author `commit-message-fidelity.md`**

Create `src/vergil_tooling/lib/pr_workflow/prompts/commit-message-fidelity.md`:

```markdown
# Check: commit-message-fidelity

You are the AUDIT agent performing the `commit-message-fidelity` judgment check.
Conventional-commit *format* is mechanizable and lives in `vrg-validate`; whether a
message *truthfully* describes its diff is judgment.

## What you are judging

Does each commit message in this PR's delta truthfully describe what that commit
actually changed — not vague ("fix stuff"), not mislabeled (a `refactor:` that
changes behaviour, a `docs:` that edits code)?

## How to perform it

1. List the commits in range (`git log --oneline <base>..HEAD`).
2. For each commit, read its message and its diff (`git show <sha>`). Judge whether
   the message honestly and specifically describes the change, and whether the
   conventional-commit type matches what the diff does.

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "commit-message-fidelity",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<commit sha>", "line": 0, "severity": "warning", "note": "<the mismatch>" } ],
      "reason": "<why a human is needed>" }

- **pass** — every commit message truthfully and specifically describes its diff.
- **fail** — one or more messages are vague or mislabeled. Emit one `findings` entry
  per commit, putting the short SHA in `file`, `line: 0`, and the problem in `note`.
- **escalate** — you cannot determine intent (e.g. a squashed commit spanning
  unrelated work where the right message is genuinely unclear). Set `reason`.
```

- [ ] **Step 8: Author `scope-coherence.md`**

Create `src/vergil_tooling/lib/pr_workflow/prompts/scope-coherence.md`:

```markdown
# Check: scope-coherence

You are the AUDIT agent performing the `scope-coherence` judgment check. A linter
cannot know what an issue is "about"; only judgment can relate a diff to intent.

## What you are judging

Do all the changes in this PR's delta relate to the stated issue, or did unrelated
edits sneak in (scope creep)?

## How to perform it

1. Read the issue the PR addresses (its number is in the workflow state via
   `vrg-pr-workflow status`; read the issue body with `vrg-gh issue view <n>` if
   available).
2. Read the cumulative delta (`git diff <base>..HEAD`).
3. For each distinct change, judge whether it plausibly serves the stated issue.
   Flag changes that belong to a different concern and should be split into their own
   PR. (Incidental, directly-supporting changes — a needed refactor, a test for the
   fix — are in scope.)

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "scope-coherence",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<path>", "line": <int>, "severity": "warning", "note": "<why unrelated>" } ],
      "reason": "<why a human is needed>" }

- **pass** — every change serves the stated issue.
- **fail** — one or more unrelated changes are present. Emit one `findings` entry per
  out-of-scope change, naming the file and why it is unrelated.
- **escalate** — the issue is too vaguely scoped to judge relatedness. Set `reason`.
```

- [ ] **Step 9: Author `test-adequacy.md`**

Create `src/vergil_tooling/lib/pr_workflow/prompts/test-adequacy.md`:

```markdown
# Check: test-adequacy

You are the AUDIT agent performing the `test-adequacy` judgment check. Coverage
percentage is mechanizable and lives in `vrg-validate`; whether a test actually
*exercises the intent* of a change is judgment.

## What you are judging

For the new or changed behaviour in this PR's delta, do the tests meaningfully
exercise it — asserting the thing the change claims to do, including the failure or
edge cases the behaviour is about?

## How to perform it

1. Read the cumulative delta (`git diff <base>..HEAD`). Identify new/changed
   behaviour in production code.
2. Find the tests added/changed in the same delta. For each behaviour, judge whether
   a test asserts its intent — not merely that the code runs, but that it does the
   right thing (e.g. a validator is tested with an invalid input, not only a valid
   one).

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "test-adequacy",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<path>", "line": <int>, "severity": "warning", "note": "<what is untested>" } ],
      "reason": "<why a human is needed>" }

- **pass** — new/changed behaviour is meaningfully exercised (or the delta is
  non-behavioural, e.g. docs-only).
- **fail** — behaviour is untested or only superficially tested. Emit one `findings`
  entry per gap, pointing at the production change that lacks a meaningful test.
- **escalate** — testing the behaviour requires infrastructure or judgement a human
  must weigh (e.g. it can only be verified against a live external system). Set
  `reason`.
```

- [ ] **Step 10: Declare the prompts as package data**

In `pyproject.toml`, under `[tool.setuptools.package-data]` (after the existing
`vergil_tooling = [...]` line), add:

```toml
"vergil_tooling.lib.pr_workflow.prompts" = ["*.md"]
```

- [ ] **Step 11: Run the conformance test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_prompts.py -q`
Expected: PASS (13 passed — 6 + 6 parametrized + 1).

- [ ] **Step 12: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1534-pr-workflow-oracle
vrg-git add src/vergil_tooling/lib/pr_workflow/prompts/ pyproject.toml tests/vergil_tooling/pr_workflow/test_prompts.py
vrg-commit --type feat --scope prw --message "author the six judgment-check prompts" \
  --body "Add the six seed check prompts as package data (site-docs-reflection, docstring-accuracy, pr-description-fidelity, commit-message-fidelity, scope-coherence, test-adequacy), each authored to emit a check.v1 payload. Declared as setuptools package-data; structural conformance test asserts each names its check, the three statuses, and the JSON output shape."
```

---

## Task 2: `registry.check_prompt` loader

**Files:**
- Modify: `src/vergil_tooling/lib/pr_workflow/registry.py`
- Test: `tests/vergil_tooling/pr_workflow/test_registry.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/vergil_tooling/pr_workflow/test_registry.py`:

```python
import pytest

from vergil_tooling.lib.pr_workflow.errors import WorkflowError


@pytest.mark.parametrize("check_id", registry.check_ids())
def test_check_prompt_loads_named_check(check_id: str) -> None:
    text = registry.check_prompt(check_id)
    assert check_id in text
    assert "check.v1" in text


def test_check_prompt_rejects_unknown_id() -> None:
    with pytest.raises(WorkflowError, match="unknown check"):
        registry.check_prompt("made-up")
```

(The file already imports `from vergil_tooling.lib.pr_workflow import registry`.)

- [ ] **Step 2: Run it to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_registry.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'check_prompt'`.

- [ ] **Step 3: Implement `check_prompt`**

Replace the contents of `src/vergil_tooling/lib/pr_workflow/registry.py` with:

```python
"""The judgment-check registry.

The canonical check IDs (used by the engine to sequence checks) plus the prompt
loader (used by the CLI to inline a check's instructions into the audit directive).
Prompts live as package data under ``prompts/<id>.md`` and are read via
``importlib.resources`` so they resolve whether the package is run from source or
pip/uv-installed. Adding a check is one ``CHECK_IDS`` entry plus one ``.md`` file.
"""

from __future__ import annotations

from importlib.resources import files

from vergil_tooling.lib.pr_workflow.errors import WorkflowError

CHECK_IDS: tuple[str, ...] = (
    "site-docs-reflection",
    "docstring-accuracy",
    "pr-description-fidelity",
    "commit-message-fidelity",
    "scope-coherence",
    "test-adequacy",
)

_PROMPTS = files("vergil_tooling.lib.pr_workflow.prompts")


def check_ids() -> tuple[str, ...]:
    """Return the canonical, ordered tuple of judgment-check IDs."""
    return CHECK_IDS


def check_prompt(check_id: str) -> str:
    """Return the markdown prompt text for ``check_id``.

    Raises ``WorkflowError`` for an unknown id.
    """
    if check_id not in CHECK_IDS:
        raise WorkflowError(f"unknown check id {check_id!r}; known checks: {sorted(CHECK_IDS)}")
    return (_PROMPTS / f"{check_id}.md").read_text(encoding="utf-8")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/pr_workflow/test_registry.py -q`
Expected: PASS (2 original + 6 parametrized + 1 = 9 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/registry.py tests/vergil_tooling/pr_workflow/test_registry.py
vrg-commit --type feat --scope prw --message "load check prompts via importlib.resources" \
  --body "registry.check_prompt(id) reads prompts/<id>.md as package data; rejects unknown ids with WorkflowError. Keeps the engine pure — only the registry touches the filesystem."
```

---

## Task 3: Inline the current check's prompt into the audit directive

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_pr_workflow.py` (the `_next_audit` function)
- Test: `tests/vergil_tooling/pr_workflow/test_cli_orchestration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/vergil_tooling/pr_workflow/test_cli_orchestration.py`:

```python
from vergil_tooling.lib.pr_workflow.registry import check_ids, check_prompt


def test_next_audit_directive_inlines_the_current_check_prompt(capsys) -> None:
    transport = FakeTransport()
    transport.state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    cli._next_audit(_args(as_role="audit", issue="1534"), transport)
    directive = json.loads(capsys.readouterr().out)
    first = check_ids()[0]
    assert directive["check"] == first
    assert directive["prompt"] == check_prompt(first)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `vrg-container-run -- uv run pytest "tests/vergil_tooling/pr_workflow/test_cli_orchestration.py::test_next_audit_directive_inlines_the_current_check_prompt" -q`
Expected: FAIL — `KeyError: 'prompt'` (the directive has no prompt yet).

- [ ] **Step 3: Inline the prompt in `_next_audit`**

In `src/vergil_tooling/bin/vrg_pr_workflow.py`, add `registry` to the import:

```python
from vergil_tooling.lib.pr_workflow import engine, registry, settings
```

Then change the tail of `_next_audit` from:

```python
    state = transport.wait_until_owner("audit", timeout=_LONG_TIMEOUT)
    _emit(engine.directive_for(state, "audit"))
    return 0
```

to:

```python
    state = transport.wait_until_owner("audit", timeout=_LONG_TIMEOUT)
    directive = engine.directive_for(state, "audit")
    directive["prompt"] = registry.check_prompt(directive["check"])
    _emit(directive)
    return 0
```

(`directive["check"]` is the current pending check id — always present and non-null
when it is audit's turn, because the final `submit-check` of a round flips ownership
away from audit before the next `next`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `vrg-container-run -- uv run pytest "tests/vergil_tooling/pr_workflow/test_cli_orchestration.py::test_next_audit_directive_inlines_the_current_check_prompt" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_pr_workflow.py tests/vergil_tooling/pr_workflow/test_cli_orchestration.py
vrg-commit --type feat --scope prw --message "inline the current check's prompt into the audit directive" \
  --body "The CLI enriches each audit `next` directive with the pending check's prompt text (registry.check_prompt), so the agent receives exactly one prompt per round-trip. Engine stays pure; prompt I/O lives in the CLI/registry."
```

---

## Task 4: Full validation and Phase 2 wrap

**Files:** none (verification only).

- [ ] **Step 1: Run the entire validation pipeline**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — lint, typecheck (mypy + ty), full pytest suite at 100% coverage,
audit, and common checks (markdownlint covers the new `.md` prompts).

- [ ] **Step 2: Fix any findings**

If markdownlint flags a prompt (e.g. line length, heading spacing, fenced-code
language), fix it in the `.md` file and re-run `vrg-container-run -- vrg-validate`
until green. If coverage dropped, add the missing test. Do not suppress — fix.

- [ ] **Step 3: Commit any fixes**

```bash
vrg-git add -A
vrg-commit --type fix --scope prw --message "satisfy markdownlint/coverage for the check prompts" \
  --body "Address findings surfaced by vrg-validate on the Phase 2 prompts and loader."
```

(Skip if Step 1 was already green.)

- [ ] **Step 4: Confirm the deliverable**

Phase 2 is complete when `vrg-validate` is green: the six prompts load via the
registry and the audit directive inlines the current check's prompt. The audits are
now real and pluggable into the Phase 1 per-check loop. Phase 3 (skill rewrites +
`vrg-submit-pr` integration + identity enforcement) follows in its own plan.

---

## Self-Review

**Spec coverage (§12 Phase 2):**

- Six seed check prompts authored → Task 1 (one `.md` each, full text).
- Prompts as package data via `importlib.resources` → Task 1 (package-data) + Task 2
  (`check_prompt`).
- Registry holds the prompts → Task 2.
- Inline the current check's prompt into the audit directive → Task 3.
- Output maps 1:1 to `check.v1` → each prompt's Output section + the conformance test
  (Task 1) asserting `check.v1` + the three statuses are named.

Deferred (noted in the header): skill rewrites + `vrg-submit-pr` integration +
identity enforcement (Phase 3); eval-style judgment-quality testing; `GitHubTransport`.

**Placeholder scan:** No TBD/TODO; every prompt is complete prose; every code/test
step shows the actual content.

**Type/name consistency:** `registry.check_ids` (unchanged) and new
`registry.check_prompt(check_id) -> str`; the CLI calls `registry.check_prompt(directive["check"])`;
`_PROMPTS = files("vergil_tooling.lib.pr_workflow.prompts")` is used identically in
the prompts test and the registry. The six prompt filenames exactly match the six
`CHECK_IDS`. The directive key added (`prompt`) matches the test assertion.
