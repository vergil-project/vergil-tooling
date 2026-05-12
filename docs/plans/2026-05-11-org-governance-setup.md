# VERGIL Org Governance Setup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Set up the `vergil-project` GitHub org with the full governance
model (identity separation, branch protection, credential management,
tooling) so that the VERGIL migration (separate plan) can proceed into a
properly configured org.

**Architecture:** Three-phase approach — manual infrastructure setup
(accounts, org, credentials), codebase preparation (remove escape hatches,
consolidate co-author config), and post-migration governance activation
(org-level rulesets, verification). The codebase changes are small and
testable; the manual setup is scripted via `gh` CLI where possible.

**Tech Stack:** GitHub API via `gh` CLI, Python (standard-tooling
codebase), macOS Keychain via `security` CLI, GitHub Apps (JWT/PyJWT)

**Spec:** `docs/specs/2026-05-11-org-governance-design.md`

**Relationship to VERGIL rename plan:** This plan is Plan A (org setup).
The existing rename plan (`docs/plans/2026-05-11-vergil-rename.md`) is
Plan B (migration). Plan A must complete through Task 9 before Plan B
begins. Tasks 10–15 execute after Plan B completes.

**Task ordering within Phase 1:** Tasks 1 and 4 must complete before
Task 2 (PATs require the org to exist and the agent account to be
created). Task 2 must complete before Task 3 (Keychain storage needs
the PAT values). Task 6 requires the org to exist (Task 4).

---

## Phase 1: Manual Infrastructure Setup

These tasks are performed once, mostly via the GitHub web UI and `gh`
CLI. They establish the identities, credentials, and org that the rest
of the plan depends on.

### Task 1: Create `wphillipmoore-agent` GitHub Account

**Files:** None (GitHub web UI)

This replaces the per-harness accounts (`wphillipmoore-claude`,
`wphillipmoore-codex`) with a single agent identity.

- [ ] **Step 1: Create the GitHub account**

  Go to <https://github.com/signup>. Create account:
  - Username: `wphillipmoore-agent`
  - Email: a dedicated email address (not your primary)
  - The account must have a profile that identifies it as an AI agent
    identity belonging to you

- [ ] **Step 2: Configure the account profile**

  Set the profile bio to indicate this is an AI agent identity:
  - Name: `wphillipmoore-agent`
  - Bio: "AI agent identity for @wphillipmoore"
  - No avatar needed — the default is fine

- [ ] **Step 3: Enable 2FA on the account**

  Go to Settings → Password and authentication → Enable two-factor
  authentication. Use your authenticator app.

- [ ] **Step 4: Verify the account exists**

  ```bash
  gh api users/wphillipmoore-agent --jq '.login'
  ```

  Expected: `wphillipmoore-agent`

- [ ] **Step 5: Note the GitHub-assigned noreply email**

  ```bash
  gh api users/wphillipmoore-agent --jq '.id'
  ```

  The noreply email will be:
  `<ID>+wphillipmoore-agent@users.noreply.github.com`

  Record this — it is needed for the co-author trailer in Task 6.

### Task 2: Generate Fine-Grained PATs

**Files:** None (GitHub web UI)

Two PATs: one for the human identity, one for the agent identity.

- [ ] **Step 1: Generate the human PAT**

  Go to <https://github.com/settings/personal-access-tokens/new>
  (logged in as `wphillipmoore`).

  - Token name: `vergil-human`
  - Expiration: 1 year (maximum for fine-grained)
  - Resource owner: `vergil-project` (once the org exists — do this
    step after Task 3)
  - Repository access: All repositories
  - Permissions:
    - Administration: Read and write
    - Contents: Read and write
    - Issues: Read and write
    - Pull requests: Read and write
    - Actions: Read and write
    - Metadata: Read (auto-granted)

  Copy the token value.

- [ ] **Step 2: Generate the agent PAT**

  Go to <https://github.com/settings/personal-access-tokens/new>
  (logged in as `wphillipmoore-agent`).

  - Token name: `vergil-agent`
  - Expiration: 1 year
  - Resource owner: `vergil-project` (once the org exists and the
    agent account is invited — do this step after Task 4)
  - Repository access: All repositories
  - Permissions:
    - Contents: Read and write
    - Issues: Read and write
    - Pull requests: Read and write
    - Metadata: Read (auto-granted)
  - **Not granted:** Administration, Actions, org settings, secrets,
    deployments

  Copy the token value.

