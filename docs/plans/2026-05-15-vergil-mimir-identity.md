# Vergil/Mimir Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `-agent` identity convention with `-vergil`, revert the #799 credential workaround, update all specs/docs/tests, and seed the `mimir-project` GitHub org.

**Architecture:** Three phases executed sequentially. Phase 1 is a collaborative human-driven account creation walkthrough that produces a setup guide. Phase 2 is a single code PR renaming `-agent` to `-vergil` across the codebase and reverting the temporary credential workaround. Phase 3 seeds the `mimir-project` org with its `.github` repo.

**Tech Stack:** Python (vergil-tooling), GitHub CLI (`gh`), TOML config, Markdown docs.

**Spec:** `docs/specs/2026-05-15-vergil-mimir-identity-design.md`

---

## Phase 1 — Account Creation and Documentation

Phase 1 is human-driven. The AI agent guides and documents but the
human performs all account creation and credential operations. Each
task is a conversation checkpoint — not a code commit.

### Task 1: Create the `wphillipmoore-vergil` GitHub Account

This task is performed by the human with AI guidance. The AI
documents each step for the setup guide.

**Files:**
- Create: `docs/site/docs/guides/account-setup.md` (started here, completed in Task 3)

- [ ] **Step 1: Create the GitHub account**

The human creates a new GitHub account named `wphillipmoore-vergil`.
Document the account creation process:
- Go to github.com/signup
- Username: `wphillipmoore-vergil`
- Email: a dedicated email (or alias) distinct from the human account

- [ ] **Step 2: Configure the profile**

The human configures the profile to mitigate shadow-ban risk (a
bare account with high automated activity is the likely trigger):
- Display name: set to something like "Phillip Moore (Vergil)"
- Bio: explain the account's purpose (e.g., "AI agent identity for
  @wphillipmoore — operates under the VERGIL methodology")
- Profile links: point back to the human account
- Avatar: the Vergil image

Record each field and its purpose in notes.

- [ ] **Step 3: Record the noreply email**

The human navigates to Settings → Emails on the new account and
records the GitHub noreply email address (format:
`<id>+wphillipmoore-vergil@users.noreply.github.com`). This value
is needed for the `vergil.toml` co-author entry in Phase 2.

- [ ] **Step 4: Set up `gh auth login`**

The human runs:
```bash
gh auth login -h github.com -u wphillipmoore-vergil
```

Then verifies both accounts are logged in:
```bash
gh auth status
```

Expected output should show two accounts:
```
github.com
  ✓ Logged in to github.com account wphillipmoore (keyring)
  ✓ Logged in to github.com account wphillipmoore-vergil (keyring)
```

- [ ] **Step 5: Grant outside-collaborator access**

The human (as org owner) invites `wphillipmoore-vergil` as an
outside collaborator with Write access on the appropriate
`vergil-project` repos. Then accepts the invitation from the
`-vergil` account.

### Task 2: Create the `wphillipmoore-mimir` GitHub Account

Same process as Task 1, for the Mimir identity.

- [ ] **Step 1: Create the GitHub account**

Username: `wphillipmoore-mimir`. Same process as Task 1 Step 1.

- [ ] **Step 2: Configure the profile**

