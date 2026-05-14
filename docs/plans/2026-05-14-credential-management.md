# Credential Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `GH_TOKEN` hard gate from `vrg-docker-run`,
update documentation to reflect the new credential management model
(classic PATs via `gh auth`, credential selection by `vrg-gh`), and
create tracking issues for deferred work.

**Architecture:** The credential management spec (#775) supersedes
the org governance design's Section 3. The immediate code changes are
small — removing a gate and updating docs. The credential selection
logic itself is implemented as part of the `vrg-gh` wrapper in the
permission model plan (Task 2 of
`docs/plans/2026-05-14-permission-model.md`), which must be updated
to incorporate credential selection from this spec. This plan handles
the changes that can land independently.

**Tech Stack:** Python (vergil-tooling codebase), Markdown (specs,
plans, guides)

**Spec:** `docs/specs/2026-05-14-credential-management-design.md`

**Relationship to permission model plan:** This plan is independent
of the permission model plan. Both can proceed in parallel. The
permission model plan's Task 2 (`vrg-gh`) must be updated to include
credential selection logic from this spec's Section 4, but that
update is tracked as a task in this plan (Task 5), not implemented
here.

---

## Task 1: Remove `GH_TOKEN` Hard Gate from `vrg-docker-run`

**Requirement:** Spec Section 6 — remove the hard gate; the
container launches regardless of whether `GH_TOKEN` is set.

**Note:** The env-var passthrough in `docker.py` (which forwards
`GH_*`, `GITHUB_*`, and `MQ_*` prefixes into the container) is
left as-is. Cleanup of the hardcoded prefix list is tracked
separately in #777.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_docker_run.py:40,80-86`
- Modify: `tests/vergil_tooling/test_vrg_docker_run.py:77-82`

#### RED — Container launches without `GH_TOKEN`

- [ ] **Step 1: Update the existing test to expect success**

  In `tests/vergil_tooling/test_vrg_docker_run.py`, replace
  `test_missing_gh_token` (lines 77-82) with a test that verifies
  the container launches without `GH_TOKEN`:

  ```python
  def test_launches_without_gh_token(tmp_path: Path) -> None:
      (tmp_path / "pyproject.toml").write_text("[project]\n")
      with (
          patch("vergil_tooling.bin.vrg_docker_run.git.repo_root", return_value=tmp_path),
          patch("vergil_tooling.bin.vrg_docker_run.assert_docker_available"),
          patch("vergil_tooling.bin.vrg_docker_run.ensure_cached_image") as mock_cache,
          patch("vergil_tooling.bin.vrg_docker_run.os.execvp") as mock_exec,
          patch.dict("os.environ", {}, clear=True),
      ):
          mock_cache.return_value = "ghcr.io/vergil-project/prod-python:3.14"
          main(["--", "uv", "run", "vrg-validate"])
      mock_exec.assert_called_once()
  ```

- [ ] **Step 2: Run the test to verify it fails**

  ```bash
  cd <worktree> && vrg-docker-run -- uv run pytest \
    tests/vergil_tooling/test_vrg_docker_run.py::test_launches_without_gh_token -v
  ```

  Expected: FAIL — `main()` returns 1 because the `GH_TOKEN` gate
  rejects the invocation before reaching `execvp`.

#### GREEN — Remove the gate

- [ ] **Step 3: Remove the `GH_TOKEN` check from `vrg_docker_run.py`**

  Delete lines 80-86:

  ```python
  # DELETE these lines:
  if not os.environ.get("GH_TOKEN"):
      print(
          "ERROR: GH_TOKEN is not set. Set GH_TOKEN in your environment before\n"
          "running vrg-docker-run. See docs/development/environment-setup.md.",
          file=sys.stderr,
      )
      return 1
  ```

- [ ] **Step 4: Update the usage text**

  In `_USAGE` (line 40), change:

  ```
  GH_TOKEN                (required) GitHub token passed into the container
  ```

  To:

  ```
  GH_TOKEN                GitHub token (passed into container when set)
  ```

- [ ] **Step 5: Run the new test to verify it passes**

  ```bash
  cd <worktree> && vrg-docker-run -- uv run pytest \
    tests/vergil_tooling/test_vrg_docker_run.py::test_launches_without_gh_token -v
  ```

  Expected: PASS

- [ ] **Step 6: Update help test assertion**

  In `test_help_flag` (line 23), the assertion `assert "GH_TOKEN"
  in out` still passes because `GH_TOKEN` remains in the usage text
  (it's optional now, not removed). Verify this test still passes:

  ```bash
  cd <worktree> && vrg-docker-run -- uv run pytest \
    tests/vergil_tooling/test_vrg_docker_run.py::test_help_flag -v
  ```

  Expected: PASS

- [ ] **Step 7: Run the full test file**

  ```bash
  cd <worktree> && vrg-docker-run -- uv run pytest \
    tests/vergil_tooling/test_vrg_docker_run.py -v
  ```

  Expected: all tests pass. Many existing tests use
  `{"GH_TOKEN": "tok"}` in their `patch.dict` — these still pass
  because the env var is still forwarded when present. The gate
  removal only affects the missing-token case.

- [ ] **Step 8: Run full validation**

  ```bash
  cd <worktree> && vrg-docker-run -- uv run vrg-validate
  ```

  Expected: all checks pass.

- [ ] **Step 9: Commit**

  ```bash
  cd <worktree> && vrg-commit --type refactor --scope docker-run \
    --message "remove GH_TOKEN hard gate from vrg-docker-run" \
    --body "The container now launches regardless of whether GH_TOKEN is set. GitHub credentials are not needed for validation, linting, or testing. When GH_TOKEN is present it is still passed through to the container. Ref #775." \
    --agent wphillipmoore-agent
  ```

---

## Task 2: Update Org Governance Design (Supersession Notice)

**Requirement:** Spec Section 7 — the org governance design's
Section 3 is superseded by the credential management spec.

**Files:**
- Modify: `docs/specs/2026-05-11-org-governance-design.md:193-453`

- [ ] **Step 1: Add supersession notice to Section 3**

  At the top of Section 3 (line 193, after `## Section 3: Credential
  Management`), add:

  ```markdown
  > **Superseded.** This section is superseded by the credential
  > management design spec
  > (`docs/specs/2026-05-14-credential-management-design.md`, #775).
  > The approach described below (fine-grained PATs, custom keychain
  > management) was replaced with classic PATs managed through
  > `gh auth`, with credential selection enforced by the `vrg-gh`
  > wrapper. The content below is retained as historical context.
  ```

- [ ] **Step 2: Commit**

  ```bash
  cd <worktree> && vrg-commit --type docs --scope specs \
    --message "add supersession notice to org governance credential section" \
    --body "Section 3 of the org governance design is superseded by the credential management spec (#775). Fine-grained PATs and custom keychain management are replaced with classic PATs via gh auth. Ref #775." \
    --agent wphillipmoore-agent
  ```

---

## Task 3: Update Org Governance Setup Plan

**Requirement:** Spec Section 7 — Tasks 2, 3, and 10 of the setup
plan are rewritten for classic PATs and `gh auth`.

**Files:**
- Modify: `docs/plans/2026-05-11-org-governance-setup.md:98-166,500+`

- [ ] **Step 1: Add supersession notice to Task 2**

  At the top of Task 2 (line 98, after `### Task 2: Generate Human
  Fine-Grained PAT`), add:

  ```markdown
  > **Superseded.** This task is superseded by the credential
  > management design (#775). Use a classic PAT instead of a
  > fine-grained PAT. Log in via `gh auth login --with-token`
  > instead of storing in macOS Keychain. See
  > `docs/specs/2026-05-14-credential-management-design.md`,
  > Section 3.
  ```

- [ ] **Step 2: Add supersession notice to Task 3**

  At the top of Task 3 (line 126, after `### Task 3: Store Human PAT
  in macOS Keychain`), add:

  ```markdown
  > **Superseded.** This task is superseded by the credential
  > management design (#775). Credentials are stored in `gh auth`,
  > not the macOS Keychain. The `GH_TOKEN` keychain entry and
  > `vergil/human-pat` entry are retired after the transition. See
  > `docs/specs/2026-05-14-credential-management-design.md`,
  > Section 3.
  ```

- [ ] **Step 3: Locate and add supersession notice to Task 10**

  Find Task 10 (the agent PAT creation task in Phase 3). Add:

  ```markdown
  > **Superseded.** This task is superseded by the credential
  > management design (#775). The agent account uses a classic PAT
  > logged in via `gh auth login --with-token`. Fine-grained PATs
  > for the agent account are abandoned (#761 closed as won't-fix).
  > See `docs/specs/2026-05-14-credential-management-design.md`,
  > Section 2.
  ```

- [ ] **Step 4: Commit**

  ```bash
  cd <worktree> && vrg-commit --type docs --scope plans \
    --message "add supersession notices to org governance setup plan" \
    --body "Tasks 2, 3, and 10 are superseded by the credential management design (#775). Classic PATs via gh auth replace fine-grained PATs and keychain storage. Ref #775." \
    --agent wphillipmoore-agent
  ```

---

## Task 4: Update Permission Model Plan (Task 2)

**Requirement:** Spec Section 4 and Section 7 — the `vrg-gh` wrapper
gains credential selection responsibility. The permission model plan's
Task 2 must be updated to include credential selection logic and
escalation.

**Files:**
- Modify: `docs/plans/2026-05-14-permission-model.md:142-246`

- [ ] **Step 1: Add credential selection note to Task 2**

  At the top of Task 2 (line 142, after `### Task 2: \`vrg-gh\`
  Wrapper`), add:

  ```markdown
  > **Extended by credential management design (#775).** This task
  > must also implement credential selection: `vrg-gh` determines
  > which `gh auth` account to use based on the command being
  > executed. Default is agent account; escalation to human account
  > is allowed only for release workflow operations with context
  > validation. `pr merge` and `pr review --approve` change from
  > unconditionally denied to conditionally allowed with credential
  > escalation. Additionally, mechanized tools that call `github.py`
  > directly (`vrg-merge-when-green`, `vrg-prepare-release`) must
  > be updated to set `GH_TOKEN` in their process environment
  > per-phase (Spec Section 5) — ship in the same PR as `vrg-gh`.
  > See `docs/specs/2026-05-14-credential-management-design.md`,
  > Sections 4 and 5.
  ```

- [ ] **Step 2: Add credential selection note to the permission model spec**

  In `docs/specs/2026-05-14-permission-model-design.md`, at the end
  of the `vrg-gh` section (after the `gh auth` denial row in the
  subcommand table, around line 211), add:

  ```markdown
  ### Credential Selection

  > See the credential management design
  > (`docs/specs/2026-05-14-credential-management-design.md`, #775)
  > for the full credential selection model. `vrg-gh` is responsible
  > for choosing which `gh auth` account to use per-command. The
  > `pr merge` and `pr review --approve` entries in the table above
  > are conditionally allowed for release workflow operations under
  > the human account — see that spec's Section 4.
  ```

- [ ] **Step 3: Commit**

  ```bash
  cd <worktree> && vrg-commit --type docs --scope plans \
    --message "add credential selection cross-references to permission model" \
    --body "The permission model plan and spec are updated to reference the credential management design (#775). vrg-gh gains credential selection responsibility. pr merge and pr review --approve change from denied to conditionally allowed with escalation. Ref #775." \
    --agent wphillipmoore-agent
  ```

---

## Task 5: Update Consuming Repo Setup Guide

**Requirement:** Spec Section 7 — consuming repo setup guide is
updated to reference `gh auth`, not `GH_TOKEN` export.

**Files:**
- Modify: `docs/site/docs/guides/consuming-repo-setup.md`

- [ ] **Step 1: Find the `GH_TOKEN` export instruction**

  Search for the `export GH_TOKEN` line in the guide:

  ```bash
  grep -n 'GH_TOKEN\|gh auth' docs/site/docs/guides/consuming-repo-setup.md
  ```

- [ ] **Step 2: Update the instruction**

  Replace the `export GH_TOKEN=$(gh auth token)` instruction with
  guidance that `GH_TOKEN` is loaded automatically from the
  developer's shell configuration or set by `vrg-gh`. Add a note
  that both human and agent accounts should be logged into `gh auth`:

  ```markdown
  Both the human and agent GitHub accounts must be logged into
  `gh auth` on the developer's machine. See the credential
  management design
  (`docs/specs/2026-05-14-credential-management-design.md`) for
  setup instructions.
  ```

  Remove any `export GH_TOKEN=...` instructions.

- [ ] **Step 3: Commit**

  ```bash
  cd <worktree> && vrg-commit --type docs --scope guides \
    --message "update consuming repo setup for gh auth credential model" \
    --body "Replace GH_TOKEN export instructions with gh auth reference. Developers log both accounts into gh auth; credential selection is handled by vrg-gh. Ref #775." \
    --agent wphillipmoore-agent
  ```

---

## Task 6: Create Follow-On Issues for Deferred Work

**Requirement:** Spec Section 8 — deferred credential lifecycle work
must have tracking issues.

**Files:** None (GitHub issues via `gh` CLI)

- [ ] **Step 1: Create token expiration monitoring issue**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "feat: token expiration monitoring for classic PATs" \
    --body-file <temp-file>
  ```

  Body content:

  ```markdown
  ## Problem

  Classic PATs can be created with expiration dates. An expired
  token in `gh auth` still returns from `gh auth token` but fails
  with 401 on API calls. There is no proactive warning.

  ## Proposed Solution

  A periodic report (monthly or as a CI job) that checks token
  expiration dates via the GitHub API and surfaces warnings for
  tokens approaching expiry (within 30 days).

  This issue also covers evaluating whether `vrg-credential-audit`
  (planned in the org governance design but never built) should be
  revived in modified form as the implementation vehicle for this
  monitoring.

  ## Context

  Deferred from credential management design (#775, Section 8).
  The 12-month expiration window on newly-created PATs provides
  runway, but this work must exist to prevent silent expiration.
  ```

- [ ] **Step 2: Create rotation procedures issue**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "docs: credential rotation procedures for classic PATs" \
    --body-file <temp-file>
  ```

  Body content:

  ```markdown
  ## Problem

  No documented procedure for rotating classic PATs or the GitHub
  App private key.

  ## Proposed Solution

  Document the annual rotation cadence: generate new PAT, update
  `gh auth login --with-token`, verify with `gh auth token -u`,
  revoke old PAT. Include the App private key rotation steps.

  ## Context

  Deferred from credential management design (#775, Section 8).
  Carried forward from org governance design (#717, Section 3).
  ```

- [ ] **Step 3: Create compromise response issue**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "docs: credential compromise response procedures" \
    --body-file <temp-file>
  ```

  Body content:

  ```markdown
  ## Problem

  No documented procedure for responding to suspected credential
  compromise.

  ## Proposed Solution

  Document the response: immediate revocation, operation halt,
  audit via org audit log, replacement credential issuance,
  retrospective. Include blast radius analysis per credential
  type (agent PAT, human PAT, App private key).

  ## Context

  Deferred from credential management design (#775, Section 8).
  Carried forward from org governance design (#717, Section 3).
  ```

- [ ] **Step 4: Record the issue numbers**

  Note all three issue numbers for the commit message.

- [ ] **Step 5: Commit (no files — issues only)**

  No commit needed. The issues are the deliverable.

---

## Task 7: Run Full Validation and Submit PR

**Files:** None (operational)

- [ ] **Step 1: Run full validation**

  ```bash
  cd <worktree> && vrg-docker-run -- uv run vrg-validate
  ```

  Expected: all checks pass.

- [ ] **Step 2: Push branch and create PR**

  ```bash
  cd <worktree> && git push -u origin feature/775-credential-management
  ```

  Create PR targeting `develop` with title and body summarizing
  all changes:
  - Removed `GH_TOKEN` hard gate from `vrg-docker-run`
  - Added supersession notices to org governance design and plan
  - Added credential selection cross-references to permission model
  - Updated consuming repo setup guide
  - Created tracking issues for deferred credential lifecycle work
  - Ref #775