- [ ] **Step 3: Verify PAT scopes are correct**

  For each token, verify it can only do what it should:

  ```bash
  # Human PAT — should succeed
  GH_TOKEN=<human-pat> gh api user --jq '.login'
  # Expected: wphillipmoore

  # Agent PAT — should succeed
  GH_TOKEN=<agent-pat> gh api user --jq '.login'
  # Expected: wphillipmoore-agent
  ```

### Task 3: Store Credentials in macOS Keychain

**Files:** None (macOS Keychain)

- [ ] **Step 1: Store the human PAT**

  ```bash
  security add-generic-password \
    -a "vergil" \
    -s "vergil/human-pat" \
    -w "<paste-human-pat-here>" \
    -T "" \
    -U
  ```

- [ ] **Step 2: Store the agent PAT**

  ```bash
  security add-generic-password \
    -a "vergil" \
    -s "vergil/agent-pat" \
    -w "<paste-agent-pat-here>" \
    -T "" \
    -U
  ```

- [ ] **Step 3: Verify retrieval**

  ```bash
  security find-generic-password -s "vergil/human-pat" -w
  security find-generic-password -s "vergil/agent-pat" -w
  ```

  Each should print the stored token.

- [ ] **Step 4: Remove the global GH_TOKEN export**

  Remove any `export GH_TOKEN=...` from your shell profile
  (`~/.zshrc`, `~/.zprofile`, etc.). Verify:

  ```bash
  source ~/.zshrc && echo "${GH_TOKEN:-not set}"
  ```

  Expected: `not set`

  **Note:** After this step, raw `gh` commands will fall back to
  `gh auth login` state. The Vergil tooling will retrieve tokens from
  the Keychain. This is the transitional state — full credential
  integration happens in a later plan.

### Task 4: Create `vergil-project` GitHub Org

**Files:** None (GitHub web UI)

- [ ] **Step 1: Create the org**

  Go to <https://github.com/organizations/plan>
  - Plan: Free
  - Org name: `vergil-project`
  - Contact email: your GitHub email
  - Owner: your personal account (`wphillipmoore`)

- [ ] **Step 2: Verify org creation**

  ```bash
  gh api orgs/vergil-project --jq '.login'
  ```

  Expected: `vergil-project`

- [ ] **Step 3: Invite `wphillipmoore-agent` as outside collaborator**

  This cannot be done until repos exist in the org. Record this as a
  post-migration step — after repos are transferred, invite the agent
  account as an outside collaborator with Write access to each repo.

### Task 5: Configure Org Security Settings

**Files:** None (`gh` CLI)

- [ ] **Step 1: Require 2FA for all org members**

  ```bash
  gh api orgs/vergil-project \
    -X PATCH \
    -f two_factor_requirement_enabled=true
  ```

- [ ] **Step 2: Set default repository permission to Write**

  ```bash
  gh api orgs/vergil-project \
    -X PATCH \
    -f default_repository_permission=write
  ```

- [ ] **Step 3: Disable forking of private repos**

  ```bash
  gh api orgs/vergil-project \
    -X PATCH \
    -F members_can_fork_private_repositories=false
  ```

- [ ] **Step 4: Verify settings**

  ```bash
  gh api orgs/vergil-project \
    --jq '{two_factor: .two_factor_requirement_enabled, default_perm: .default_repository_permission, fork_private: .members_can_fork_private_repositories}'
  ```

  Expected:
  ```json
  {
    "two_factor": true,
    "default_perm": "write",
    "fork_private": false
  }
  ```

### Task 6: Register `vergil-release` GitHub App

**Files:** None (GitHub web UI + Keychain)

- [ ] **Step 1: Register the App**

  Go to <https://github.com/organizations/vergil-project/settings/apps/new>

  - App name: `vergil-release`
  - Homepage URL: `https://github.com/vergil-project`
  - Webhook: uncheck "Active" (not needed)
  - Permissions:
    - Repository permissions:
      - Contents: Read and write
      - Pull requests: Read and write
      - Metadata: Read-only
    - No organization permissions
    - No account permissions
  - Where can this app be installed: Only on this account

- [ ] **Step 2: Generate a private key**

  On the App settings page, under "Private keys", click
  "Generate a private key". Download the `.pem` file.

