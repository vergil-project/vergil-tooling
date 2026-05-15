# Claude Code Permission Model — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate from `bypassPermissions` mode to a defense-in-depth
permission model where agents operate through VRG wrapper tools with
subcommand allowlists, Claude Code's permission layer controls what
reaches the shell, and existing plugin hooks serve as a backstop.

**Architecture:** Seven-step rollout — build wrappers, update hooks,
update CLAUDE.md guidance, deploy project settings to consuming repos,
deploy vergil-tooling's own permission config, switch the global
default mode, then observe and refine.

**Tech Stack:** Python CLI tools (argparse), Claude Code settings (JSON),
vergil plugin hooks (JSON + bash), CLAUDE.md (markdown)

**Spec:** `docs/specs/2026-05-14-permission-model-design.md`

**Execution order:** This plan and the credential management plan
(#775, `docs/plans/2026-05-14-credential-management.md`) are executed
as a unit. Phase 0: credential management Tasks 1-3 and 6 (independent
prep). Phase 1: this plan's Tasks 1-5, with Task 2 (`vrg-gh`)
incorporating credential selection from the credential management
spec's Section 4. Phase 2: this plan's Tasks 6-10 (deploy). Phase 3:
credential management Tasks 5 and 7 (finalize).

---

## Phase 1: Build the Wrappers

The wrappers are the foundation — everything else depends on agents
having `vrg-git` and `vrg-gh` available before the deny rules go live.

### Task 1: `vrg-git` Wrapper

**Requirement:** Spec Section 2 — validate git subcommands and flags
before execution; deny dangerous operations; log all invocations.

**Files:**
`src/vergil_tooling/bin/vrg_git.py`,
`tests/test_vrg_git.py`,
`pyproject.toml` (console script entry)

#### RED — Subcommand allowlist

- [ ] Write tests that each allowed subcommand (`status`, `log`,
      `diff`, `show`, `branch`, `ls-remote`, `rev-parse`, `add`,
      `push`, `fetch`, `pull`, `checkout`, `switch`, `stash`, `merge`,
      `cherry-pick`, `rebase`) passes validation and reaches
      `subprocess.run` (mock the subprocess call)
- [ ] Write tests that compound subcommands (`worktree add`,
      `worktree list`, `worktree remove`) pass validation when the
      first two arguments are provided as a pair
- [ ] Write a test that an unrecognized subcommand (e.g., `bisect`)
      exits non-zero with a clear error message
- [ ] Write a test that invoking `vrg-git` with no arguments exits
      non-zero with a usage message
- [ ] Expected failure: no implementation exists yet
- [ ] If any test passes unexpectedly: the test is not isolating
      correctly — it may be calling real `git` instead of validating
      through the wrapper

#### GREEN — Subcommand allowlist

- [ ] Create `src/vergil_tooling/bin/vrg_git.py` with a `main()`
      entry point that reads `sys.argv[1:]`, validates the first
      argument against the subcommand allowlist (dict lookup), and
      calls `subprocess.run(["git"] + args, shell=False)`
- [ ] Handle compound subcommands by checking whether the first
      argument is `worktree` and validating the pair
- [ ] Reject unrecognized subcommands with exit code 1 and a message
      naming the rejected subcommand
- [ ] Add `vrg-git` console script entry point in `pyproject.toml`
- [ ] All RED tests pass

#### RED — Denied subcommands

- [ ] Write tests that each explicitly denied subcommand (`commit`,
      `reset`, `clean`, `config`, `remote`, `reflog`, `gc`, `prune`,
      `filter-branch`, `replace`) exits non-zero with a message naming
      the denied subcommand
- [ ] Write a test that `commit` denial message suggests `vrg-commit`
- [ ] Expected failure: denied subcommands are not yet distinguished
      from unrecognized ones (both rejected, but error messages differ)

#### GREEN — Denied subcommands

- [ ] Add an explicit deny list that is checked before the
      unrecognized-subcommand fallback, with per-subcommand error
      messages that suggest the VRG alternative where one exists
- [ ] All RED tests pass

#### RED — Flag deny lists

- [ ] Write tests that denied flags on allowed subcommands are
      rejected:
      - `branch -D`, `branch --force`
      - `push --force`, `push --force-with-lease`, `push -f`
      - `checkout -- .`, `checkout -- *`
      - `rebase -i`, `rebase --interactive`
- [ ] Write tests that the same subcommands with allowed flags pass:
      - `branch -d some-branch` (safe delete)
      - `push origin feature/foo` (normal push)
      - `checkout -- src/specific/file.py` (specific-file restore)
      - `rebase main` (non-interactive)
- [ ] Expected failure: no flag validation exists yet

#### GREEN — Flag deny lists

- [ ] Add per-subcommand flag scanning that checks remaining arguments
      against deny lists before passing through to `subprocess.run`
- [ ] For `checkout`: deny `--` followed by `.` or `*` specifically,
      not `--` followed by a file path
- [ ] All RED tests pass

#### RED — Invocation logging

- [ ] Write a test that a successful (allowed) invocation appends a
      log entry with command, arguments, "allowed", and timestamp
- [ ] Write a test that a denied invocation appends a log entry with
      command, arguments, "denied", and timestamp
- [ ] Write a test that the log directory is created if it does not
      exist
- [ ] Expected failure: no logging exists yet

#### GREEN — Invocation logging

- [ ] Add logging that appends to a local log file
      (`~/.local/share/vergil/vrg-git.log` or similar) on every
      invocation, recording command, arguments, allowed/denied, and
      timestamp
- [ ] Create the log directory if it does not exist
- [ ] All RED tests pass

#### REFACTOR

- [ ] Review for duplicated validation logic that should be extracted
      (allowlist lookup, flag scanning, error formatting)
- [ ] Check whether the deny list error messages are consistent in
      format
- [ ] Verify the subprocess call uses `shell=False` and passes
      arguments as a list, not a string
- [ ] Look for hard-coded paths that should be configurable or use
      platform-appropriate defaults

### Task 2: `vrg-gh` Wrapper

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

**Requirement:** Spec Section 3 — validate gh two-level subcommand
pairs before execution; deny dangerous operations; log all invocations.

**Files:**
`src/vergil_tooling/bin/vrg_gh.py`,
`tests/test_vrg_gh.py`,
`pyproject.toml` (console script entry)

#### RED — Two-level subcommand allowlist

- [ ] Write tests that each allowed subcommand pair passes validation
      and reaches `subprocess.run` (mock the subprocess call):
      - `issue`: `view`, `create`, `close`, `edit`, `list`, `comment`
      - `pr`: `view`, `checks`, `list`, `diff`, `comment`, `edit`
      - `run`: `list`, `view`, `watch`
      - `repo`: `view`
      - `label`: `list`, `create`
- [ ] Write a test that an unrecognized top-level subcommand (e.g.,
      `codespace`) exits non-zero with a clear error message
- [ ] Write a test that an unrecognized second-level subcommand under
      a known top-level (e.g., `issue pin`) exits non-zero
- [ ] Write a test that invoking `vrg-gh` with no arguments exits
      non-zero with a usage message
- [ ] Write a test that invoking `vrg-gh` with only a top-level
      subcommand and no second-level (e.g., `vrg-gh issue`) exits
      non-zero
- [ ] Expected failure: no implementation exists yet

#### GREEN — Two-level subcommand allowlist

- [ ] Create `src/vergil_tooling/bin/vrg_gh.py` with a `main()` entry
      point that reads `sys.argv[1:]`, validates the first two
      arguments as a subcommand pair against a nested allowlist (dict
      of dicts), and calls `subprocess.run(["gh"] + args, shell=False)`
- [ ] Reject unrecognized top-level subcommands
- [ ] Reject unrecognized second-level subcommands under known
      top-level commands
- [ ] Reject invocations with fewer than two arguments
- [ ] Add `vrg-gh` console script entry point in `pyproject.toml`
- [ ] All RED tests pass

#### RED — Denied subcommand pairs and top-level denials

- [ ] Write tests that each explicitly denied subcommand pair exits
      non-zero with a message naming the denied operation:
      - `pr merge`, `pr close`, `pr create`
      - `repo edit`, `repo create`, `repo delete`
- [ ] Write tests that top-level denials reject the entire subtree:
      - `api` with any arguments (e.g., `api repos/...`)
      - `auth` with any arguments (e.g., `auth login`)
- [ ] Expected failure: denied pairs are not yet distinguished from
      unrecognized ones (both rejected, but error messages differ)

#### GREEN — Denied subcommand pairs and top-level denials

- [ ] Add an explicit deny list checked before the
      unrecognized-subcommand fallback, with per-pair error messages
      that suggest VRG alternatives where they exist (e.g., "use
      vrg-submit-pr instead of gh pr create")
- [ ] Handle top-level denials (`api`, `auth`) by rejecting before
      checking for a second-level subcommand
- [ ] All RED tests pass

#### RED — `pr review` flag gating

- [ ] Write a test that `pr review` without `--approve` passes
      validation (agents can comment on reviews)
- [ ] Write a test that `pr review --approve` is rejected with a
      message explaining agents cannot approve PRs
- [ ] Write a test that `pr review --comment -b "looks good"` passes
- [ ] Expected failure: no per-subcommand flag validation exists yet

#### GREEN — `pr review` flag gating

- [ ] Add flag scanning for `pr review` that denies `--approve`
      specifically while allowing other flags
- [ ] All RED tests pass

#### RED — Invocation logging

- [ ] Write a test that a successful (allowed) invocation appends a
      log entry with command, arguments, "allowed", and timestamp
- [ ] Write a test that a denied invocation appends a log entry with
      command, arguments, "denied", and timestamp
- [ ] Expected failure: no logging exists yet

#### GREEN — Invocation logging

- [ ] Add logging that appends to a local log file
      (`~/.local/share/vergil/vrg-gh.log` or similar), same format
      as `vrg-git`
- [ ] All RED tests pass

#### REFACTOR

- [ ] Review for duplicated logic shared with `vrg-git` (feeds into
      Task 3's extraction decision)
- [ ] Check whether error messages are consistent across all deny
      types (top-level denied, pair denied, flag denied, unrecognized)
- [ ] Verify the subprocess call uses `shell=False` and passes
      arguments as a list
- [ ] Look for hard-coded paths that should be configurable or use
      platform-appropriate defaults

### Task 3: Shared Infrastructure

**Files:**
`src/vergil_tooling/lib/wrapper.py` (new, if warranted)

Evaluate during Task 1/2 implementation whether shared code between
the two wrappers warrants extraction. Both wrappers share: argument
parsing pattern (allowlist lookup + flag deny), logging format,
error message format, `subprocess.run` invocation.

- [ ] If the two implementations share substantial code, extract
      shared logic into `src/vergil_tooling/lib/wrapper.py`
- [ ] If not, skip — prefer duplication over premature abstraction

### Task 4: Verify `Bash(git *)` Pattern Matching

**Status: RESOLVED** — verified via Claude Code documentation
(permissions.md). `Bash(git *)` uses word-boundary matching: the
space before `*` means `git status` matches but `vrg-git status`
does not. Fully qualified path bypass (`/usr/bin/git status`) is
closed by adding `Bash(*/git *)` and `Bash(*/gh *)` deny patterns.
Compound commands are evaluated per-subcommand. Results documented
in the spec (Section 4, pattern matching note).

### Task 5: Validation and Release

**Files:** `pyproject.toml` (version bump if needed)

- [ ] Run `vrg-docker-run -- uv run vrg-validate` — full pipeline
      must pass with the new wrappers and tests
- [ ] Verify `uv tool install` installs both `vrg-git` and `vrg-gh`
      as available console scripts
- [ ] Commit and PR for the wrapper code (Tasks 1-3)

---

## Phase 2: Update Supporting Infrastructure

With the wrappers available, update the guidance and hook layers to
point agents toward them.

### Task 6: Update Vergil Plugin Hook Messages

**Files:** vergil-claude-plugin hooks (separate repo)

Existing hooks that block raw `git` and `gh` usage should update
their error messages to point agents to the wrappers.

- [ ] `block-raw-git-commit`: update message to reference `vrg-commit`
      (already does) and note that all `git` operations should use
      `vrg-git`
- [ ] `block-raw-gh-pr-create`: update message to reference
      `vrg-submit-pr` (already does) and note that all `gh` operations
      should use `vrg-gh`
- [ ] `block-agent-merge`: update message to note that `vrg-gh`
      rejects `pr merge` as well
- [ ] `block-github-contents-api`: update message to note that
      `vrg-gh` denies `gh api` entirely
- [ ] No hooks are removed — they remain as Layer 3 backstops
- [ ] Commit and PR for hook message updates

### Task 7: Update CLAUDE.md Across All Repos

**Files:** `CLAUDE.md` in vergil-tooling, vergil-actions,
vergil-docker, vergil-claude-plugin

Add a section to each repo's CLAUDE.md instructing agents to use
the wrappers.

- [ ] Add a "Shell command policy" section (or equivalent) to each
      CLAUDE.md:
      - Use `vrg-git` instead of `git` for all git operations
      - Use `vrg-gh` instead of `gh` for all GitHub CLI operations
      - These are not optional preferences — raw `git` and `gh` are
        denied by the permission model
      - If a command is not available through the wrappers, explain
        the situation to the human who can run it via `! <command>`
- [ ] Verify the wording is consistent across all four repos
- [ ] One PR per repo (can run in parallel)

---

## Phase 3: Deploy Permission Configuration

Settings files are deployed to repos and the global mode is switched.
This is the "flip the switch" phase — everything before this was
preparation.

### Task 8: Deploy Project Settings to Consuming Repos

**Files:** `.claude/settings.json` in vergil-actions, vergil-docker,
vergil-claude-plugin

Each consuming repo gets the project-level `Bash(vrg-*)` allowlist.

- [ ] Update `.claude/settings.json` in vergil-actions to add:
      ```json
      {
        "permissions": {
          "allow": ["Bash(vrg-*)"]
        }
      }
      ```
      (Merged with existing plugin marketplace config)
- [ ] Same for vergil-docker
- [ ] Same for vergil-claude-plugin
- [ ] Document the `.claude/settings.local.json` template for phase 1
      read-only exceptions in CONTRIBUTING.md or developer docs — this
      is gitignored and per-developer, so it cannot be committed
- [ ] One PR per repo (can run in parallel)

### Task 9: Deploy Permission Configuration for vergil-tooling

**Files:**
`.claude/settings.json` (project-level),
`~/.claude/settings.json` (global, manual),
`~/.claude/settings.local.json` (global local, manual)

Deploy the full permission stack for vergil-tooling itself.

- [ ] Update `.claude/settings.json` (project-level) to add the
      `Bash(vrg-*)` allowlist (merged with existing plugin config)
- [ ] Prepare but do not yet apply the global settings changes:
      ```json
      {
        "permissions": {
          "defaultMode": "acceptEdits",
          "deny": [
            "Bash(git *)",
            "Bash(*/git *)",
            "Bash(gh *)",
            "Bash(*/gh *)"
          ]
        }
      }
      ```
      These are applied in Task 10, not here — the project-level
      changes can be committed and merged first.
- [ ] Prepare the `.claude/settings.local.json` template with the
      phase 1 read-only exceptions (from the spec's Section 4)
- [ ] Commit and PR for the project-level settings change

### Task 10: Switch Global Default Mode

**Files:** `~/.claude/settings.json` (manual, not committed anywhere)

This is the migration moment. All wrappers, hooks, CLAUDE.md updates,
and project settings must be deployed and merged before this step.

- [ ] Confirm prerequisites:
      - `vrg-git` and `vrg-gh` are installed and working
      - All four repos have updated CLAUDE.md
      - All four repos have project-level `Bash(vrg-*)` allowlist
      - Task 4 (pattern matching verification) is complete and results
        are documented
- [ ] Apply global `~/.claude/settings.json`:
      ```json
      {
        "permissions": {
          "defaultMode": "acceptEdits",
          "deny": [
            "Bash(git *)",
            "Bash(*/git *)",
            "Bash(gh *)",
            "Bash(*/gh *)"
          ]
        }
      }
      ```
- [ ] Apply `~/.claude/settings.local.json` with the phase 1
      read-only exceptions
- [ ] Remove `skipDangerousModePermissionPrompt` from global settings
      (no longer needed when not in bypass mode)

---

## Phase 4: Observe and Refine

### Task 11: One-Week Observation Period

**Files:** None (operational)

Run the new permission model for one week of normal development
across all four repos. Collect data on prompt frequency, wrapper
usage, and friction points.

- [ ] Monitor: which commands trigger prompts most frequently?
      Candidates for new VRG tools or allowlist additions.
- [ ] Monitor: which wrapper subcommands are agents using most?
      Validates the allowlist design.
- [ ] Monitor: are agents finding workarounds or getting stuck?
      Indicates missing allowlist entries or overly restrictive
      wrappers.
- [ ] Review `vrg-git` and `vrg-gh` logs for patterns
- [ ] After one week: update issue #754 with findings and open
      follow-up issues for any phase 2 work identified
- [ ] If the prompt volume is unacceptably high, adjust the
      `.claude/settings.local.json` read-only exceptions. If specific
      `vrg-git` or `vrg-gh` subcommands are missing, add them to the
      wrappers.

---

## Implementation Notes

### Task Dependencies

```text
Task 1 (vrg-git) ──┐
Task 2 (vrg-gh) ───┤
Task 3 (shared) ───┼── Task 4 (pattern verify) ── Task 5 (validate/PR)
                   │
                   ├── Task 6 (hook messages)  ─┐
                   ├── Task 7 (CLAUDE.md × 4) ──┤
                   │                            ├── Task 10 (switch mode)
                   ├── Task 8 (consuming repos) ┤
                   └── Task 9 (vergil-tooling) ─┘
                                                │
                                           Task 11 (observe)
```

Tasks 1-3 can proceed in parallel. Task 4 begins after Tasks 1-3 complete. Tasks 6, 7, 8, 9 can proceed
in parallel after Task 5 merges. Task 10 is the gating step — all
prerequisites must be merged. Task 11 follows Task 10.

### Scope Boundaries

- **In scope:** wrapper tools, permission config, CLAUDE.md updates,
  hook message updates, pattern matching verification.
- **Out of scope:** credential management (covered by #717, #764,
  #765), unified `vrg` CLI (phase 2/3), `vrg-search` or other
  read-only tool replacements (phase 2), Windows/Linux credential
  backends (#764, #765).

### Risk Mitigations

- **Task 4 is a gate.** If `Bash(git *)` matches `vrg-git`, the
  deny rules cannot go in Claude Code settings. The plan has an
  explicit fallback: hook-only enforcement for the deny layer.
- **Phase 1 read-only exceptions are temporary.** They live in
  gitignored local settings so they can be tightened per-developer
  without requiring a PR.
- **The switch (Task 10) is reversible.** If the prompt volume is
  unmanageable, reverting `~/.claude/settings.json` to
  `bypassPermissions` restores the prior behavior. The wrappers and
  hooks remain as improvements regardless.