The Mimir profile leans into the evil robot personality:
- Display name: "Phillip Moore (Mimir)"
- Bio: adversarial tone (e.g., "Chaos agent for @wphillipmoore —
  dedicated to the complete destruction of your guardrails")
- Avatar: the Mimir dystopian robot image

- [ ] **Step 3: Record the noreply email**

Same process as Task 1 Step 3. Record the noreply address for
future use (not needed in `vergil.toml` — Mimir has no co-author
entry in Vergil tooling).

- [ ] **Step 4: Set up `gh auth login`**

```bash
gh auth login -h github.com -u wphillipmoore-mimir
```

Verify:
```bash
gh auth status
```

Note: the human may need to switch active accounts between
sessions. Three accounts will now be logged in.

### Task 3: Write the Vergil Account Setup Guide

Compile the notes from Tasks 1 and 2 into a formal guide.

**Files:**
- Create: `docs/site/docs/guides/account-setup.md`

- [ ] **Step 1: Write the setup guide**

Structure the guide based on the documented steps from Tasks 1-2.
Sections:

1. **Prerequisites** — a human GitHub account, `gh` CLI installed
2. **Create the `-vergil` account** — naming convention, email
3. **Configure the profile** — display name, bio, avatar, links
   (include shadow-ban mitigation rationale)
4. **Record the noreply email** — where to find it, why it's needed
5. **Authenticate with `gh auth`** — `gh auth login` command,
   verify with `gh auth status`
6. **Request collaborator access** — org owner invites as outside
   collaborator, accept invitation

Tone: professional, constructive. This is the Vergil version.

- [ ] **Step 2: Commit the guide**

```bash
vrg-commit --type docs --scope identity \
  --message "add Vergil account setup guide" \
  --agent wphillipmoore-vergil
```

Note: this commit uses the NEW `-vergil` account. If the tooling
rename (Phase 2) has not landed yet, use the old `-agent` key and
update after Phase 2.

### Task 4: Retire the `wphillipmoore-agent` Account

- [ ] **Step 1: Log out the old account**

```bash
gh auth logout -u wphillipmoore-agent
```

- [ ] **Step 2: Revoke collaborator access**

The human (as org owner) removes `wphillipmoore-agent` as an
outside collaborator from all `vergil-project` repos.

- [ ] **Step 3: Verify `gh auth status`**

```bash
gh auth status
```

Should show only `wphillipmoore` and `wphillipmoore-vergil` (and
`wphillipmoore-mimir` if set up).

---

## Phase 2 — Tooling Rename PR

All tasks in this phase target the worktree at
`.worktrees/issue-805-vergil-mimir-identity/`. Every file path
below is relative to that worktree root.

### Task 5: Update `vrg-gh` Account Discovery

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_gh.py:64-82`
- Test: `tests/vergil_tooling/test_vrg_gh.py`

- [ ] **Step 1: Update the discovery test fixtures**

In `tests/vergil_tooling/test_vrg_gh.py`, update all `-agent`
references to `-vergil`:

```python
_AUTH_STATUS_TWO_ACCOUNTS = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  - Active account: false
"""
```

```python
def test_discover_accounts() -> None:
    from vergil_tooling.bin.vrg_gh import _discover_accounts

    with patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=_AUTH_STATUS_TWO_ACCOUNTS,
        )
        human, agent = _discover_accounts()
    assert human == "jdoe"
    assert agent == "jdoe-vergil"
```

Update `_AUTH_STATUS_DUPLICATE_HUMAN` similarly:

```python
_AUTH_STATUS_DUPLICATE_HUMAN = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
  ✓ Logged in to github.com account jdoe (token)
  - Active account: false
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  - Active account: false
"""
```

```python
def test_discover_accounts_deduplicates() -> None:
    from vergil_tooling.bin.vrg_gh import _discover_accounts

    with patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=_AUTH_STATUS_DUPLICATE_HUMAN,
        )
        human, agent = _discover_accounts()
    assert human == "jdoe"
    assert agent == "jdoe-vergil"
```

Update the error message test — `_AUTH_STATUS_NO_AGENT` stays the
same (it has no agent account at all), but the test name and
docstring should reference `-vergil`:

Rename `test_discover_accounts_missing_agent` to
`test_discover_accounts_missing_vergil`.

Update the `_discover_accounts` mock return values in the two
credential tests (`test_default_uses_human_token_workaround` and
`test_pr_merge_release_branch_escalates`) from
`("jdoe", "jdoe-agent")` to `("jdoe", "jdoe-vergil")`, and update
the assertion `"jdoe-agent" not in` to `"jdoe-vergil" not in`.
These tests will be fully rewritten in Task 6 — this step only
updates the fixture data to eliminate `-agent` references.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .worktrees/issue-805-vergil-mimir-identity && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py -v`

Expected: failures in discovery tests (code still checks `-agent`).

- [ ] **Step 3: Update `_discover_accounts()` in `vrg_gh.py`**

In `src/vergil_tooling/bin/vrg_gh.py`, lines 73-80:

```python
def _discover_accounts() -> tuple[str, str]:
    result = subprocess.run(  # noqa: S603
        ["gh", "auth", "status"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout or result.stderr
    accounts = list(dict.fromkeys(re.findall(r"Logged in to github\.com account (\S+)", output)))
    human = [a for a in accounts if not a.endswith("-vergil")]
    agent = [a for a in accounts if a.endswith("-vergil")]
    if len(human) != 1 or len(agent) != 1:
        print(
            "vrg-gh: cannot discover accounts. Expected one human and one "
            f"-vergil account in gh auth status. Found human={human}, agent={agent}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return human[0], agent[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .worktrees/issue-805-vergil-mimir-identity && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-commit --type refactor --scope vrg-gh \
  --message "rename -agent suffix to -vergil in account discovery" \
  --agent wphillipmoore-vergil
```