- [ ] **Step 3: Store the App private key and ID in Keychain**

  ```bash
  # Store the App ID (visible on the App settings page)
  security add-generic-password \
    -a "vergil" \
    -s "vergil/app-id" \
    -w "<app-id>" \
    -T "" \
    -U

  # Store the private key (read from the downloaded .pem file)
  security add-generic-password \
    -a "vergil" \
    -s "vergil/app-private-key" \
    -w "$(cat /path/to/downloaded-key.pem)" \
    -T "" \
    -U
  ```

- [ ] **Step 4: Install the App on the org**

  Go to the App settings page → "Install App" → Install on
  `vergil-project` → All repositories.

- [ ] **Step 5: Verify the installation**

  ```bash
  gh api orgs/vergil-project/installations --jq '.[].app_slug'
  ```

  Expected: `vergil-release`

- [ ] **Step 6: Delete the downloaded `.pem` file**

  ```bash
  rm /path/to/downloaded-key.pem
  ```

  The key is now stored in the Keychain only.

---

## Phase 2: Codebase Preparation

These tasks modify the standard-tooling codebase before the migration.
They are developed on a feature branch, submitted as a PR, and merged
to `develop` through the normal workflow. All changes are TDD.

### Task 7: Remove `skip_rulesets` Escape Hatch

**Files:**
- Modify: `src/standard_tooling/lib/config.py:62-63,143-146`
- Modify: `src/standard_tooling/lib/github_config.py:310-313`
- Modify: `tests/standard_tooling/test_config.py:264-282`
- Modify: `tests/standard_tooling/test_github_config_lib.py:300-330`
- Modify: `docs/specs/standard-tooling-toml.md` (remove `[github]`
  section docs)

- [ ] **Step 1: Update tests — remove skip_rulesets test cases**

  In `tests/standard_tooling/test_config.py`, remove:
  - The `_GITHUB_OVERRIDE_TOML` fixture (lines 264-270)
  - `test_read_config_github_overrides` (lines 273-276)
  - Update `test_read_config_no_github_section` (lines 279-282) —
    this test should still pass but the assertion changes since
    `GithubOverrides` will no longer exist

  In `tests/standard_tooling/test_github_config_lib.py`, remove:
  - The `skip_rulesets` parameter from the `_st_config` helper
    (line 306)
  - `test_compute_desired_state_skip_rulesets` (lines 328-330)

- [ ] **Step 2: Run tests to verify they fail**

  ```bash
  st-docker-run -- uv run pytest tests/standard_tooling/test_config.py -v
  st-docker-run -- uv run pytest tests/standard_tooling/test_github_config_lib.py -v
  ```

  Expected: compilation/import errors from removed test fixtures

- [ ] **Step 3: Remove `GithubOverrides` dataclass from config.py**

  In `src/standard_tooling/lib/config.py`:

  Remove the `GithubOverrides` dataclass (lines 62-63):
  ```python
  # DELETE:
  @dataclass
  class GithubOverrides:
      skip_rulesets: bool
  ```

  Remove the `[github]` parsing (lines 143-146):
  ```python
  # DELETE:
  github_raw = raw.get("github", {})
  github_overrides = GithubOverrides(
      skip_rulesets=bool(github_raw.get("skip-rulesets", False)),
  )
  ```

  Remove the `github` field from `StConfig` dataclass and its
  assignment in the constructor.

- [ ] **Step 4: Remove skip_rulesets check from github_config.py**

  In `src/standard_tooling/lib/github_config.py`, change lines
  310-313 from:

  ```python
  if not config.github.skip_rulesets:
      rulesets.append(desired_branch_protection_ruleset())
      rulesets.append(desired_tag_protection_ruleset())
      rulesets.append(desired_ci_gates_ruleset(config.project, config.ci))
  ```

  To:

  ```python
  rulesets.append(desired_branch_protection_ruleset())
  rulesets.append(desired_tag_protection_ruleset())
  rulesets.append(desired_ci_gates_ruleset(config.project, config.ci))
  ```

  Rulesets are now always computed — no escape hatch.

- [ ] **Step 5: Update the `_st_config` test helper**

  In `tests/standard_tooling/test_github_config_lib.py`, remove the
  `skip_rulesets` parameter and `github=GithubOverrides(...)` from
  the `_st_config` helper. Remove the `GithubOverrides` import.

- [ ] **Step 6: Run full validation**

  ```bash
  st-docker-run -- uv run st-validate
  ```

  Expected: all checks pass.

- [ ] **Step 7: Commit**

  ```bash
  st-commit --type refactor --scope config \
    --message "remove skip-rulesets escape hatch" \
    --body "The skip-rulesets override in [github] config is removed. Rulesets are now always enforced. No repo uses this override; the repo it was designed for (mq-rest-admin-template) is marked for archival." \
    --agent claude
  ```

### Task 8: Consolidate Co-Author Config to Single Agent Entry

**Files:**
- Modify: `standard-tooling.toml:8-10`
- Modify: `tests/standard_tooling/test_config.py` (update fixtures)

This task updates the co-author configuration in standard-tooling's own
config. Consumer repos will be updated during the migration (Plan B).

- [ ] **Step 1: Update tests — change co-author fixtures**

  In `tests/standard_tooling/test_config.py`, update `_BASE_TOML`
  (line 78) from:

  ```toml
  [project.co-authors]
  claude = "Co-Authored-By: user-claude <111+user-claude@users.noreply.github.com>"
  ```

  To:

  ```toml
  [project.co-authors]
  agent = "Co-Authored-By: user-agent <111+user-agent@users.noreply.github.com>"
  ```

  Update `test_read_config_valid` (lines 93-94) from:

  ```python
  assert "claude" in cfg.project.co_authors
  assert "user-claude" in cfg.project.co_authors["claude"]
  ```

  To:

  ```python
  assert "agent" in cfg.project.co_authors
  assert "user-agent" in cfg.project.co_authors["agent"]
  ```

  Update `test_read_config_malformed_co_author` (lines 125-126) to
  use `agent` instead of `claude` in the replacement string.

- [ ] **Step 2: Run tests to verify they fail**

  ```bash
  st-docker-run -- uv run pytest tests/standard_tooling/test_config.py -v
  ```

  Expected: assertion errors on co-author key names.

- [ ] **Step 3: Update `standard-tooling.toml`**

  Replace the co-author entries (lines 8-10) from:

  ```toml
  [project.co-authors]
  claude = "Co-Authored-By: wphillipmoore-claude <255925739+wphillipmoore-claude@users.noreply.github.com>"
  codex = "Co-Authored-By: wphillipmoore-codex <255923655+wphillipmoore-codex@users.noreply.github.com>"
  ```

  To:

  ```toml
  [project.co-authors]
  agent = "Co-Authored-By: wphillipmoore-agent <AGENT_ID+wphillipmoore-agent@users.noreply.github.com>"
  ```

  Replace `AGENT_ID` with the actual GitHub user ID from Task 1
  Step 5.

- [ ] **Step 4: Run full validation**

  ```bash
  st-docker-run -- uv run st-validate
  ```

  Expected: all checks pass.

- [ ] **Step 5: Commit**

  ```bash
  st-commit --type refactor --scope config \
    --message "consolidate co-author config to single agent identity" \
    --body "Replace per-harness co-author entries (claude, codex) with a single agent entry. The security boundary is human vs. not-human, not which AI tool produced the work." \
    --agent claude
  ```

### Task 9: Update `st-commit` for Agent Flag Compatibility

**Files:**
- Modify: `src/standard_tooling/bin/st_commit.py`
- Modify: `tests/standard_tooling/test_st_commit.py`

The `--agent` flag currently accepts `claude` or `codex`. After Task 8,
the only valid value in standard-tooling's own config is `agent`. But
consumer repos may still use `claude`/`codex` until they migrate. The
tool must continue to accept any value — it looks up the key in
`[project.co-authors]`, so this is already the case. This task verifies
that and updates any hardcoded references.

- [ ] **Step 1: Verify the tool accepts arbitrary agent names**

  Review `src/standard_tooling/bin/st_commit.py` to confirm the
  `--agent` flag does not validate against a fixed set of names —
  it should just be a string that is looked up in
  `config.project.co_authors`.

- [ ] **Step 2: Run existing tests**

  ```bash
  st-docker-run -- uv run pytest tests/standard_tooling/test_st_commit.py -v
  ```

  Expected: all pass. If any tests hardcode `claude` or `codex` as
  the agent name, update them to use `agent`.

- [ ] **Step 3: Commit (if any changes were needed)**

  ```bash
  st-commit --type refactor --scope commit \
    --message "update st-commit tests for agent identity convention" \
    --agent claude
  ```

---

## Phase 3: Post-Migration Governance Activation

These tasks execute after Plan B (the VERGIL rename/migration) is
complete and all repos are in the `vergil-project` org.

### Task 10: Invite Agent Account to Org Repos

**Files:** None (`gh` CLI)

- [ ] **Step 1: List all repos in the org**

  ```bash
  gh repo list vergil-project --json name --jq '.[].name'
  ```