### Task 6: Revert the `_get_token` Workaround (#799)

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_gh.py:85-97`
- Test: `tests/vergil_tooling/test_vrg_gh.py`

The `_get_token` function currently always uses the human account's
credentials as a workaround for the shadow-banned `-agent` account
(#799). With the new `-vergil` account unflagged, restore normal
credential selection: agent credentials by default.

- [ ] **Step 1: Update the credential selection test**

Replace `test_default_uses_human_token_workaround` with a test
that verifies agent credentials are used by default:

```python
def test_default_uses_agent_token() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh._discover_accounts",
            return_value=("jdoe", "jdoe-vergil"),
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="agent-token\n")
        from vergil_tooling.bin.vrg_gh import _get_token

        token = _get_token(["issue", "list"])
    assert token == "agent-token"  # noqa: S105
    token_call = mock_run.call_args_list[-1]
    assert "jdoe-vergil" in token_call[0][0]
    assert "jdoe" not in token_call[0][0] or "jdoe-vergil" in token_call[0][0]
```

Also update the escalation test to verify human token is used for
`pr merge`:

```python
def test_pr_merge_escalates_to_human_token() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh._discover_accounts",
            return_value=("jdoe", "jdoe-vergil"),
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
        patch("vergil_tooling.bin.vrg_gh._validate_merge_context"),
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="human-token\n")
        from vergil_tooling.bin.vrg_gh import _get_token

        token = _get_token(["pr", "merge", "42"])
    assert token == "human-token"  # noqa: S105
    token_call = mock_run.call_args_list[-1]
    assert "jdoe" in token_call[0][0]
    assert "jdoe-vergil" not in token_call[0][0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .worktrees/issue-805-vergil-mimir-identity && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py::test_default_uses_agent_token -v`

Expected: FAIL (code still uses human token for all commands).

- [ ] **Step 3: Implement credential selection in `_get_token`**

Replace the workaround with proper credential selection:

```python
def _get_token(command: list[str]) -> str:
    human, agent = _discover_accounts()
    pair = (command[0], command[1]) if len(command) >= 2 else ()  # noqa: PLR2004
    account = human if pair in _ESCALATED_COMMANDS else agent

    result = subprocess.run(  # noqa: S603
        ["gh", "auth", "token", "-u", account],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .worktrees/issue-805-vergil-mimir-identity && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-commit --type fix --scope vrg-gh \
  --message "revert #799 workaround, restore agent credential selection" \
  --agent wphillipmoore-vergil
```

### Task 7: Update `vergil.toml` Co-Author Entry

**Files:**
- Modify: `vergil.toml`
- Test: `tests/vergil_tooling/test_config.py` (no changes needed — tests
  use generic `agent` key, not the real account name)

- [ ] **Step 1: Update the co-author entry**

In `vergil.toml`, replace:

```toml
[project.co-authors]
wphillipmoore-agent = "Co-Authored-By: wphillipmoore-agent <284101533+wphillipmoore-agent@users.noreply.github.com>"
```

With (substitute the actual noreply email recorded in Task 1 Step 3):

```toml
[project.co-authors]
wphillipmoore-vergil = "Co-Authored-By: wphillipmoore-vergil <NOREPLY_ID+wphillipmoore-vergil@users.noreply.github.com>"
```

- [ ] **Step 2: Run full validation**

Run: `cd .worktrees/issue-805-vergil-mimir-identity && vrg-docker-run -- uv run vrg-validate`

Expected: all checks pass. The config parser validates the co-author
trailer format.

- [ ] **Step 3: Commit**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-commit --type chore --scope config \
  --message "rename co-author entry from -agent to -vergil" \
  --agent wphillipmoore-vergil
```

### Task 8: Update Org Governance Spec

**Files:**
- Modify: `docs/specs/2026-05-11-org-governance-design.md`

- [ ] **Step 1: Replace all `-agent` references**

Search and replace across the file. Key locations (from grep):

- Line 43: `<username>-agent` → `<username>-vergil` in the
  identity table
- Line 71: "The agent account name follows the pattern
  `<username>-agent`" → `<username>-vergil`
- Line 85: `wphillipmoore-agent` → `wphillipmoore-vergil`
- Line 87: "the `-agent` convention" → "the `-vergil` convention"
- Line 180: `<username>-agent` naming convention →
  `<username>-vergil`
- Line 457: "create a `<username>-agent` account" →
  `<username>-vergil`
- Line 531: same pattern
- Line 555: same pattern

Add a note at the top of the spec that the identity convention was
updated by #805 from `-agent` to `-vergil`.

- [ ] **Step 2: Commit**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-commit --type docs --scope specs \
  --message "update org governance spec: -agent to -vergil" \
  --agent wphillipmoore-vergil
```

### Task 9: Update Credential Management Spec

**Files:**
- Modify: `docs/specs/2026-05-14-credential-management-design.md`

- [ ] **Step 1: Replace all `-agent` references**

Key locations (from grep):

- Line 60: `<username>-agent` → `<username>-vergil` in identity
  table
- Line 63: naming convention description
- Line 144: `gh auth token -u <username>-agent` →
  `<username>-vergil`
- Line 224: `f"{human_account}-agent"` → `f"{human_account}-vergil"`
  in pseudocode
- Line 257: "end in `-agent`" → "end in `-vergil`"
- Line 258: derive agent account description
- Line 335: `wphillipmoore-agent` → `wphillipmoore-vergil`
- Line 397: account creation instructions
- Line 413: `gh auth token` example
- Line 415: collaborator invitation

Add a note referencing #805.

- [ ] **Step 2: Commit**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-commit --type docs --scope specs \
  --message "update credential management spec: -agent to -vergil" \
  --agent wphillipmoore-vergil
```

### Task 10: Update Permission Model Spec

**Files:**
- Modify: `docs/specs/2026-05-14-permission-model-design.md`

- [ ] **Step 1: Replace `-agent` references**

From grep, line 449 references `block-agent-merge`. Check the full
file for any other `-agent` references and update them. The hook
name `block-agent-merge` may stay as-is if it's a GitHub webhook
name that's already deployed — check before renaming.

- [ ] **Step 2: Commit**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-commit --type docs --scope specs \
  --message "update permission model spec: -agent to -vergil" \
  --agent wphillipmoore-vergil
```

### Task 11: Update CLAUDE.md and AGENTS.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md` (if it exists and references `-agent`)

- [ ] **Step 1: Check and update references**

From grep, `CLAUDE.md` line 52 mentions "parallel-agent session"
which is a generic term (not the suffix convention) — likely no
change needed. Search both files for any references to the
`-agent` suffix convention or `wphillipmoore-agent` specifically
and update them.

- [ ] **Step 2: Commit**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-commit --type docs --scope config \
  --message "update CLAUDE.md and AGENTS.md: -agent to -vergil" \
  --agent wphillipmoore-vergil
```

### Task 12: Run Full Validation

- [ ] **Step 1: Run `vrg-validate`**

```bash
cd .worktrees/issue-805-vergil-mimir-identity && \
vrg-docker-run -- uv run vrg-validate
```

Expected: all checks pass (lint, typecheck, tests, audit, common
checks).

- [ ] **Step 2: Fix any failures**

If validation fails, fix the issue and commit the fix.

---

## Phase 3 — Mimir Org Seeding

Phase 3 is primarily human-driven (GitHub org creation) with AI
assistance writing the content.

### Task 13: Create the `mimir-project` GitHub Org

- [ ] **Step 1: Create the org**

The human creates the `mimir-project` GitHub organization via
github.com/organizations/plan.

- [ ] **Step 2: Create the `.github` repo**

The human creates `mimir-project/.github` as a public repository.

- [ ] **Step 3: Grant collaborator access**

The human invites `wphillipmoore-mimir` as an outside collaborator
with Write access on `mimir-project/.github`.

### Task 14: Write the Mimir Org Profile and Docs

**Files:**
- Create: `profile/README.md` (in `mimir-project/.github`)
- Create: `CONTRIBUTING.md` (in `mimir-project/.github`)

- [ ] **Step 1: Write the org profile README**

The README is Mimir's manifesto — deliberately over-the-top evil
robot personality. Content should cover:

- What Mimir is (adversarial testing of the VERGIL methodology)
- The chaos monkey concept
- The duality with Vergil (link to `vergil-project`)
- Written in Mimir's voice — the tone is a commentary on AI
  failure modes given a face and name

- [ ] **Step 2: Write the contributing guide**

The CONTRIBUTING.md leans into the personality. Same technical
structure as a normal contributing guide (how to set up, how to
submit work) but written in Mimir's voice. Requirements to
contribute include "commitment to the complete destruction of
guardrails" etc.

Include the Mimir-flavored account setup guide (same technical
content as the Vergil version from Task 3, different personality).

- [ ] **Step 3: Commit and push**

Push the content to `mimir-project/.github`.