- [ ] **Step 2: Invite `wphillipmoore-agent` as outside collaborator**

  For each repo:

  ```bash
  gh api orgs/vergil-project/outside_collaborators/wphillipmoore-agent \
    -X PUT \
    -f permission=push
  ```

  Or, if the API requires per-repo invitations:

  ```bash
  for repo in $(gh repo list vergil-project --json name --jq '.[].name'); do
    gh api repos/vergil-project/$repo/collaborators/wphillipmoore-agent \
      -X PUT \
      -f permission=push
  done
  ```

- [ ] **Step 3: Accept the invitation**

  Log in as `wphillipmoore-agent` and accept the collaboration
  invitations, or use the API:

  ```bash
  GH_TOKEN=<agent-pat> gh api user/repository_invitations --jq '.[].id' | \
    xargs -I{} gh api user/repository_invitations/{} -X PATCH
  ```

- [ ] **Step 4: Verify access**

  ```bash
  GH_TOKEN=<agent-pat> gh repo list vergil-project --json name --jq '.[].name'
  ```

  Expected: all repos listed.

### Task 11: Configure Org-Level Rulesets

**Files:** None (`gh` CLI)

- [ ] **Step 1: Create the branch protection ruleset for `develop`**

  ```bash
  gh api orgs/vergil-project/rulesets \
    -X POST \
    --input - <<'JSON'
  {
    "name": "Branch protection (develop)",
    "target": "branch",
    "enforcement": "active",
    "conditions": {
      "ref_name": {
        "include": ["refs/heads/develop"],
        "exclude": []
      }
    },
    "rules": [
      { "type": "pull_request",
        "parameters": {
          "required_approving_review_count": 1,
          "dismiss_stale_reviews_on_push": true,
          "require_code_owner_review": false,
          "require_last_push_approval": true,
          "required_review_thread_resolution": true
        }
      },
      { "type": "required_status_checks",
        "parameters": {
          "strict_status_checks_policy": true,
          "status_checks": []
        }
      },
      { "type": "deletion" },
      { "type": "non_fast_forward" }
    ],
    "bypass_actors": []
  }
  JSON
  ```

  **Note:** `bypass_actors: []` means no one can bypass — not even
  org owners. The `require_last_push_approval` ensures the reviewer
  is not the person who last pushed to the branch.

  **Note:** `status_checks` is empty at the org level because CI
  check names vary by repo. Per-repo CI gate rulesets (managed by
  `vrg-repo-config`) handle this.

- [ ] **Step 2: Create the branch protection ruleset for `main`**

  ```bash
  gh api orgs/vergil-project/rulesets \
    -X POST \
    --input - <<'JSON'
  {
    "name": "Branch protection (main)",
    "target": "branch",
    "enforcement": "active",
    "conditions": {
      "ref_name": {
        "include": ["refs/heads/main"],
        "exclude": []
      }
    },
    "rules": [
      { "type": "pull_request",
        "parameters": {
          "required_approving_review_count": 1,
          "dismiss_stale_reviews_on_push": true,
          "require_code_owner_review": false,
          "require_last_push_approval": true,
          "required_review_thread_resolution": true
        }
      },
      { "type": "required_status_checks",
        "parameters": {
          "strict_status_checks_policy": true,
          "status_checks": []
        }
      },
      { "type": "deletion" },
      { "type": "non_fast_forward" }
    ],
    "bypass_actors": []
  }
  JSON
  ```

- [ ] **Step 3: Verify rulesets are active**

  ```bash
  gh api orgs/vergil-project/rulesets --jq '.[] | {name: .name, enforcement: .enforcement}'
  ```

  Expected:
  ```json
  {"name": "Branch protection (develop)", "enforcement": "active"}
  {"name": "Branch protection (main)", "enforcement": "active"}
  ```

- [ ] **Step 4: Test the rulesets — verify direct push is blocked**

  Pick a test repo in the org. Try to push directly to `develop`:

  ```bash
  cd <test-repo>
  echo "test" >> /tmp/test-file
  # Attempt a direct push via the API or git push — should be rejected
  ```

  Expected: push rejected with a message about branch protection.

- [ ] **Step 5: Test the rulesets — verify PR without review is blocked**

  Create a test PR and attempt to merge without approval:

  ```bash
  GH_TOKEN=<agent-pat> gh pr create \
    --repo vergil-project/<test-repo> \
    --title "test: verify branch protection" \
    --body "Testing governance rulesets. Will delete." \
    --head <test-branch> \
    --base develop

  # Attempt to merge without approval — should fail
  GH_TOKEN=<agent-pat> gh pr merge <PR-NUMBER> \
    --repo vergil-project/<test-repo> \
    --merge
  ```

  Expected: merge rejected — required reviews not satisfied.

- [ ] **Step 6: Test the rulesets — verify human approval enables merge**

  Approve the test PR as the human, then merge:

  ```bash
  # Approve as human
  GH_TOKEN=<human-pat> gh pr review <PR-NUMBER> \
    --repo vergil-project/<test-repo> \
    --approve

  # Merge as human
  GH_TOKEN=<human-pat> gh pr merge <PR-NUMBER> \
    --repo vergil-project/<test-repo> \
    --merge
  ```

  Expected: merge succeeds.

- [ ] **Step 7: Clean up test branch**

  ```bash
  gh api repos/vergil-project/<test-repo>/git/refs/heads/<test-branch> -X DELETE
  ```

### Task 12: Update `vrg-repo-config` for Org-Aware Scope

**Files:**
- Modify: `src/standard_tooling/lib/github_config.py`
- Modify: `tests/standard_tooling/test_github_config_lib.py`

After the migration, `vrg-repo-config` (renamed from
`st-github-config`) must skip branch protection rulesets for org repos
since those are managed at the org level by `vrg-org-config`.

**Note:** This task uses the post-rename filenames. The actual file
paths will reflect the VERGIL rename.

- [ ] **Step 1: Write the failing test**

  In the github_config_lib test file, add:

  ```python
  def test_compute_desired_state_org_skips_branch_protection() -> None:
      state = compute_desired_state(
          _st_config(), visibility="public", is_org=True,
      )
      ruleset_names = [r.name for r in state.rulesets]
      assert "Branch protection" not in ruleset_names
      assert "Tag protection" in ruleset_names
      assert "CI gates" in ruleset_names

  def test_compute_desired_state_personal_includes_branch_protection() -> None:
      state = compute_desired_state(
          _st_config(), visibility="public", is_org=False,
      )
      ruleset_names = [r.name for r in state.rulesets]
      assert "Branch protection" in ruleset_names
      assert "Tag protection" in ruleset_names
      assert "CI gates" in ruleset_names
  ```

- [ ] **Step 2: Run tests to verify the first test fails**

  ```bash
  st-docker-run -- uv run pytest tests/standard_tooling/test_github_config_lib.py::test_compute_desired_state_org_skips_branch_protection -v
  ```

  Expected: FAIL — branch protection is currently always included.

- [ ] **Step 3: Update `compute_desired_state`**

  In `src/standard_tooling/lib/github_config.py`, change the ruleset
  computation to condition branch protection on `is_org`:

  ```python
  def compute_desired_state(config: StConfig, *, visibility: str, is_org: bool) -> DesiredState:
      rulesets: list[DesiredRuleset] = []
      if not is_org:
          rulesets.append(desired_branch_protection_ruleset())
      rulesets.append(desired_tag_protection_ruleset())
      rulesets.append(desired_ci_gates_ruleset(config.project, config.ci))
  ```

  For org repos, branch protection is managed at the org level
  (Task 11). For personal repos, the per-repo tool manages it.

- [ ] **Step 4: Run tests to verify both pass**

  ```bash
  st-docker-run -- uv run pytest tests/standard_tooling/test_github_config_lib.py -v -k "org_skips_branch_protection or personal_includes_branch_protection"
  ```

  Expected: both PASS.

- [ ] **Step 5: Run full validation**

  ```bash
  st-docker-run -- uv run st-validate
  ```

  Expected: all checks pass.

- [ ] **Step 6: Commit**

  ```bash
  st-commit --type feat --scope github-config \
    --message "skip branch protection rulesets for org repos" \
    --body "Org repos have branch protection managed at the org level via rulesets. The per-repo tool now skips branch protection for org repos and only manages tag protection and CI gates." \
    --agent claude
  ```

### Task 13: Update Migration Plan Task 1

**Files:**
- Modify: `docs/plans/2026-05-11-vergil-rename.md`

- [ ] **Step 1: Update Task 1 to reference org setup as prerequisite**

  Replace the current Task 1 Steps 3-5 (which create the org from
  scratch) with a verification step:

  ```markdown
  ### Step 3: Verify org setup is complete

  The `vergil-project` org was created and configured as part of the
  org governance setup plan
  (`docs/plans/2026-05-11-org-governance-setup.md`). Verify it is
  ready:

  ` ``bash
  gh api orgs/vergil-project --jq '.login'
  # Expected: vergil-project

  gh api orgs/vergil-project/rulesets --jq '.[].name'
  # Expected: Branch protection (develop), Branch protection (main)
  ` ``

  If either check fails, complete the org governance setup plan
  before proceeding.
  ```

- [ ] **Step 2: Commit**

  ```bash
  st-commit --type docs --scope plans \
    --message "update migration plan to reference org governance setup" \
    --agent claude
  ```

### Task 14: Create Deferred Work Issues in New Org

**Files:** None (`gh` CLI)

Create issues in the `vergil-project` org for all deferred work items
so they are tracked where the work will be done — not left behind in
the pre-migration issue tracker.

- [ ] **Step 1: Identify the target repo for governance issues**

  Governance tooling issues belong in `vergil-tooling` (the renamed
  standard-tooling). Verify it exists:

  ```bash
  gh repo view vergil-project/vergil-tooling --json name --jq '.name'
  ```

  Expected: `vergil-tooling`

- [ ] **Step 2: Create issue for `vrg-org-config` tool**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "feat: build vrg-org-config tool for automated org-level configuration" \
    --body-file /tmp/issue-vrg-org-config.md
  ```

  Body content:
  > Automate enforcement of org-level settings and rulesets. Currently,
  > org configuration is manual (gh CLI commands in the governance
  > setup plan). The tool should support audit, diff, and apply modes
  > — matching the pattern of vrg-repo-config — for org security
  > settings, org-level rulesets, outside collaborator management, and
  > GitHub App installation verification.
  >
  > **Spec:** docs/specs/2026-05-11-org-governance-design.md, Section 7
  >
  > **Depends on:** VERGIL migration complete

- [ ] **Step 3: Create issue for credential selection integration**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "feat: integrate keyring-based credential selection into all vrg-* tools" \
    --body-file /tmp/issue-credential-integration.md
  ```

  Body content:
  > Build keyring-based credential retrieval into all Vergil tools so
  > each tool automatically selects the correct PAT based on its role.
  > Development tools retrieve the agent PAT, administrative tools
  > retrieve the human PAT. No manual GH_TOKEN switching required.
  >
  > Includes a pluggable backend interface (macOS Keychain, Linux
  > Secret Service, Windows Credential Manager) via the keyring Python
  > library.
  >
  > **Spec:** docs/specs/2026-05-11-org-governance-design.md, Section 3
  > (Credential Tooling subsection)

- [ ] **Step 4: Create issue for `vrg-release` mechanized workflow**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "feat: build vrg-release fully mechanized release workflow" \
    --body-file /tmp/issue-vrg-release.md
  ```

  Body content:
  > Build the fully automated release tool that orchestrates the
  > entire release process: create release branch, update changelog,
  > open PR to main (using GitHub App identity), wait for CI, approve
  > and merge (using human PAT), back-merge to develop, tag release,
  > clean up.
  >
  > This replaces the current st-prepare-release + st-merge-when-green
  > workflow. The release tool uses two credentials: the GitHub App for
  > PR authorship and the human PAT for approval and merge.
  >
  > **Prerequisite:** Credential selection integration must be complete.
  >
  > **Spec:** docs/specs/2026-05-11-org-governance-design.md, Section 4

- [ ] **Step 5: Create issue for cross-human review CI check**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "feat: cross-human review CI check for multi-contributor orgs" \
    --body-file /tmp/issue-cross-human-review.md
  ```

  Body content:
  > Implement a CI status check that enforces cross-human
  > accountability for PR reviews. When a PR is opened by an AI agent
  > account (e.g., alice-agent), the check ensures the approver is a
  > different human than the agent's owner.
  >
  > Includes a scale-of-one safety valve: if the org has only one
  > human member, the check short-circuits with exit 0.
  >
  > **Requirement:** Mandatory the moment a second human joins the org.
  >
  > **Spec:** docs/specs/2026-05-11-org-governance-design.md, Section 2
  > (Cross-Human Review subsection)
  >
  > **Pre-migration issue:** wphillipmoore/standard-tooling#719

- [ ] **Step 6: Create issue for `vrg-setup-credentials` utility**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "feat: build vrg-setup-credentials guided contributor onboarding" \
    --body-file /tmp/issue-vrg-setup-credentials.md
  ```

  Body content:
  > Build a guided setup utility for new contributors to store their
  > PATs and App key in the platform's secure credential store.
  > Prompts for each token, validates format and scopes, and writes
  > entries under the standard credential names (vergil/human-pat,
  > vergil/agent-pat, etc.).
  >
  > **Spec:** docs/specs/2026-05-11-org-governance-design.md, Section 3
  > (Credential Tooling subsection)

- [ ] **Step 7: Create issue for `.github` profile repo and Claude Code permissions**

  ```bash
  gh issue create --repo vergil-project/vergil-tooling \
    --title "feat: set up .github profile repo and Claude Code permission model" \
    --body-file /tmp/issue-github-repo-sandbox.md
  ```

  Body content:
  > Two related setup tasks for the org:
  >
  > 1. Create the vergil-project/.github repository for org-level
  >    configuration (org README, default community health files,
  >    CONTRIBUTING.md, issue/PR templates).
  >
  > 2. Design a proper Claude Code permission model to move away from
  >    YOLO/bypass mode. Define the minimal set of allowed operations
  >    for AI agent sessions.
  >
  > **Pre-migration issue:** wphillipmoore/standard-tooling#718

- [ ] **Step 8: Verify all issues are created**

  ```bash
  gh issue list --repo vergil-project/vergil-tooling \
    --json number,title \
    --jq '.[] | "\(.number): \(.title)"'
  ```

  Expected: all six issues listed.

### Task 15: End-to-End Verification

**Files:** None (manual verification)

This is the final gate. All deferred issues are created (Task 14),
all governance is active, and the org is ready for production use.

- [ ] **Step 1: Verify identity separation**

  ```bash
  # Agent can push a branch
  GH_TOKEN=<agent-pat> git push origin test-verify-branch

  # Agent cannot merge without approval
  GH_TOKEN=<agent-pat> gh pr create \
    --repo vergil-project/<test-repo> \
    --title "test: identity verification" \
    --body "Verifying governance model." \
    --head test-verify-branch \
    --base develop

  GH_TOKEN=<agent-pat> gh pr merge <PR> --merge
  # Expected: FAIL — review required

  # Human approves
  GH_TOKEN=<human-pat> gh pr review <PR> --approve

  # Human merges
  GH_TOKEN=<human-pat> gh pr merge <PR> --merge
  # Expected: SUCCESS
  ```

- [ ] **Step 2: Verify credential isolation**

  ```bash
  # Agent PAT cannot administer
  GH_TOKEN=<agent-pat> gh api orgs/vergil-project \
    -X PATCH \
    -f description="test"
  # Expected: 403 Forbidden

  # Human PAT can administer
  GH_TOKEN=<human-pat> gh api orgs/vergil-project \
    -X PATCH \
    -f description="VERGIL — Validation Engine for Repository Governance, Integration & Lifecycle"
  # Expected: 200 OK
  ```

- [ ] **Step 3: Verify GitHub App**

  ```bash
  # Verify the App is installed
  GH_TOKEN=<human-pat> gh api orgs/vergil-project/installations \
    --jq '.[].app_slug'
  # Expected: vergil-release
  ```

- [ ] **Step 4: Clean up test branches and PRs**

  Remove any test branches and close any test PRs created during
  verification.

- [ ] **Step 5: Record completion**

  The org governance setup is complete. Plan B (the VERGIL
  rename/migration) can now proceed.

---

## Deferred to Separate Plans

The following items are described in the governance design spec but
are substantial engineering projects that warrant their own
spec → plan → implementation cycles:

1. **`vrg-org-config` tool** — automated enforcement of org-level
   settings and rulesets. Currently, org configuration is manual
   (Tasks 5, 11). The tool automates this for auditability and
   repeatability.

2. **Credential selection integration** — building the `keyring`-based
   credential retrieval into all Vergil tools so that each tool
   automatically selects the correct PAT based on its role. Currently,
   credentials are manually set via `GH_TOKEN`.

3. **`vrg-release` mechanized release workflow** — the fully automated
   release tool that uses the GitHub App for PR creation and the human
   PAT for approval/merge. Currently, the release workflow uses
   `st-prepare-release` + `st-merge-when-green` with manual
   intervention.

4. **Cross-human review CI check (#719)** — required at humans > 1.

5. **`vrg-setup-credentials` utility** — guided setup for new
   contributors to store their PATs and App key in the platform's
   secure credential store.

These items do not block the org setup or migration. They improve
automation and enforcement over time.
